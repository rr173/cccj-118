"""Record decoder for compiled copybook layouts.

Input files are sequences of RDW-framed records: a 4-byte record descriptor
word (big-endian halfword length that includes the RDW itself, followed by
two reserved bytes that must be zero) and then the payload.  There is no
block descriptor word.  Payload bytes are decoded field by field:

* text (``PIC X``) is decoded as cp037 — spaces, NULs and every other byte
  value are preserved exactly, nothing is trimmed;
* unsigned ``DISPLAY`` numerics accept only EBCDIC digit bytes (F0-F9);
* ``COMP`` is big-endian binary in 2/4/8 bytes (by PIC digit count), signed
  only when ``S`` is present, and range-checked against the PIC digits;
* ``COMP-3`` is packed decimal: every digit nibble must be 0-9, the final
  nibble is C/F (non-negative) or D (negative), and a padding nibble, when
  present, must be zero;
* implied decimal places occupy no bytes; numeric values are emitted as
  canonical decimal strings that keep the declared scale, never as floats.

Records are decoded one at a time from a stream; memory use is proportional
to a single record, never to the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Dict, Iterator, Optional, Union

from .compiler import Alternation, CompiledLayout, Node
from .errors import DecodeError


def format_decimal(value: int, scale: int) -> str:
    """Canonical decimal string for a scaled integer, preserving ``scale``.

    ``format_decimal(12345, 2) == "123.45"``; ``format_decimal(-5, 2) ==
    "-0.05"``; ``format_decimal(0, 2) == "0.00"`` (negative zero is
    normalised).  No float is ever involved.
    """
    if value < 0:
        sign, magnitude = "-", -value
    else:
        sign, magnitude = "", value
    digits = str(magnitude)
    if scale == 0:
        return sign + digits
    digits = digits.rjust(scale + 1, "0")
    return f"{sign}{digits[:-scale]}.{digits[-scale:]}"


@dataclass
class DecodedRecord:
    """One decoded record: 1-based number, RDW file offset, payload length."""

    record_no: int
    offset: int
    length: int
    data: dict

    def to_dict(self) -> dict:
        return {
            "record": self.record_no,
            "offset": self.offset,
            "length": self.length,
            "data": self.data,
        }


class _Context:
    """Per-record decode state: the payload and the referenced-field values."""

    __slots__ = ("layout", "payload", "values")

    def __init__(self, layout: CompiledLayout, payload: bytes):
        self.layout = layout
        self.payload = payload
        self.values: Dict[str, Union[int, str]] = {}


# -- elementary field decoders -------------------------------------------------


def _need(ctx: _Context, pos: int, size: int, path: str) -> None:
    if pos + size > len(ctx.payload):
        raise DecodeError(
            f"record too short: field needs {size} byte(s) at payload offset "
            f"{pos}, but the record holds {len(ctx.payload)} byte(s)",
            field_path=path)


def _decode_text(ctx: _Context, node: Node, pos: int, path: str):
    _need(ctx, pos, node.byte_len, path)
    return ctx.payload[pos:pos + node.byte_len].decode("cp037"), pos + node.byte_len


def _decode_display(ctx: _Context, node: Node, pos: int, path: str):
    digits = node.pic.total_digits
    _need(ctx, pos, digits, path)
    value = 0
    for i in range(pos, pos + digits):
        byte = ctx.payload[i]
        if not 0xF0 <= byte <= 0xF9:
            raise DecodeError(
                f"byte at payload offset {i} is 0x{byte:02X}, not an EBCDIC "
                f"digit (F0-F9); illegal digits are never repaired to zero",
                field_path=path)
        value = value * 10 + (byte - 0xF0)
    return value, pos + digits


def _decode_comp(ctx: _Context, node: Node, pos: int, path: str):
    _need(ctx, pos, node.byte_len, path)
    raw = ctx.payload[pos:pos + node.byte_len]
    value = int.from_bytes(raw, "big", signed=node.pic.signed)
    limit = 10 ** node.pic.total_digits - 1
    if value > limit or value < -limit:
        raise DecodeError(
            f"binary value {value} does not fit in PIC {node.pic.raw.upper()} "
            f"(magnitude must be at most {limit})",
            field_path=path)
    return value, pos + node.byte_len


def _decode_comp3(ctx: _Context, node: Node, pos: int, path: str):
    _need(ctx, pos, node.byte_len, path)
    raw = ctx.payload[pos:pos + node.byte_len]
    sign = raw[-1] & 0x0F
    if sign in (0x0C, 0x0F):
        negative = False
    elif sign == 0x0D:
        negative = True
    else:
        raise DecodeError(
            f"invalid packed sign nibble 0x{sign:X} (C or F = non-negative, "
            f"D = negative)",
            field_path=path)
    if negative and not node.pic.signed:
        raise DecodeError(
            "negative sign (D) in an unsigned packed field; unsigned fields "
            "must not carry a negative sign",
            field_path=path)
    nibbles = []
    for i, byte in enumerate(raw):
        nibbles.append(byte >> 4)
        if i < len(raw) - 1:
            nibbles.append(byte & 0x0F)
    total = node.pic.total_digits
    if len(nibbles) == total + 1:
        if nibbles[0] != 0:
            raise DecodeError(
                f"packed padding nibble must be zero, got 0x{nibbles[0]:X}",
                field_path=path)
        nibbles = nibbles[1:]
    value = 0
    for k, nibble in enumerate(nibbles):
        if nibble > 9:
            raise DecodeError(
                f"invalid packed digit 0x{nibble:X} at digit position {k + 1} "
                f"(every digit nibble must be 0-9)",
                field_path=path)
        value = value * 10 + nibble
    if negative and value != 0:
        value = -value
    return value, pos + node.byte_len


def _decode_leaf(ctx: _Context, node: Node, pos: int, path: str):
    """Decode one occurrence of an elementary item.

    Returns ``(raw_value, new_pos)`` where raw_value is ``str`` for text and
    ``int`` (scaled by the implied decimal places) for numerics.  FILLER
    consumes its bytes without validation and returns ``None``.
    """
    if node.is_filler:
        _need(ctx, pos, node.byte_len, path)
        return None, pos + node.byte_len
    if node.pic.category == "text":
        return _decode_text(ctx, node, pos, path)
    if node.usage == "DISPLAY":
        return _decode_display(ctx, node, pos, path)
    if node.usage == "COMP":
        return _decode_comp(ctx, node, pos, path)
    return _decode_comp3(ctx, node, pos, path)


def _output_value(node: Node, raw):
    if raw is None or node.pic.category == "text":
        return raw
    return format_decimal(raw, node.pic.scale)


# -- structural walk -------------------------------------------------------------


def _occurs_count(node: Node, ctx: _Context, path: str) -> int:
    occurs = node.occurs
    if not occurs.variable:
        return occurs.max_times
    count = ctx.values.get(node.counter.path)
    if not isinstance(count, int):
        raise DecodeError(
            f"DEPENDING ON counter {node.counter.path} was not decoded before "
            f"this array", field_path=path)
    if not occurs.min_times <= count <= occurs.max_times:
        raise DecodeError(
            f"DEPENDING ON count {count} from {node.counter.path} is outside "
            f"the declared range {occurs.min_times} TO {occurs.max_times}",
            field_path=path)
    return count


def _decode_one(node: Node, ctx: _Context, pos: int, path: str):
    """Decode a single occurrence of ``node``; returns (value, new_pos)."""
    if node.is_group:
        result: Dict[str, object] = {}
        for entry in node.entries:
            pos = _decode_entry(entry, ctx, pos, result, path)
        return result, pos
    raw, pos = _decode_leaf(ctx, node, pos, path)
    if node.path is not None and node.path in ctx.layout.referenced:
        ctx.values[node.path] = raw
    return _output_value(node, raw), pos


def _decode_alternation(alt: Alternation, ctx: _Context, pos: int, out: dict,
                        parent_path: str) -> int:
    using = alt.selector_using
    raw = ctx.values.get(using.path)
    if raw is None:
        raise DecodeError(
            f"selector {using.path} was not decoded before this REDEFINES "
            f"group", field_path=f"{parent_path}.{alt.target.name}")
    key = raw if isinstance(raw, str) else format_decimal(raw, 0)
    branch = alt.value_map.get(key)
    alt_path = f"{parent_path}.{alt.target.name}"
    if branch is None:
        known = ", ".join(sorted(alt.value_map))
        raise DecodeError(
            f"unknown selection value {key!r} from selector {using.path} "
            f"(configured values: {known}); branches are never probed "
            f"speculatively",
            field_path=alt_path)
    _decode_item(branch, ctx, pos, out, parent_path)
    # Unselected branches and the slack up to the longest branch are skipped
    # without any validation.
    return pos + alt.span


def _decode_item(node: Node, ctx: _Context, pos: int, out: dict,
                 parent_path: str) -> int:
    """Decode one layout item (with its OCCURS, if any) into ``out``."""
    node_path = f"{parent_path}.{node.name if node.name else 'FILLER'}"
    if node.occurs is None:
        value, pos = _decode_one(node, ctx, pos, node_path)
        if node.name is not None:
            out[node.name] = value
        return pos
    count = _occurs_count(node, ctx, node_path)
    items = []
    for i in range(count):
        value, pos = _decode_one(node, ctx, pos, f"{node_path}[{i + 1}]")
        items.append(value)
    if node.name is not None:
        out[node.name] = items
    return pos


def _decode_entry(entry, ctx: _Context, pos: int, out: dict, parent_path: str) -> int:
    if isinstance(entry, Alternation):
        return _decode_alternation(entry, ctx, pos, out, parent_path)
    return _decode_item(entry, ctx, pos, out, parent_path)


def decode_record(layout: CompiledLayout, payload: bytes) -> dict:
    """Decode one record payload (the bytes after the RDW).

    The layout must consume the payload exactly: a short record fails at the
    field that overruns it, and leftover bytes are reported as trailing.
    """
    ctx = _Context(layout, payload)
    out: Dict[str, object] = {}
    pos = 0
    for entry in layout.root.entries:
        pos = _decode_entry(entry, ctx, pos, out, layout.root.name)
    if pos != len(payload):
        raise DecodeError(
            f"{len(payload) - pos} trailing byte(s) after {layout.record_name}: "
            f"the compiled layout consumed {pos} of {len(payload)} payload "
            f"byte(s); every record must be consumed exactly")
    return out


# -- RDW-framed file streaming -----------------------------------------------------


def iter_records(layout: CompiledLayout, fileobj: BinaryIO,
                 first_offset: int = 0) -> Iterator[DecodedRecord]:
    """Yield :class:`DecodedRecord` one at a time from an RDW-framed stream.

    Only the current record is held in memory.  A malformed record raises
    :class:`DecodeError` carrying the record number and file offset; records
    already yielded stay yielded.
    """
    offset = first_offset
    record_no = 0
    while True:
        header = fileobj.read(4)
        if not header:
            return
        record_no += 1
        record_offset = offset
        if len(header) < 4:
            raise DecodeError(
                f"truncated RDW: {len(header)} of 4 byte(s) available",
                record_no=record_no, offset=record_offset)
        length = int.from_bytes(header[0:2], "big")
        if header[2] != 0 or header[3] != 0:
            raise DecodeError(
                f"RDW reserved bytes must be zero, got "
                f"0x{header[2]:02X} 0x{header[3]:02X}",
                record_no=record_no, offset=record_offset)
        if length < 4:
            raise DecodeError(
                f"RDW length {length} is smaller than the 4-byte RDW itself",
                record_no=record_no, offset=record_offset)
        want = length - 4
        payload = fileobj.read(want)
        if len(payload) < want:
            raise DecodeError(
                f"truncated record: RDW promises {want} payload byte(s), only "
                f"{len(payload)} available before end of file",
                record_no=record_no, offset=record_offset)
        offset = record_offset + length
        try:
            data = decode_record(layout, payload)
        except DecodeError as exc:
            raise exc.with_record(record_no, record_offset)
        yield DecodedRecord(record_no=record_no, offset=record_offset,
                            length=want, data=data)


def iter_file(layout: CompiledLayout, path: str) -> Iterator[DecodedRecord]:
    """Open ``path`` and yield decoded records one at a time."""
    with open(path, "rb") as handle:
        yield from iter_records(layout, handle)
