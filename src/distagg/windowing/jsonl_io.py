"""Strict JSONL reader with physical line numbers and duplicate-key detection.

The window contract is deliberately unforgiving: a blank line, a truncated
record, a duplicate JSON member inside any object, or a value of the wrong
type fails the whole job and reports the physical 1-based line number.  No
default values are guessed and identical lines are never merged.
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO, Iterator, Optional

from .errors import WindowInputError

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


class _DuplicateKeyError(ValueError):
    """Raised inside ``object_pairs_hook`` when an object repeats a member."""

    def __init__(self, key: str):
        super().__init__(f"duplicate JSON member {key!r}")
        self.key = key


def _reject_duplicates(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKeyError(key)
        seen.add(key)
    return dict(pairs)


def parse_jsonl_line(raw: bytes, line_number: int, path: Optional[str] = None) -> Any:
    """Parse one physical JSONL line under the strict contract."""
    if not raw:
        raise WindowInputError(line_number, "empty line is not a valid JSON object", path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WindowInputError(
            line_number, f"line is not valid UTF-8: {exc}", path
        ) from exc
    if not text.strip():
        raise WindowInputError(line_number, "blank line is not a valid JSON object", path)
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError as exc:
        raise WindowInputError(line_number, str(exc), path) from exc
    except json.JSONDecodeError as exc:
        raise WindowInputError(
            line_number, f"invalid JSON: {exc.msg} at column {exc.colno}", path
        ) from exc
    if not isinstance(obj, dict):
        raise WindowInputError(
            line_number,
            f"top-level JSON value must be an object, got {type(obj).__name__}",
            path,
        )
    return obj


def iter_jsonl(
    handle: BinaryIO,
    *,
    path: Optional[str] = None,
    max_row_bytes: Optional[int] = None,
) -> Iterator[tuple]:
    """Yield ``(line_number, raw_bytes, parsed_object)`` for each physical line.

    Lines are split on ``\\n`` (a trailing newline does not create an extra
    empty record; an interior blank line still fails).  ``\\r`` characters are
    part of the line and JSON's whitespace handling accepts CRLF input.
    """
    line_number = 0
    while True:
        raw = handle.readline()
        if raw == b"":
            return
        line_number += 1
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if max_row_bytes is not None and len(raw) > max_row_bytes:
            raise WindowInputError(
                line_number,
                f"line is {len(raw)} bytes, exceeding the single-row limit "
                f"of {max_row_bytes} bytes",
                path,
            )
        yield line_number, raw, parse_jsonl_line(raw, line_number, path)


def validate_row(
    obj: dict,
    *,
    line_number: int,
    partition_fields,
    order_field: str,
    value_field: str,
    path: Optional[str] = None,
):
    """Validate one record and return ``(partition_values, ts, value)``.

    * every configured field must be present at the top level;
    * partition values must be strings or null (two nulls share a group);
    * the order value must be a signed 64-bit integer (no bool, float, null);
    * the value must be an arbitrary-precision integer or null (no bool/float).
    """
    for name in list(partition_fields) + [order_field, value_field]:
        if name not in obj:
            raise WindowInputError(
                line_number, f"missing required field {name!r}", path
            )

    partition_values = []
    for name in partition_fields:
        value = obj[name]
        if value is not None and not isinstance(value, str):
            raise WindowInputError(
                line_number,
                f"partition field {name!r} must be a string or null, got "
                f"{type(value).__name__}",
                path,
            )
        partition_values.append(value)

    ts = obj[order_field]
    # bool is a subclass of int: reject it explicitly alongside floats/null.
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise WindowInputError(
            line_number,
            f"order field {order_field!r} must be a signed 64-bit integer, got "
            f"{type(ts).__name__}",
            path,
        )
    if ts < INT64_MIN or ts > INT64_MAX:
        raise WindowInputError(
            line_number,
            f"order field {order_field!r} value {ts} is outside the signed "
            f"64-bit range [{INT64_MIN}, {INT64_MAX}]",
            path,
        )

    value = obj[value_field]
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise WindowInputError(
            line_number,
            f"value field {value_field!r} must be an integer or null, got "
            f"{type(value).__name__}",
            path,
        )

    return partition_values, ts, value
