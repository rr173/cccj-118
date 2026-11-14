"""Sorted on-disk segments ("SSTables") for the staging store.

A segment is an immutable, self-encoded file holding a sorted run of
versioned records.  No external key/value engine is involved; the layout is:

::

    [data block][data block]...[index section][footer][footer_len][magic]

Data block
    A run of records followed by a 4-byte little-endian CRC-32 of the record
    bytes.  Records in a block are ordered by ``(key asc, commit desc)`` and
    **all versions of one key always live in a single block** (the writer
    only closes a block before starting a new key), so a point lookup needs
    at most one block per segment.  Blocks may exceed the target size when
    one key carries many versions.

Record
    ``flags(1) | commit uvarint | key_len uvarint | key | value_len uvarint
    | value`` where ``flags & 1`` marks a deletion tombstone (no value
    bytes follow).  An empty key or empty value is legal and distinct from
    a tombstone.

Index section (sparse)
    ``entry_count uvarint`` then per block ``first_key_len uvarint |
    first_key | block_offset uvarint | block_len uvarint`` (block_len
    includes the block CRC), then a 4-byte CRC-32 of the preceding index
    bytes.  One entry per block: the reader binary-searches the first keys
    to find the single candidate block for a lookup.  The index is cached
    in memory while the store is open; record data is never bulk-loaded.

Footer
    ``min_key | max_key | min_commit | max_commit | record_count |
    index_offset | index_len`` (length-prefixed keys, uvarint numbers),
    followed by a 4-byte CRC-32 of those bytes, then ``footer_len`` as a
    4-byte little-endian integer and finally the 4-byte magic ``b"DSG1"``.
    A file that does not end with the magic, or whose footer/index CRC does
    not match, is rejected as corrupt — never silently rebuilt.
"""

from __future__ import annotations

import os
import struct
import threading
from bisect import bisect_right
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .errors import ClosedError, CorruptionError
from .format import BadEncoding, NeedMore, crc32, put_bytes, put_uvarint, read_bytes, read_uvarint

SEGMENT_MAGIC = b"DSG1"
FLAG_TOMBSTONE = 0x01
_TRAILER = 4  # CRC-32 LE appended to blocks and to the index section


@dataclass
class SegmentMeta:
    """The manifest-visible facts about one segment file."""

    file: str
    min_key: bytes
    max_key: bytes
    min_commit: int
    max_commit: int
    records: int
    bytes: int

    def to_json(self) -> dict:
        return {
            "file": self.file,
            "min_key_hex": self.min_key.hex(),
            "max_key_hex": self.max_key.hex(),
            "min_commit": self.min_commit,
            "max_commit": self.max_commit,
            "records": self.records,
            "bytes": self.bytes,
        }

    @classmethod
    def from_json(cls, data: dict, source: str) -> "SegmentMeta":
        expected = {"file", "min_key_hex", "max_key_hex", "min_commit", "max_commit", "records", "bytes"}
        if not isinstance(data, dict) or set(data) != expected:
            raise CorruptionError(f"{source}: malformed segment entry {data!r}")
        try:
            return cls(
                file=_checked_name(data["file"], source),
                min_key=bytes.fromhex(data["min_key_hex"]),
                max_key=bytes.fromhex(data["max_key_hex"]),
                min_commit=_checked_int(data["min_commit"], source),
                max_commit=_checked_int(data["max_commit"], source),
                records=_checked_int(data["records"], source),
                bytes=_checked_int(data["bytes"], source),
            )
        except (TypeError, ValueError) as exc:
            raise CorruptionError(f"{source}: malformed segment entry: {exc}") from exc


def _checked_name(value, source) -> str:
    if not isinstance(value, str) or "/" in value or "\\" in value or value in ("", ".", ".."):
        raise CorruptionError(f"{source}: bad file name {value!r}")
    return value


def _checked_int(value, source) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CorruptionError(f"{source}: bad non-negative integer {value!r}")
    return value


