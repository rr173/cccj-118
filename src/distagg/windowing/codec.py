"""Deterministic byte codecs for composite partition keys and big integers.

Partition component values are only ever ``str`` or ``None``.  We need a
single blob per row that

* keeps component boundaries (so ``("ab", "c")`` never collides with
  ``("a", "bc")``),
* treats two nulls as equal,
* preserves original string text byte-for-byte (no trimming, no case folding),
* sorts components independently of their contents.

The encoding is a tagged, escaped concatenation::

    null component  -> byte 0x00
    string component -> 0x01 + utf-8 bytes with 0x00 escaped as 0x00 0xFF
                        and terminator 0x02

Because both marker bytes and the escape cannot occur unescaped in the text
payload, the boundary is unambiguous and ordering of two distinct blobs is
determined by the first differing component (null sorts before any string).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

_NULL_MARKER = b"\x00"
_TEXT_MARKER = b"\x01"
_END_MARKER = b"\x02"
_ESCAPED_ZERO = b"\x00\xff"


def encode_partition(components: List[Optional[str]]) -> bytes:
    """Encode an ordered list of string/null partition values to a blob."""
    parts: List[bytes] = []
    for value in components:
        if value is None:
            parts.append(_NULL_MARKER)
        else:
            payload = value.encode("utf-8").replace(b"\x00", _ESCAPED_ZERO)
            parts.append(_TEXT_MARKER)
            parts.append(payload)
            parts.append(_END_MARKER)
    return b"".join(parts)


def decode_partition(blob: bytes) -> Tuple[Optional[str], ...]:
    """Inverse of :func:`encode_partition` (used by tests and diagnostics)."""
    result: List[Optional[str]] = []
    index = 0
    size = len(blob)
    while index < size:
        marker = blob[index]
        index += 1
        if marker == 0x00:
            result.append(None)
            continue
        if marker != 0x01:  # pragma: no cover - defensive, never written by us
            raise ValueError(f"invalid partition blob marker 0x{marker:02x}")
        collected = bytearray()
        while True:
            if index >= size:  # pragma: no cover - defensive
                raise ValueError("truncated partition blob")
            byte = blob[index]
            index += 1
            if byte == 0x02:
                break
            if byte == 0x00:
                if index >= size or blob[index] != 0xFF:  # pragma: no cover
                    raise ValueError("invalid escape sequence in partition blob")
                index += 1
                collected.append(0x00)
            else:
                collected.append(byte)
        result.append(collected.decode("utf-8"))
    return tuple(result)


def encode_integer(value: int) -> str:
    """Canonical decimal text used to store arbitrary-precision integers.

    Integers never cross SQLite's 64-bit arithmetic: they are stored as text
    and every sum/min/max is performed in Python.
    """
    return str(value)


def decode_integer(text: str) -> int:
    return int(text)
