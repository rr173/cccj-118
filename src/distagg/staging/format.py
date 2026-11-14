"""Binary encoding helpers shared by the staging store files.

Everything the staging store persists is built from these primitives:

* unsigned base-128 varints (little-endian groups, high bit = continuation),
* fixed 4-byte little-endian CRC-32 checksums (``zlib.crc32``),
* file names derived from a single monotonically increasing file id.

Keys and values themselves are opaque byte strings; they are never decoded,
only length-prefixed.  Ordering is the unsigned-byte lexicographic order of
Python ``bytes`` (``b"" < b"\\x00" < b"\\x00a" < b"a" < b"\\xff"``).
"""

from __future__ import annotations

import zlib


class NeedMore(Exception):
    """Internal: ran out of bytes while decoding (a truncated structure)."""


class BadEncoding(Exception):
    """Internal: bytes were present but malformed."""


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def put_uvarint(buf: bytearray, n: int) -> None:
    if not isinstance(n, int) or n < 0:
        raise ValueError(f"uvarint must be a non-negative int, got {n!r}")
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def read_uvarint(buf, pos: int):
    """Decode one uvarint from ``buf`` at ``pos``; return ``(value, new_pos)``."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise NeedMore("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise BadEncoding("varint too long")


def put_bytes(buf: bytearray, data: bytes) -> None:
    put_uvarint(buf, len(data))
    buf += data


def read_bytes(buf, pos: int):
    n, pos = read_uvarint(buf, pos)
    end = pos + n
    if end > len(buf):
        raise NeedMore("truncated byte string")
    return bytes(buf[pos:end]), end


# ---------------------------------------------------------------------------
# File names.  All files of one store live flat in the store directory.
#
#   LOCK                 taken (flock, exclusive) by the process that opens
#                        the store for writing
#   CURRENT              one line: the name of the live MANIFEST file
#   MANIFEST-<id>        JSON document: which files form the committed store
#   wal-<id>.log         write-ahead log, a sequence of batch frames
#   seg-<id>.sst         sorted segment (self-encoded, see segment.py)
#   *.tmp                anything being written; never part of the store and
#                        deleted on open
# ---------------------------------------------------------------------------

def wal_name(file_id: int) -> str:
    return f"wal-{file_id:06d}.log"


def segment_name(file_id: int) -> str:
    return f"seg-{file_id:06d}.sst"


def manifest_name(manifest_id: int) -> str:
    return f"MANIFEST-{manifest_id:06d}"


def parse_numbered_file(name: str, prefix: str, suffix: str):
    """Return the numeric id of ``prefix-<id><suffix>`` or ``None``."""
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return None
    middle = name[len(prefix):len(name) - len(suffix) if suffix else len(name)]
    if middle.isdigit():
        return int(middle)
    return None