class SegmentWriter:
    """Builds one segment file; records must arrive in (key asc, commit desc) order."""

    def __init__(self, tmp_path: str, block_size: int = 4096):
        self._tmp_path = tmp_path
        self._block_size = max(64, block_size)
        self._fh = open(tmp_path, "wb")
        self._pos = 0
        self._block = bytearray()
        self._block_first_key: Optional[bytes] = None
        self._block_last_key: Optional[bytes] = None
        self._index: List[Tuple[bytes, int, int]] = []
        self._last_key: Optional[bytes] = None
        self._last_commit: Optional[int] = None
        self._min_key: Optional[bytes] = None
        self._max_key: Optional[bytes] = None
        self._min_commit: Optional[int] = None
        self._max_commit: Optional[int] = None
        self.record_count = 0

    def add(self, key: bytes, commit: int, value: Optional[bytes]) -> None:
        if self._last_key is not None:
            if key < self._last_key or (key == self._last_key and commit >= self._last_commit):
                raise ValueError(
                    "segment records must be added in (key asc, commit desc) order"
                )
        if (
            self._block
            and key != self._block_last_key
            and len(self._block) >= self._block_size
        ):
            self._flush_block()
        if not self._block:
            self._block_first_key = key
        record = bytearray()
        record.append(FLAG_TOMBSTONE if value is None else 0)
        put_uvarint(record, commit)
        put_bytes(record, key)
        if value is not None:
            put_bytes(record, value)
        self._block += record
        self._block_last_key = key
        self._last_key = key
        self._last_commit = commit
        if self._min_key is None:
            self._min_key = key
        self._max_key = key
        self._min_commit = commit if self._min_commit is None else min(self._min_commit, commit)
        self._max_commit = commit if self._max_commit is None else max(self._max_commit, commit)
        self.record_count += 1

    def _flush_block(self) -> None:
        if not self._block:
            return
        data = bytes(self._block)
        blob = data + struct.pack("<I", crc32(data))
        self._fh.write(blob)
        self._index.append((self._block_first_key, self._pos, len(blob)))
        self._pos += len(blob)
        self._block = bytearray()
        self._block_first_key = None

    def finish(self, final_path: str) -> SegmentMeta:
        """Write index + footer, fsync, and atomically rename into place."""
        self._flush_block()
        if self.record_count == 0:
            raise ValueError("cannot finish an empty segment")
        index = bytearray()
        put_uvarint(index, len(self._index))
        for first_key, offset, length in self._index:
            put_bytes(index, first_key)
            put_uvarint(index, offset)
            put_uvarint(index, length)
        index += struct.pack("<I", crc32(bytes(index)))
        index_offset = self._pos
        self._fh.write(index)
        self._pos += len(index)
        footer = bytearray()
        put_bytes(footer, self._min_key)
        put_bytes(footer, self._max_key)
        put_uvarint(footer, self._min_commit)
        put_uvarint(footer, self._max_commit)
        put_uvarint(footer, self.record_count)
        put_uvarint(footer, index_offset)
        put_uvarint(footer, len(index))
        footer += struct.pack("<I", crc32(bytes(footer)))
        self._fh.write(footer)
        self._fh.write(struct.pack("<I", len(footer)))
        self._fh.write(SEGMENT_MAGIC)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        os.replace(self._tmp_path, final_path)
        _fsync_dir(os.path.dirname(final_path) or ".")
        return SegmentMeta(
            file=os.path.basename(final_path),
            min_key=self._min_key,
            max_key=self._max_key,
            min_commit=self._min_commit,
            max_commit=self._max_commit,
            records=self.record_count,
            bytes=os.path.getsize(final_path),
        )

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse_block(data: bytes, path: str, offset: int):
    """Decode one block's records; the trailing CRC was already checked."""
    records = []
    pos = 0
    try:
        while pos < len(data):
            flags = data[pos]
            pos += 1
            commit, pos = read_uvarint(data, pos)
            key, pos = read_bytes(data, pos)
            if flags & FLAG_TOMBSTONE:
                value = None
            else:
                value, pos = read_bytes(data, pos)
            records.append((key, commit, value))
    except (NeedMore, BadEncoding) as exc:
        raise CorruptionError(
            f"segment {path}: malformed record in block at offset {offset}: {exc}",
            path=path, offset=offset,
        ) from exc
    return records


