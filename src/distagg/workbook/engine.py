"""Job orchestration: recipe + workbook -> verifiable JSONL handover rows.

One :func:`run_recalc_job` call:

1. loads and validates the recipe (sheet existence, row bands, field names
   and columns) **before** any output is opened;
2. loads the XLSX package through its real relationship graph;
3. computes the selected coordinates, reference-checks every touched
   formula (cycles, unsupported constructs, missing sheets) and only then
   evaluates them lazily with a shared memo and a cell-access budget;
4. writes one JSON object per data row to a private temporary file and
   publishes it atomically only when the whole job is acceptable.

Delivery modes:

``strict`` (default)
    At the first selected cell whose *computed* value is an error, the job
    stops with a non-zero exit.  The temporary output is discarded, so no
    incomplete file is ever left at the destination; the JSON error object
    carries the sheet, row, address, error type and the partial-row count.
``annotate``
    Errors stay in their fields (``status: "error"`` with ``type``,
    ``message`` and the reference ``chain``); the job succeeds and the
    summary separates ``ok``/``blank``/``error`` field counts.

Faults that are not about one selected field -- a broken package, a cycle,
an unsupported construct, budget exhaustion -- are job-level failures in
both modes.  They are never disguised as a blank cell.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .errors import (
    RecipeError,
    WorkbookBudgetExceeded,
    WorkbookCycle,
    WorkbookError,
    WorkbookFormulaError,
    WorkbookPackageError,
    WorkbookStrictStop,
    WorkbookUnsupported,
)
from .evaluator import CellKey, Evaluator, TouchReport
from .ooxml import RawCell, Workbook, Worksheet, load_workbook, shared_formula_source
from .recipe import Recipe, SheetSelection, load_recipe, resolve_sheet
from .refs import address
from .values import (
    Bool,
    FormulaError,
    Number,
    Scalar,
    Text,
    decimal_text,
)

MODE_STRICT = "strict"
MODE_ANNOTATE = "annotate"


@dataclass(frozen=True)
class RecalcConfig:
    """Resource and policy knobs for one recalculation job.

    ``cell_budget`` bounds the number of charged cell accesses the reference
    check and the evaluation together may perform (``None`` = unlimited).
    ``max_formula_cells`` bounds how many formula cells one job may parse,
    as an additional guard against runaway workbooks.
    """

    mode: str = MODE_STRICT
    cell_budget: Optional[int] = None
    max_formula_cells: Optional[int] = None

    def __post_init__(self) -> None:
        if self.mode not in (MODE_STRICT, MODE_ANNOTATE):
            raise WorkbookError(
                f"mode must be {MODE_STRICT!r} or {MODE_ANNOTATE!r}, "
                f"got {self.mode!r}"
            )
        if self.cell_budget is not None:
            if isinstance(self.cell_budget, bool) or \
                    not isinstance(self.cell_budget, int) or self.cell_budget < 1:
                raise WorkbookError(
                    f"cell_budget must be a positive integer or None, "
                    f"got {self.cell_budget!r}"
                )
        if self.max_formula_cells is not None:
            if isinstance(self.max_formula_cells, bool) or \
                    not isinstance(self.max_formula_cells, int) or \
                    self.max_formula_cells < 1:
                raise WorkbookError(
                    "max_formula_cells must be a positive integer or None, "
                    f"got {self.max_formula_cells!r}"
                )


@dataclass
class RecalcStats:
    rows_written: int = 0
    fields: int = 0
    ok_fields: int = 0
    blank_fields: int = 0
    error_fields: int = 0
    selections: int = 0
    formula_cell_count: int = 0
    touch: Optional[TouchReport] = None
    budget: Optional[dict] = None
    mode: str = MODE_STRICT
    output_path: Optional[str] = None

    def as_dict(self) -> dict:
        payload = {
            "mode": self.mode,
            "rows_written": self.rows_written,
            "fields": self.fields,
            "ok_fields": self.ok_fields,
            "blank_fields": self.blank_fields,
            "error_fields": self.error_fields,
            "selections": self.selections,
            "formula_cells": self.formula_cell_count,
            "output_path": self.output_path,
            "complete": True,
        }
        if self.touch is not None:
            payload["touched"] = self.touch.as_dict()
        if self.budget is not None:
            payload["budget"] = self.budget
        return payload


@dataclass
class CheckStats:
    recipe: Recipe
    sheets: List[dict] = field(default_factory=list)
    selections: List[dict] = field(default_factory=list)
    touch: Optional[TouchReport] = None
    budget: Optional[dict] = None
    formula_cells_in_selection: int = 0
    constant_cells_in_selection: int = 0
    blank_cells_in_selection: int = 0

    def as_dict(self) -> dict:
        return {
            "sheets": self.sheets,
            "selections": self.selections,
            "formula_cells_in_selection": self.formula_cells_in_selection,
            "constant_cells_in_selection": self.constant_cells_in_selection,
            "blank_cells_in_selection": self.blank_cells_in_selection,
            "touched": self.touch.as_dict() if self.touch else None,
            "budget": self.budget,
            "note": (
                "Counts are per selected coordinate: a coordinate used by two "
                "fields is counted once, a formula cell outside the selection "
                "may still be parsed because it is a dependency."
            ),
        }


# ---------------------------------------------------------------------------
# selection helpers
# ---------------------------------------------------------------------------

def _check_sheets(workbook: Workbook, recipe: Recipe) -> List[SheetSelection]:
    names = [sheet.name for sheet in workbook.sheets]
    resolved = []
    for selection in recipe.selections:
        canonical = resolve_sheet(selection.sheet_name, names,
                                  where=f"recipe sheet {selection.sheet_name!r}")
        resolved.append(SheetSelection(
            canonical, selection.first_row, selection.last_row, selection.fields
        ))
    return resolved


def _selection_keys(selection: SheetSelection):
    """Iterate the selected coordinates lazily (no per-band address list)."""
    for row in range(selection.first_row, selection.last_row + 1):
        for field in selection.fields:
            yield (selection.sheet_name, row, field.column)


def _count_selection_kinds(workbook: Workbook, selection: SheetSelection,
                           evaluator: Evaluator) -> Tuple[int, int, int]:
    seen = set()
    formulas = constants = blanks = 0
    for (sheet, row, col) in _selection_keys(selection):
        if (row, col) in seen:
            continue
        seen.add((row, col))
        kind = evaluator.cell_kind((sheet, row, col))
        if kind == "formula":
            formulas += 1
        elif kind == "constant":
            constants += 1
        else:
            blanks += 1
    return formulas, constants, blanks


# ---------------------------------------------------------------------------
# field serialization
# ---------------------------------------------------------------------------

#: Maximum reference-chain entries embedded in one field.  A deeper chain is
#: cut and explicitly marked, so the handover never silently looks complete.
MAX_CHAIN_ENTRIES = 64


def _scalar_payload(value: Scalar) -> dict:
    if isinstance(value, FormulaError):
        chain = value.chain()
        payload = {
            "status": "error",
            "type": value.code,
            "message": value.message,
            "chain": chain[:MAX_CHAIN_ENTRIES],
            "chain_length": len(chain),
        }
        if len(chain) > MAX_CHAIN_ENTRIES:
            payload["chain_truncated"] = True
        return payload
    if value.kind == "blank":
        return {"status": "blank"}
    if isinstance(value, Number):
        return {"status": "ok", "value": decimal_text(value.value)}
    if isinstance(value, Text):
        return {"status": "ok", "value": value.value}
    if isinstance(value, Bool):
        return {"status": "ok", "value": value.value}
    raise WorkbookError(  # pragma: no cover - scalars are closed
        f"unknown scalar kind {value.kind!r}"
    )


def _cell_provenance(workbook: Workbook, raw: Optional[RawCell],
                     sheet: Worksheet, row: int, col: int) -> dict:
    """Original address plus literal or formula text, for one coordinate."""
    addr = address(row, col)
    if (row, col) in sheet.covered:
        anchor_row, anchor_col = sheet.covered[(row, col)]
        return {
            "address": addr,
            "origin": "merged-covered",
            "anchor": address(anchor_row, anchor_col),
        }
    if raw is None:
        return {"address": addr, "origin": "blank"}
    if raw.formula is not None:
        if raw.shared_index is None:
            return {
                "address": addr,
                "origin": "formula",
                "formula": "=" + raw.formula,
            }
        source = shared_formula_source(workbook, sheet.name, row, col)
        provenance = {
            "address": addr,
            "origin": "shared-formula",
            "shared_index": raw.shared_index,
        }
        if source is not None:
            master_address, translated, is_master = source
            provenance["master"] = master_address
            provenance["translated_formula"] = translated
            provenance["is_master"] = is_master
        if raw.formula.strip():
            provenance["master_formula"] = "=" + raw.formula
        return provenance
    if raw.kind == "blank":
        return {"address": addr, "origin": "blank"}
    return {
        "address": addr,
        "origin": raw.kind,
        "literal": _literal_text(raw),
    }


def _literal_text(raw: RawCell) -> str:
    value = raw.value
    if isinstance(value, Number):
        return decimal_text(value.value)
    if isinstance(value, Text):
        return value.value
    if isinstance(value, Bool):
        return "TRUE" if value.value else "FALSE"
    if isinstance(value, FormulaError):
        return value.code
    if value.kind == "blank":
        return ""
    return ""


def _build_row(workbook: Workbook, evaluator: Evaluator, sheet: Worksheet,
               selection: SheetSelection, row: int) -> dict:
    cells = {}
    ok = blank = errors = 0
    for field in selection.fields:
        raw = sheet.cells.get((row, field.column))
        key = (sheet.name, row, field.column)
        value = evaluator.value_of(key)
        payload = _scalar_payload(value)
        payload.update(
            _cell_provenance(workbook, raw, sheet, row, field.column)
        )
        cells[field.name] = payload
        if payload["status"] == "ok":
            ok += 1
        elif payload["status"] == "blank":
            blank += 1
        else:
            errors += 1
    return {
        "sheet": sheet.name,
        "row": row,
        "cells": cells,
        "_ok": ok,
        "_blank": blank,
        "_errors": errors,
    }


# ---------------------------------------------------------------------------
# output file handling
# ---------------------------------------------------------------------------

def _atomic_writer(path: str, *, partial_path: Optional[str] = None):
    """Open a private temp file next to ``path``.

    When ``partial_path`` is given, a strict-mode stop moves the rows that
    were completed *before* the failing row there, so "rows already emitted"
    can be inspected -- clearly named as a partial handover -- without ever
    leaving a file at the real destination that looks complete.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        raise WorkbookError(f"output directory {directory!r} does not exist")
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".distagg-workbook-",
        suffix=".jsonl", delete=False,
    )
    return handle


