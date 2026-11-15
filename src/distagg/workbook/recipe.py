"""Recipe document: what to hand over from a supplier workbook.

A recipe is a small JSON object.  It never guesses structure: no header-row
detection, no automatic skipping of hidden rows or columns, no "column A is
the key" assumption.  Everything the handover touches is named explicitly.

```json
{
  "sheets": [
    {
      "sheet": "Costs",
      "first_row": 2,
      "last_row": 6,
      "fields": [
        {"name": "part", "column": "A"},
        {"name": "unit_cost", "column": "F"}
      ]
    }
  ]
}
```

* ``sheet`` is resolved against the workbook's real sheet names using
  Excel's case-insensitive matching.  A name that does not exist is an error.
* ``first_row`` / ``last_row`` are 1-based inclusive bounds, within
  ``1..1048576``.  Empty rows inside the band are still emitted.
* ``fields`` is an ordered non-empty list.  ``name`` is the target field name
  and must be unique within the sheet selection (and globally, because the
  JSONL row keys would otherwise collide ambiguously).  ``column`` is a
  column letter (case-insensitive, no ``$``, no row).
* Several sheet selections are emitted in recipe order; rows inside one
  selection in ascending row order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .errors import RecipeError
from .refs import MAX_COLUMN, MAX_ROW, RefError, column_to_index, index_to_column

_TOP_KEYS = {"sheets"}
_SHEET_KEYS = {"sheet", "first_row", "last_row", "fields"}
_FIELD_KEYS = {"name", "column"}


@dataclass(frozen=True)
class Field:
    name: str
    column: int          # 1-based column index
    column_letters: str  # canonical upper-case letters for reporting


@dataclass(frozen=True)
class SheetSelection:
    sheet_name: str
    first_row: int
    last_row: int
    fields: Tuple[Field, ...]

    def column_of(self, name: str) -> int:
        for field in self.fields:
            if field.name == name:
                return field.column
        raise KeyError(name)


@dataclass(frozen=True)
class Recipe:
    selections: Tuple[SheetSelection, ...]

    def field_names(self) -> List[str]:
        names = []
        for selection in self.selections:
            names.extend(field.name for field in selection.fields)
        return names


def _reject_duplicate_members(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise RecipeError(f"duplicate JSON member {key!r}")
        seen.add(key)
    return dict(pairs)


def load_recipe(path: str) -> Recipe:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle, object_pairs_hook=_reject_duplicate_members)
    except OSError as exc:
        raise RecipeError(f"cannot read recipe: {exc}", path) from exc
    except json.JSONDecodeError as exc:
        raise RecipeError(
            f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
            path,
        ) from exc
    except RecipeError as exc:
        raise RecipeError(exc.message, path) from exc
    return parse_recipe(document, path=path)


def parse_recipe(document, *, path: Optional[str] = None) -> Recipe:
    try:
        return _parse(document)
    except RecipeError as exc:
        if path is not None and exc.path is None:
            raise RecipeError(exc.message, path) from exc
        raise


def _parse(document) -> Recipe:
    if not isinstance(document, dict):
        raise RecipeError("recipe must be a JSON object")
    unknown = set(document) - _TOP_KEYS
    if unknown:
        raise RecipeError(
            "unknown recipe member(s): " + ", ".join(sorted(unknown))
        )
    if "sheets" not in document:
        raise RecipeError("recipe must contain 'sheets'")
    sheets = document["sheets"]
    if not isinstance(sheets, list) or not sheets:
        raise RecipeError("'sheets' must be a non-empty array")
    selections = []
    for index, entry in enumerate(sheets):
        selections.append(_parse_selection(entry, index))
    return Recipe(tuple(selections))


def resolve_sheet(recipe_sheet: str, canonical_names, *, where: str = "recipe"):
    """Match a recipe sheet name case-insensitively against the workbook.

    Excel sheet lookup is case-insensitive; the workbook itself must not
    contain two sheet names that only differ by case (that is rejected by
    the package reader).  Return the canonical name.
    """
    target = recipe_sheet.lower()
    matches = [name for name in canonical_names if name.lower() == target]
    if not matches:
        raise RecipeError(
            f"{where}: sheet {recipe_sheet!r} does not exist in the workbook"
        )
    if len(matches) > 1:  # pragma: no cover - package reader prevents this
        raise RecipeError(
            f"{where}: sheet name {recipe_sheet!r} is ambiguous after "
            "case folding"
        )
    return matches[0]


def _parse_selection(entry, index: int) -> SheetSelection:
    where = f"sheets[{index}]"
    if not isinstance(entry, dict):
        raise RecipeError(f"{where} must be an object")
    unknown = set(entry) - _SHEET_KEYS
    if unknown:
        raise RecipeError(
            f"{where}: unknown member(s): " + ", ".join(sorted(unknown))
        )
    for required in ("sheet", "first_row", "last_row", "fields"):
        if required not in entry:
            raise RecipeError(f"{where}: missing {required!r}")

    sheet_name = entry["sheet"]
    if not isinstance(sheet_name, str) or not sheet_name:
        raise RecipeError(f"{where}: 'sheet' must be a non-empty string")

    first_row = _parse_row(entry["first_row"], f"{where}.first_row")
    last_row = _parse_row(entry["last_row"], f"{where}.last_row")
    if first_row > last_row:
        raise RecipeError(
            f"{where}: first_row ({first_row}) is after last_row ({last_row})"
        )

    fields = entry["fields"]
    if not isinstance(fields, list) or not fields:
        raise RecipeError(f"{where}: 'fields' must be a non-empty array")
    parsed = []
    local_names = set()
    for field_index, field in enumerate(fields):
        location = f"{where}.fields[{field_index}]"
        if not isinstance(field, dict):
            raise RecipeError(f"{location} must be an object")
        unknown = set(field) - _FIELD_KEYS
        if unknown:
            raise RecipeError(
                f"{location}: unknown member(s): " + ", ".join(sorted(unknown))
            )
        if "name" not in field or "column" not in field:
            raise RecipeError(f"{location}: both 'name' and 'column' are required")
        name = field["name"]
        if not isinstance(name, str) or not name:
            raise RecipeError(f"{location}: 'name' must be a non-empty string")
        if name in local_names:
            # A repeated target name inside one mapping would silently drop a
            # column from every output row, so it is rejected up front.
            raise RecipeError(
                f"{location}: duplicate target field {name!r} in this "
                "selection's mapping"
            )
        column = _parse_column(field["column"], f"{location}.column")
        local_names.add(name)
        parsed.append(Field(name, column, index_to_column(column)))
    return SheetSelection(sheet_name, first_row, last_row, tuple(parsed))


def _parse_row(value, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeError(f"{location} must be an integer, got {value!r}")
    if not (1 <= value <= MAX_ROW):
        raise RecipeError(
            f"{location} must be between 1 and {MAX_ROW}, got {value}"
        )
    return value


def _parse_column(value, location: str) -> int:
    if not isinstance(value, str) or not value:
        raise RecipeError(f"{location} must be a column letter string")
    text = value
    if text.startswith("$"):
        raise RecipeError(f"{location} must not contain '$': {value!r}")
    if not text.isalpha() or not text.isascii() or len(text) > 3:
        raise RecipeError(
            f"{location} is not a plain column letter (A..XFD): {value!r}"
        )
    try:
        column = column_to_index(text)
    except RefError as exc:
        raise RecipeError(f"{location}: {exc}") from exc
    if column > MAX_COLUMN:
        raise RecipeError(
            f"{location} is beyond the last column XFD: {value!r}"
        )
    return column