class SegmentReader:
    """Reads one segment file; caches only the sparse index in memory.

    ``pins`` and ``obsolete`` are managed by the owning store (under its
    lock): an obsolete reader whose pin count reaches zero is closed and its
    file deleted.  ``blocks_read``/``bytes_read`` feed the access statistics
    used to demonstrate that point lookups touch only candidate blocks.
    """

    def __init__(self, path: str, meta: SegmentMeta, index: List[Tuple[bytes, int, int]]):
        self.path = path
        self.meta = meta
        self._index = index
        self._first_keys = [entry[0] for entry in index]
        self._fh = open(path, "rb")
        self._lock = threading.Lock()
        self._closed = False
        self.pins = 0
        self.obsolete = False
        self.blocks_read = 0
        self.bytes_read = 0

    # -- facts, mirroring the manifest entry ------------------------------
    @property
    def min_key(self) -> bytes:
        return self.meta.min_key

    @property
    def max_key(self) -> bytes:
        return self.meta.max_key

    @property
    def min_commit(self) -> int:
        return self.meta.min_commit

    @property
    def max_commit(self) -> int:
        return self.meta.max_commit

    @property
    def record_count(self) -> int:
        return self.meta.records

    @property
    def index_entries(self) -> int:
        return len(self._index)

    @classmethod
    def open(cls, path: str, expected: Optional[SegmentMeta] = None) -> "SegmentReader":
        """Open and validate footer + sparse index (data blocks are checked per read)."""
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise CorruptionError(f"segment {path}: cannot stat: {exc}", path=path) from exc
        if size < 8:
            raise CorruptionError(f"segment {path}: too small to be a segment", path=path)
        with open(path, "rb") as handle:
            handle.seek(size - 8)
            trailer = handle.read(8)
            footer_len = struct.unpack("<I", trailer[:4])[0]
            if trailer[4:] != SEGMENT_MAGIC:
                raise CorruptionError(
                    f"segment {path}: bad magic; not a committed segment file", path=path
                )
            if footer_len > size - 8:
                raise CorruptionError(f"segment {path}: footer length out of range", path=path)
            footer_start = size - 8 - footer_len
            handle.seek(footer_start)
            footer = handle.read(footer_len)
            if len(footer) != footer_len:
                raise CorruptionError(f"segment {path}: short footer", path=path)
            if crc32(footer[:-4]) != struct.unpack("<I", footer[-4:])[0]:
                raise CorruptionError(
                    f"segment {path}: footer checksum mismatch", path=path, offset=footer_start
                )
            try:
                min_key, pos = read_bytes(footer, 0)
                max_key, pos = read_bytes(footer, pos)
                min_commit, pos = read_uvarint(footer, pos)
                max_commit, pos = read_uvarint(footer, pos)
                record_count, pos = read_uvarint(footer, pos)
                index_offset, pos = read_uvarint(footer, pos)
                index_len, pos = read_uvarint(footer, pos)
            except (NeedMore, BadEncoding) as exc:
                raise CorruptionError(
                    f"segment {path}: malformed footer: {exc}", path=path, offset=footer_start
                ) from exc
            if pos != footer_len - 4:
                raise CorruptionError(
                    f"segment {path}: trailing bytes in footer", path=path, offset=footer_start
                )
            if index_offset + index_len > footer_start:
                raise CorruptionError(
                    f"segment {path}: index section out of range", path=path
                )
            handle.seek(index_offset)
            index_blob = handle.read(index_len)
            if len(index_blob) != index_len or index_len < 4:
                raise CorruptionError(
                    f"segment {path}: short index section", path=path, offset=index_offset
                )
            if crc32(index_blob[:-4]) != struct.unpack("<I", index_blob[-4:])[0]:
                raise CorruptionError(
                    f"segment {path}: index checksum mismatch", path=path, offset=index_offset
                )
            body = index_blob[:-4]
            try:
                count, pos = read_uvarint(body, 0)
                index = []
                for _ in range(count):
                    first_key, pos = read_bytes(body, pos)
                    block_offset, pos = read_uvarint(body, pos)
                    block_len, pos = read_uvarint(body, pos)
                    index.append((first_key, block_offset, block_len))
            except (NeedMore, BadEncoding) as exc:
                raise CorruptionError(
                    f"segment {path}: malformed index: {exc}", path=path, offset=index_offset
                ) from exc
            if pos != len(body):
                raise CorruptionError(
                    f"segment {path}: trailing bytes in index", path=path, offset=index_offset
                )
        meta = SegmentMeta(
            file=os.path.basename(path),
            min_key=min_key,
            max_key=max_key,
            min_commit=min_commit,
            max_commit=max_commit,
            records=record_count,
            bytes=size,
        )
        if expected is not None and expected != meta:
            raise CorruptionError(
                f"segment {path}: does not match the manifest entry "
                f"(manifest: {expected.to_json()}, file: {meta.to_json()})",
                path=path,
            )
        if not index:
            raise CorruptionError(f"segment {path}: empty index", path=path)
        return cls(path, meta, index)

    def _check_open(self) -> None:
        if self._closed:
            raise ClosedError(f"segment {self.path} is closed")

    def find_block_index(self, key: bytes) -> Optional[int]:
        """Index of the only block that can contain ``key`` (or ``None``)."""
        i = bisect_right(self._first_keys, key) - 1
        return i if i >= 0 else None

    def read_block(self, block_index: int):
        """Read and verify one block; returns ``(records, byte_length)``."""
        self._check_open()
        first_key, offset, length = self._index[block_index]
        with self._lock:
            self._fh.seek(offset)
            blob = self._fh.read(length)
        if len(blob) != length:
            raise CorruptionError(
                f"segment {self.path}: short read for block at offset {offset}",
                path=self.path, offset=offset,
            )
        data, trailer = blob[:-_TRAILER], blob[-_TRAILER:]
        if crc32(data) != struct.unpack("<I", trailer)[0]:
            raise CorruptionError(
                f"segment {self.path}: block checksum mismatch at offset {offset}",
                path=self.path, offset=offset,
            )
        records = _parse_block(data, self.path, offset)
        if records and records[0][0] != first_key:
            raise CorruptionError(
                f"segment {self.path}: block at offset {offset} does not match its index entry",
                path=self.path, offset=offset,
            )
        self.blocks_read += 1
        self.bytes_read += length
        return records, length

    def find(self, key: bytes):
        """All versions of ``key`` (newest commit first) or ``None``.

        Reads at most one data block: the sparse index names a single
        candidate block and all versions of a key are stored together.
        """
        i = self.find_block_index(key)
        if i is None:
            return None
        records, _ = self.read_block(i)
        versions = []
        for rec_key, commit, value in records:
            if rec_key == key:
                versions.append((commit, value))
            elif rec_key > key:
                break
        return versions or None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._fh.close()

    @property
    def closed(self) -> bool:
        return self._closed