def _same_file(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.abspath(left) == os.path.abspath(right)


def _guard_destination(workbook_path: str, recipe_path: str,
                       output_path: str) -> None:
    for source in (workbook_path, recipe_path):
        if source and _same_file(source, output_path):
            raise WorkbookError(
                f"output path {output_path!r} would overwrite input "
                f"{source!r}; refusing"
            )


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------

def run_recalc_job(workbook_path: str, recipe_path: str, output_path: str,
                   config: Optional[RecalcConfig] = None) -> RecalcStats:
    """Recompute a recipe's selected cells and write JSONL handover rows."""
    config = config or RecalcConfig()
    _guard_destination(workbook_path, recipe_path, output_path)
    recipe = load_recipe(recipe_path)
    workbook = load_workbook(workbook_path)
    selections = _check_sheets(workbook, recipe)
    evaluator = Evaluator(workbook, budget=config.cell_budget)

    report = evaluator.check_reachable(
        key for selection in selections for key in _selection_keys(selection)
    )
    _enforce_formula_cap(evaluator, config)

    stats = RecalcStats(
        selections=len(selections), touch=report, mode=config.mode,
        output_path=os.path.abspath(output_path),
    )
    partial_path = (os.path.abspath(output_path) + ".partial"
                    if config.mode == MODE_STRICT else None)
    handle = _atomic_writer(output_path)
    try:
        rows_written = 0
        for selection in selections:
            sheet = workbook.by_name[selection.sheet_name]
            for row in range(selection.first_row, selection.last_row + 1):
                built = _build_row(
                    workbook, evaluator, sheet, selection, row
                )
                ok = built.pop("_ok")
                blank = built.pop("_blank")
                errors = built.pop("_errors")
                stats.fields += len(selection.fields)
                stats.ok_fields += ok
                stats.blank_fields += blank
                stats.error_fields += errors
                if errors and config.mode == MODE_STRICT:
                    first = _first_error_field(built)
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.close()
                    if rows_written:
                        os.replace(handle.name, partial_path)
                    else:
                        os.unlink(handle.name)
                    raise WorkbookStrictStop(
                        f"strict mode stopped at "
                        f"{sheet.name}!{address(row, first['column'])}: "
                        f"{first['type']}",
                        sheet.name, row, address(row, first["column"]),
                        first["type"], rows_written,
                        partial_path if rows_written else None,
                    )
                handle.write(json.dumps(built, ensure_ascii=False,
                                        sort_keys=True) + "\n")
                rows_written += 1
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, output_path)
    except WorkbookStrictStop:
        raise
    except BaseException:
        try:
            handle.close()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover - best effort
            pass
        raise
    stats.rows_written = rows_written
    stats.formula_cell_count = evaluator.formula_cells_parsed
    stats.budget = evaluator.budget.as_dict()
    return stats


