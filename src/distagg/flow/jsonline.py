"""Strict one-line JSON decoding for the flow sources.

The contract is deliberately unforgiving and matches the rest of distagg:

* the bytes of one physical line must decode as UTF-8;
* the line must contain exactly one JSON object;
* an object may not repeat a member name (at any nesting level);
* ``NaN`` / ``Infinity`` / ``-Infinity`` are not JSON and are rejected;
* a blank line, or a line that is not an object, is a source error.

Every error is reported with the source name and the physical 1-based line
number.  The check runs *inside* ``object_pairs_hook``, so a duplicate is
detected while the line is decoded rather than after building a dict.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Set

from .errors import SourceRecordError


class _DuplicateMember(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


class _NotJsonConstant(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def _reject_duplicates(pairs):
    seen: Set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateMember(key)
        seen.add(key)
    return dict(pairs)


def _reject_constant(text: str) -> Any:
    raise _NotJsonConstant(text)


def decode_line(raw: bytes, *, source: str, line_number: int,
                path: Optional[str] = None) -> dict:
    """Decode one physical line (bytes without the trailing newline)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceRecordError(
            source, line_number,
            f"line is not valid UTF-8 ({exc.reason} at byte {exc.start})", path
        ) from exc
    if not text.strip():
        raise SourceRecordError(
            source, line_number,
            "blank line is not a valid JSON object", path
        )
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except _DuplicateMember as exc:
        raise SourceRecordError(
            source, line_number,
            f"duplicate JSON member {exc.key!r} in one object", path
        ) from exc
    except _NotJsonConstant as exc:
        raise SourceRecordError(
            source, line_number,
            f"non-JSON constant {exc.text!r} is not allowed", path
        ) from exc
    except json.JSONDecodeError as exc:
        raise SourceRecordError(
            source, line_number,
            f"invalid JSON: {exc.msg} at column {exc.colno}", path
        ) from exc
    if not isinstance(value, dict):
        raise SourceRecordError(
            source, line_number,
            f"top-level JSON value must be an object, got {type(value).__name__}",
            path,
        )
    return value
