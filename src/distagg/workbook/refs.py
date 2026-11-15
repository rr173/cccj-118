"""A1 reference geometry shared by the package reader and the evaluator."""

from __future__ import annotations

import re

MAX_COLUMN = 16384          # XFD
MAX_ROW = 1048576

_CELL_RE = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]+)\Z")
_SHEET_REF_RE = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]+)\Z")


class RefError(ValueError):
    """A reference is syntactically a cell but lies outside the grid."""


def column_to_index(letters: str) -> int:
    """``"A"`` -> 1, ``"XFD"`` -> 16384; no case dependence."""
    if not letters or not letters.isascii() or not letters.isalpha():
        raise RefError(f"invalid column letters {letters!r}")
    value = 0
    for char in letters.upper():
        value = value * 26 + (ord(char) - 64)
    return value


def index_to_column(index: int) -> str:
    if index < 1 or index > MAX_COLUMN:
        raise RefError(f"column index {index} outside 1..{MAX_COLUMN}")
    out = []
    while index:
        index, rem = divmod(index - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


def parse_cell_text(text: str):
    """Parse an exact A1 cell token (``$`` markers allowed).

    Returns ``(row, col, abs_row, abs_col)`` or raises :class:`RefError`.
    """
    match = _CELL_RE.match(text)
    if not match:
        raise RefError(f"not a cell reference: {text!r}")
    letters, digits = match.groups()
    col = column_to_index(letters)
    row = int(digits)
    abs_col = text.startswith("$")
    abs_row = text[text.index(letters) + len(letters)] == "$"
    return row, col, abs_row, abs_col


def address(row: int, col: int) -> str:
    return f"{index_to_column(col)}{row}"


def qualified(sheet: str, row: int, col: int) -> str:
    return f"{quote_sheet(sheet)}!{address(row, col)}"


def quote_sheet(name: str) -> str:
    """Quote a sheet name for display the way Excel requires when needed."""
    if not name:
        return "''"
    unsafe = set(" !'#$%&()*+,-./:;<=>?@[\\]^`{}|~\"")
    if (
        name[0].isalpha()
        and "_" not in (name[0],)
        and not any(ch in unsafe for ch in name)
        and not _looks_like_ref(name)
    ):
        return name
    return "'" + name.replace("'", "''") + "'"


def _looks_like_ref(name: str) -> bool:
    return _CELL_RE.match(name) is not None


def in_grid(row: int, col: int) -> bool:
    return 1 <= row <= MAX_ROW and 1 <= col <= MAX_COLUMN


def normalize_sheet_name(name: str) -> str:
    """Excel-style case-insensitive sheet lookup key (Unicode lower case)."""
    return name.lower()
