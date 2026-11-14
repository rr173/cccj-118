"""Write-ahead log for the staging store.

One WAL file is a sequence of frames.  Every committed batch is exactly one
frame, so a batch is either fully present or fully absent after a crash —
never half applied.

Frame layout (all integers via :mod:`.format`)::

    magic:       4 bytes, b"DWAL"
    type:        1 byte, 0x01 = batch
    payload_len: uvarint
    header_crc:  4 bytes LE, crc32 over (magic | type | payload_len bytes)
    payload:     payload_len bytes
    payload_crc: 4 bytes LE, crc32 over payload

Batch payload::

    commit:   uvarint   (the store-wide increasing commit number)
    op_count: uvarint
    ops:      op_count times:
                op_type: 1 byte (0x00 = delete, 0x01 = put)
                key:     uvarint length + raw bytes
                value:   uvarint length + raw bytes   (put only)

A batch is collapsed before it is logged (one final operation per key, last
operation in batch order wins), so a frame holds each key at most once.

Recovery rules
--------------
Frames are decoded strictly from the start:

* fewer bytes than a complete header, or a header announcing more payload
  bytes than remain -> the file was torn by a crash at the tail; recovery
  stops and reports the length of the valid prefix.  The writer truncates
  the file back to that prefix before appending again.
* a complete header with wrong magic/type or a bad header CRC, or a complete
  frame whose payload CRC does not match -> real corruption (never produced
  by a torn write, which can only leave a *prefix* of a frame).  Recovery
  raises :class:`CorruptionError` naming the file and the frame offset.

The writer buffers one frame in memory and only issues ``write`` + ``fsync``
in :meth:`WalWriter.sync`, which is what "the batch is persisted" means.
"""

from __future__ import annotations

import os
import struct

from .errors import CorruptionError
from .format import BadEncoding, NeedMore, crc32, put_bytes, put_uvarint, read_bytes, read_uvarint

WAL_MAGIC = b"DWAL"
FRAME_TYPE_BATCH = 0x01
OP_DELETE = 0x00
OP_PUT = 0x01

_HEADER_FIXED = len(WAL_MAGIC) + 1  # magic + type, before the varint length


def encode_batch_frame(commit: int, ops) -> bytes:
    """Build one complete frame for a collapsed batch.

    ``ops`` is an iterable of ``(key, value)`` pairs where ``value is None``
    marks a deletion.
    """
    payload = bytearray()
    put_uvarint(payload, commit)
    ops = list(ops)
    put_uvarint(payload, len(ops))
    for key, value in ops:
        if value is None:
            payload.append(OP_DELETE)
            put_bytes(payload, key)
        else:
            payload.append(OP_PUT)
            put_bytes(payload, key)
            put_bytes(payload, value)
    header = bytearray(WAL_MAGIC)
    header.append(FRAME_TYPE_BATCH)
    put_uvarint(header, len(payload))
    header += struct.pack("<I", crc32(bytes(header)))
    return bytes(header) + bytes(payload) + struct.pack("<I", crc32(bytes(payload)))


def decode_batch_payload(payload: bytes):
    """Parse a batch payload into ``(commit, [(key, value|None), ...])``."""
    try:
        commit, pos = read_uvarint(payload, 0)
        count, pos = read_uvarint(payload, pos)
        ops = []
        for _ in range(count):
            if pos >= len(payload):
                raise NeedMore("truncated op")
            op_type = payload[pos]
            pos += 1
            key, pos = read_bytes(payload, pos)
            if op_type == OP_DELETE:
                ops.append((key, None))
            elif op_type == OP_PUT:
                value, pos = read_bytes(payload, pos)
                ops.append((key, value))
            else:
                raise BadEncoding(f"unknown op type {op_type}")
        if pos != len(payload):
            raise BadEncoding("trailing bytes in batch payload")
        return commit, ops
    except NeedMore as exc:
        raise BadEncoding(f"short batch payload: {exc}") from exc


class WalWriter:
    """Appends frames to one WAL file; frames reach the OS only in ``sync``."""

    def __init__(self, path: str):
        self.path = path
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self._pending = bytearray()
        self._closed = False

    def append_batch(self, commit: int, ops) -> None:
        """Buffer one batch frame (not yet written to the OS)."""
        if self._closed:
            raise ValueError("wal writer is closed")
        self._pending += encode_batch_frame(commit, ops)

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    def sync(self) -> None:
        """Write buffered frames to the OS and fsync; the batch is durable after this."""
        if self._closed:
            raise ValueError("wal writer is closed")
        data = bytes(self._pending)
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        os.fsync(self._fd)
        self._pending.clear()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._fd)


def recover_wal(path: str):
    """Decode the valid prefix of a WAL file.

    Returns ``(batches, valid_length)`` where ``batches`` is a list of
    ``(commit, [(key, value|None), ...])`` in file order and ``valid_length``
    is the byte offset just past the last complete, checksum-valid frame.

    A truncated tail (a crash mid-write) simply ends the valid prefix.
    Anything else that does not decode — bad magic, bad header CRC, bad
    payload CRC, malformed payload — raises :class:`CorruptionError` with
    the exact frame offset, because a torn write can only shorten a frame,
    never produce a complete frame with wrong checksums.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    pos = 0
    batches = []
    size = len(data)
    while pos < size:
        frame_start = pos
        remaining = size - pos
        if remaining < _HEADER_FIXED + 1:
            break  # truncated tail: partial magic/type/length
        if data[pos:pos + 4] != WAL_MAGIC:
            raise CorruptionError(
                f"wal {path}: bad frame magic at offset {frame_start}",
                path=path, offset=frame_start,
            )
        if data[pos + 4] != FRAME_TYPE_BATCH:
            raise CorruptionError(
                f"wal {path}: unknown frame type {data[pos + 4]} at offset {frame_start}",
                path=path, offset=frame_start,
            )
        try:
            payload_len, after_len = read_uvarint(data, pos + 5)
        except NeedMore:
            break  # truncated tail inside the length varint
        header_end = after_len + 4
        if header_end > size:
            break  # truncated tail: header CRC missing
        header_crc = struct.unpack("<I", data[after_len:header_end])[0]
        if header_crc != crc32(data[frame_start:after_len]):
            raise CorruptionError(
                f"wal {path}: header checksum mismatch at offset {frame_start}",
                path=path, offset=frame_start,
            )
        frame_end = header_end + payload_len + 4
        if frame_end > size:
            break  # truncated tail: payload or payload CRC missing
        payload = data[header_end:header_end + payload_len]
        payload_crc = struct.unpack("<I", data[header_end + payload_len:frame_end])[0]
        if payload_crc != crc32(payload):
            raise CorruptionError(
                f"wal {path}: payload checksum mismatch at offset {frame_start}",
                path=path, offset=frame_start,
            )
        try:
            commit, ops = decode_batch_payload(payload)
        except BadEncoding as exc:
            raise CorruptionError(
                f"wal {path}: malformed batch at offset {frame_start}: {exc}",
                path=path, offset=frame_start,
            ) from exc
        batches.append((commit, ops))
        pos = frame_end
    return batches, pos