def _first_error_field(built: dict):
    for name, payload in built["cells"].items():
        if payload["status"] == "error":
            # Recover the column from the address for the message.
            addr = payload["address"]
            letters = "".join(
                ch for ch in addr if ch.isalpha()
            )
            return {
                "field": name,
                "column": _letters_to_index(letters),
                "type": payload["type"],
            }
    raise WorkbookError("strict stop requested without an error field")


def _letters_to_index(letters: str) -> int:
    value = 0
    for ch in letters.upper():
        value = value * 26 + (ord(ch) - 64)
    return value


def _enforce_formula_cap(evaluator: Evaluator, config: RecalcConfig) -> None:
    if config.max_formula_cells is None:
        return
    count = evaluator.formula_cells_parsed
    if count > config.max_formula_cells:
        raise WorkbookError(
            f"workbook requires {count} formula evaluations/parses, exceeding "
            f"max_formula_cells={config.max_formula_cells}"
        )


def check_workbook(workbook_path: str, recipe_path: str,
                   config: Optional[RecalcConfig] = None) -> CheckStats:
    """Report the recipe's ranges, sheets and the cells actually touched.

    The check performs the same reference/cycle validation as the run, and
    reports how many *selected* coordinates are formulas, constants or blank,
    plus (via the touch report) how many formula cells were parsed as
    dependencies.  It writes no output.
    """
    config = config or RecalcConfig()
    recipe = load_recipe(recipe_path)
    workbook = load_workbook(workbook_path)
    selections = _check_sheets(workbook, recipe)
    evaluator = Evaluator(workbook, budget=config.cell_budget)
    report = evaluator.check_reachable(
        key for selection in selections for key in _selection_keys(selection)
    )
    _enforce_formula_cap(evaluator, config)

    check = CheckStats(recipe=recipe, touch=report,
                       budget=evaluator.budget.as_dict())
    check.sheets = [
        {
            "name": sheet.name,
            "part": sheet.part,
            "hidden": sheet.hidden,
            "cells": len(sheet.cells),
            "merged_ranges": len(sheet.merged),
            "max_row": sheet.max_row,
            "max_column": sheet.max_col,
        }
        for sheet in workbook.sheets
    ]
    for selection in selections:
        formulas, constants, blanks = _count_selection_kinds(
            workbook, selection, evaluator
        )
        check.formula_cells_in_selection += formulas
        check.constant_cells_in_selection += constants
        check.blank_cells_in_selection += blanks
        check.selections.append({
            "sheet": selection.sheet_name,
            "first_row": selection.first_row,
            "last_row": selection.last_row,
            "rows": selection.last_row - selection.first_row + 1,
            "fields": [
                {"name": field.name, "column": field.column_letters}
                for field in selection.fields
            ],
            "formula_cells": formulas,
            "constant_cells": constants,
            "blank_cells": blanks,
        })
    return check
