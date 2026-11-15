"""Exception types for the offline workbook recalculation entry point.

Every failure raised by :mod:`distagg.workbook` derives from
:class:`WorkbookError`.  The classes separate the four things an operator has
to act on differently:

* :class:`RecipeError`       -- the recipe document is malformed or invalid;
* :class:`WorkbookPackageError` -- the XLSX container is missing a part,
  has broken XML, escapes the package through a relationship, or contains
  duplicate cell coordinates;
* :class:`WorkbookFormulaError` -- a formula hit a value error, a
  reference problem, a cycle or an unsupported construct;
* :class:`WorkbookBudgetExceeded` -- the configured cell-access budget ran
  out (a job-level failure, never a silent truncation).

:class:`WorkbookStrictStop` is raised in ``strict`` mode when a computed
selected field produced an error; it carries the rows already delivered so
the caller can mark the handover as incomplete.
"""

from __future__ import annotations


class WorkbookError(Exception):
    """Base class for workbook-recalculation failures."""

    code = "workbook_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class RecipeError(WorkbookError):
    """The recipe document is malformed or semantically invalid."""

    code = "recipe_error"

    def __init__(self, message: str, path: str = None):
        if path is not None:
            message = f"{path}: {message}"
        super().__init__(message)
        self.path = path

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.path is not None:
            data["path"] = self.path
        return data


class WorkbookPackageError(WorkbookError):
    """The XLSX container or one of its XML parts is unusable."""

    code = "workbook_package_error"

    def __init__(self, message: str, part: str = None):
        if part is not None:
            message = f"{part}: {message}"
        super().__init__(message)
        self.part = part

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.part is not None:
            data["part"] = self.part
        return data


class WorkbookFormulaError(WorkbookError):
    """A formula references something the workbook cannot provide."""

    code = "workbook_formula_error"

    def __init__(self, message: str, sheet: str = None, address: str = None):
        location = ""
        if sheet is not None and address is not None:
            location = f"{sheet}!{address}: "
        elif address is not None:
            location = f"{address}: "
        super().__init__(location + message)
        self.sheet = sheet
        self.address = address

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.sheet is not None:
            data["sheet"] = self.sheet
        if self.address is not None:
            data["address"] = self.address
        return data


class WorkbookUnsupported(WorkbookError):
    """A formula uses a construct outside the documented subset."""

    code = "workbook_unsupported"

    def __init__(self, message: str, sheet: str = None, address: str = None,
                 construct: str = None):
        location = ""
        if sheet is not None and address is not None:
            location = f"{sheet}!{address}: "
        elif address is not None:
            location = f"{address}: "
        text = location + message
        if construct is not None:
            text += f" (unsupported: {construct})"
        super().__init__(text)
        self.sheet = sheet
        self.address = address
        self.construct = construct

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.sheet is not None:
            data["sheet"] = self.sheet
        if self.address is not None:
            data["address"] = self.address
        if self.construct is not None:
            data["construct"] = self.construct
        return data


class WorkbookCycle(WorkbookError):
    """The dependency graph among touched formulas contains a cycle."""

    code = "workbook_cycle"

    def __init__(self, chain):
        self.chain = list(chain)
        text = " -> ".join(self.chain)
        super().__init__(f"circular reference: {text}")
        self.sheet = None
        self.address = None

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["chain"] = self.chain
        return data


class WorkbookBudgetExceeded(WorkbookError):
    """The configured cell-access budget was exhausted."""

    code = "workbook_budget_exceeded"

    def __init__(self, budget: int, cell: str = None):
        self.budget = budget
        self.cell = cell
        text = f"cell access budget of {budget} exceeded"
        if cell is not None:
            text += f" while touching {cell}"
        super().__init__(text)

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["budget"] = self.budget
        if self.cell is not None:
            data["cell"] = self.cell
        return data


class WorkbookStrictStop(WorkbookError):
    """Strict mode stopped at the first delivered row with an error field.

    ``rows_written`` is the number of completed JSONL rows already on disk;
    they are *not* a complete handover and the summary says so.
    """

    code = "workbook_strict_stop"

    def __init__(self, message: str, sheet: str, row: int, address: str,
                 error_type: str, rows_written: int,
                 partial_path: str = None):
        super().__init__(message)
        self.sheet = sheet
        self.row = row
        self.address = address
        self.error_type = error_type
        self.rows_written = rows_written
        self.partial_path = partial_path

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "sheet": self.sheet,
            "row": self.row,
            "address": self.address,
            "error_type": self.error_type,
            "rows_written": self.rows_written,
            "complete": False,
        })
        if self.partial_path is not None:
            data["partial_output"] = self.partial_path
            data["partial_note"] = (
                "these rows stopped before the failing row and are NOT a "
                "complete handover"
            )
        return data
