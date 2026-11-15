"""Recompute supplier cost workbooks offline, without an external calculator.

The public entry point is :func:`run_recalc_job`.  It reads one ``.xlsx``
file and one recipe document (sheets, row bands and an ordered
``field -> column`` mapping), recomputes every selected cell from the raw
OOXML value or formula, and writes one JSONL handover row per selected data
row.  Formulas are evaluated by code in this package (``formula.py`` and
``evaluator.py``); the package never starts Excel/LibreOffice, never asks an
external calculation service, never trusts the cached ``<v>`` results stored
in the file and never writes back to the source workbook.

See ``docs/WORKBOOK.md`` for the supported formula subset and its exact
semantics.
"""

from .errors import (
    WorkbookError,
    RecipeError,
    WorkbookPackageError,
    WorkbookFormulaError,
    WorkbookUnsupported,
    WorkbookCycle,
    WorkbookBudgetExceeded,
    WorkbookStrictStop,
)
from .recipe import Recipe, SheetSelection, load_recipe, parse_recipe
from .engine import (
    RecalcConfig,
    RecalcStats,
    CheckStats,
    run_recalc_job,
    check_workbook,
)
from .values import Scalar, Number, Text, Bool, FormulaError

__all__ = [
    "WorkbookError",
    "RecipeError",
    "WorkbookPackageError",
    "WorkbookFormulaError",
    "WorkbookUnsupported",
    "WorkbookCycle",
    "WorkbookBudgetExceeded",
    "WorkbookStrictStop",
    "Recipe",
    "SheetSelection",
    "load_recipe",
    "parse_recipe",
    "RecalcConfig",
    "RecalcStats",
    "CheckStats",
    "run_recalc_job",
    "check_workbook",
    "Scalar",
    "Number",
    "Text",
    "Bool",
    "FormulaError",
]
