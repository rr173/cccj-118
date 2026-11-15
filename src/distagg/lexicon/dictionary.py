"""Strict streaming reader and validator for dictionary JSONL files.

Each physical line is one JSON object with exactly the members
``term_id``, ``priority`` and ``aliases``:

* ``term_id`` — non-empty string, unique across the whole file;
* ``priority`` — an integer (smaller wins in ``resolved`` mode); a
  boolean is *not* an integer;
* ``aliases`` — a non-empty array of ``{"text", "boundary"}`` objects;
  ``text`` is a non-empty string, ``boundary`` is ``"any"`` or
  ``"ascii_word"``.

Different terms may carry the same alias text, and one term may carry
aliases that fold to the same comparison string — nothing is dropped for
sharing a name.  Every rejected line raises :class:`LexiconDictionaryError`
naming the physical 1-based line and, where relevant, the 0-based alias
index, so a maintainer can find the record without guessing.
"""

from __future__ import annotations

import json
from typing import BinaryIO, Iterator, List, Optional, Tuple

from .errors import LexiconDictionaryError
from .folding import BOUNDARY_MODES, fold_with_mapping

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

_ALIAS_KEYS = {"text", "boundary"}
_RECORD_KEYS = {"term_id", "priority", "aliases"}

#: One validated alias: (alias_index, original text, boundary, folded text).
AliasEntry = Tuple[int, str, str, str]
#: One validated record: (line_number, term_id, priority, aliases).
RecordEntry = Tuple[int, str, int, List[AliasEntry]]


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


def _parse_line(raw: bytes, line_number: int, path: Optional[str]) -> dict:
    if not raw:
        raise LexiconDictionaryError(
            line_number, "empty line is not a valid JSON object", path=path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LexiconDictionaryError(
            line_number, f"line is not valid UTF-8: {exc}", path=path) from exc
    if not text.strip():
        raise LexiconDictionaryError(
            line_number, "blank line is not a valid JSON object", path=path)
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError as exc:
        raise LexiconDictionaryError(line_number, str(exc), path=path) from exc
    except json.JSONDecodeError as exc:
        raise LexiconDictionaryError(
            line_number, f"invalid JSON: {exc.msg} at column {exc.colno}",
            path=path) from exc
    if not isinstance(obj, dict):
        raise LexiconDictionaryError(
            line_number,
            f"top-level JSON value must be an object, got {type(obj).__name__}",
            path=path)
    return obj


def _validate_record(obj: dict, line_number: int,
                     path: Optional[str]) -> RecordEntry:
    unknown = sorted(set(obj) - _RECORD_KEYS)
    if unknown:
        raise LexiconDictionaryError(
            line_number, f"unknown member(s): {', '.join(unknown)}", path=path)
    for key in ("term_id", "priority", "aliases"):
        if key not in obj:
            raise LexiconDictionaryError(
                line_number, f"missing required member {key!r}", path=path)

    term_id = obj["term_id"]
    if not isinstance(term_id, str) or not term_id:
        raise LexiconDictionaryError(
            line_number,
            f"term_id must be a non-empty string, got {term_id!r}", path=path)

    priority = obj["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise LexiconDictionaryError(
            line_number,
            f"priority must be an integer (booleans are not integers), "
            f"got {priority!r}", path=path)
    if priority < INT64_MIN or priority > INT64_MAX:
        raise LexiconDictionaryError(
            line_number,
            f"priority {priority} is outside the signed 64-bit range "
            f"[{INT64_MIN}, {INT64_MAX}]", path=path)

    raw_aliases = obj["aliases"]
    if not isinstance(raw_aliases, list) or not raw_aliases:
        raise LexiconDictionaryError(
            line_number,
            f"aliases must be a non-empty array, got {raw_aliases!r}",
            path=path)

    aliases: List[AliasEntry] = []
    for index, item in enumerate(raw_aliases):
        if not isinstance(item, dict):
            raise LexiconDictionaryError(
                line_number,
                f"alias must be an object, got {type(item).__name__}",
                alias_index=index, path=path)
        unknown_alias_keys = sorted(set(item) - _ALIAS_KEYS)
        if unknown_alias_keys:
            raise LexiconDictionaryError(
                line_number,
                f"unknown alias member(s): {', '.join(unknown_alias_keys)}",
                alias_index=index, path=path)
        for key in ("text", "boundary"):
            if key not in item:
                raise LexiconDictionaryError(
                    line_number, f"alias is missing member {key!r}",
                    alias_index=index, path=path)
        text = item["text"]
        if not isinstance(text, str) or not text:
            raise LexiconDictionaryError(
                line_number,
                f"alias text must be a non-empty string, got {text!r}",
                alias_index=index, path=path)
        boundary = item["boundary"]
        if boundary not in BOUNDARY_MODES:
            raise LexiconDictionaryError(
                line_number,
                f"unknown boundary mode {boundary!r}; expected one of "
                f"{', '.join(BOUNDARY_MODES)}",
                alias_index=index, path=path)
        folded, _mapping = fold_with_mapping(text)
        if not folded:
            raise LexiconDictionaryError(
                line_number,
                "alias folds to an empty comparison string",
                alias_index=index, path=path)
        aliases.append((index, text, boundary, folded))
    return line_number, term_id, priority, aliases


def iter_dictionary_records(
    handle: BinaryIO,
    *,
    path: Optional[str] = None,
    max_record_bytes: Optional[int] = None,
) -> Iterator[RecordEntry]:
    """Yield validated ``(line, term_id, priority, aliases)`` records.

    Lines are split on ``\\n`` (a trailing newline does not create an
    extra record; an interior blank line still fails).  The stream is
    consumed one physical line at a time — the whole dictionary is never
    held in Python objects.
    """
    line_number = 0
    while True:
        raw = handle.readline()
        if raw == b"":
            return
        line_number += 1
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if max_record_bytes is not None and len(raw) > max_record_bytes:
            raise LexiconDictionaryError(
                line_number,
                f"line is {len(raw)} bytes, exceeding the single-record "
                f"limit of {max_record_bytes} bytes", path=path)
        yield _validate_record(_parse_line(raw, line_number, path),
                               line_number, path)
