"""Formula evaluation with a dependency pre-check and a cell budget.

Two deliberately separate phases (as the documented subset requires):

1. :meth:`Evaluator.check_reachable` walks every formula that the requested
   output cells can *touch* -- including branches an ``IF`` would not select
   -- parses and validates every touched formula completely, and builds the
   dependency graph.  Any cycle is a job-level failure with the real address
   chain.  This is a pure graph walk: it computes no values, so lazy branch
   selection in phase 2 can neither hide a cycle from it nor make it trip
   over an unselected division by zero.
2. :meth:`Evaluator.value_of` computes values lazily: ``IF`` evaluates only
   its condition and the selected branch, ``IFERROR`` only its second
   argument when the first one errored.

Cells are memoized for the whole run, so a dependency shared by many output
rows is evaluated exactly once.  Both the graph walk and value computation
use explicit work lists and generators driven by an outer loop
(:func:`_drive_work`), never Python call-stack recursion: a 5000-step
reference chain needs no recursion-limit tuning.  Range dependencies and
range function arguments are walked cell by cell from the rectangle, never
expanded into an address list.  Every reference resolution and every range
cell visit is charged to a configurable budget; exhaustion raises
:class:`WorkbookBudgetExceeded` and never produces a truncated success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterator, List, Optional, Tuple

from .errors import WorkbookBudgetExceeded, WorkbookCycle, WorkbookFormulaError
from .formula import (
    BadReference,
    Binary,
    CellRef,
    FunctionCall,
    Literal,
    RangeRef,
    Unary,
    iter_references,
    parse_formula,
    validate_ast,
)
from .ooxml import RawCell, Workbook, Worksheet
from .refs import address, in_grid, qualified
from .values import (
    ARITHMETIC_CONTEXT,
    BLANK,
    Bool,
    FormulaError,
    Number,
    Scalar,
    Text,
    apply_binary,
    apply_unary,
    make_error,
)

CellKey = Tuple[str, int, int]   # (canonical sheet name, row, col)

_ACTIVE = 1
_DONE = 2

_GENERATOR_TYPE = type((lambda: (yield))())


class Budget:
    """Counts real workbook accesses and enforces the optional limit."""

    def __init__(self, limit: Optional[int]):
        self.limit = limit
        self.cell_accesses = 0
        self.range_cells_visited = 0
        self.formula_evaluations = 0

    def charge_access(self, label: str) -> None:
        self.cell_accesses += 1
        if self.limit is not None and self.cell_accesses > self.limit:
            raise WorkbookBudgetExceeded(self.limit, label)

    def charge_range_cell(self, label: str) -> None:
        self.range_cells_visited += 1
        self.charge_access(label)

    def as_dict(self) -> dict:
        return {
            "cell_accesses": self.cell_accesses,
            "range_cells_visited": self.range_cells_visited,
            "formula_evaluations": self.formula_evaluations,
        }


def _drive(generator):
    """Drive a nested-generator tree iteratively and return its final value.

    A generator yields another generator to have it evaluated and receives
    that generator's return value as the value of the ``yield`` expression.
    An explicit frame list keeps deep chains off the Python call stack.
    """
    stack = [[generator, None]]
    while stack:
        frame = stack[-1]
        try:
            sub = frame[0].send(frame[1])
        except StopIteration as stop:
            value = stop.value
            stack.pop()
            if not stack:
                return value
            stack[-1][1] = value
            continue
        if not isinstance(sub, _GENERATOR_TYPE):
            raise TypeError(
                "formula evaluation yielded a non-generator; evaluator bug"
            )
        stack.append([sub, None])
    return None


@dataclass
class TouchReport:
    """What the reference check actually touched in the workbook."""

    formula_cells: int = 0
    constant_cells: int = 0
    blank_cells: int = 0
    merged_covered_cells: int = 0
    by_sheet: Dict[str, Dict[str, int]] = field(default_factory=dict)
    missing_sheets: Dict[str, List[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "formula_cells": self.formula_cells,
            "constant_cells": self.constant_cells,
            "blank_cells": self.blank_cells,
            "merged_covered_cells": self.merged_covered_cells,
            "by_sheet": self.by_sheet,
            "missing_sheets": {
                key: sorted(value) for key, value in self.missing_sheets.items()
            },
        }


class Evaluator:
    """Evaluates selected cells of one workbook under the documented subset."""

    def __init__(self, workbook: Workbook, *, budget: Optional[int] = None):
        self.workbook = workbook
        self.budget = Budget(budget)
        self._sheet_by_name = {sheet.name: sheet for sheet in workbook.sheets}
        self._casefold = {name.lower(): name for name in self._sheet_by_name}
        self._values: Dict[CellKey, Scalar] = {}
        self._asts: Dict[CellKey, object] = {}
        #: Labels of formulas that reference a sheet the workbook lacks.
        self.missing_sheet_references: Dict[str, List[str]] = {}

    # -- facts ----------------------------------------------------------
    @property
    def formula_cells_parsed(self) -> int:
        """Number of distinct formula cells whose AST was parsed/validated."""
        return len(self._asts)

    def label(self, key: CellKey) -> str:
        return qualified(key[0], key[1], key[2])

    def has_formula(self, key: CellKey) -> bool:
        sheet = self._sheet_by_name[key[0]]
        if not in_grid(key[1], key[2]) or (key[1], key[2]) in sheet.covered:
            return False
        raw = sheet.cells.get((key[1], key[2]))
        return raw is not None and raw.formula is not None

    def cell_kind(self, key: CellKey) -> str:
        """``formula`` / ``constant`` / ``blank`` / ``merged`` for reporting."""
        sheet = self._sheet_by_name[key[0]]
        if not in_grid(key[1], key[2]):
            return "blank"
        if (key[1], key[2]) in sheet.covered:
            return "merged"
        raw = sheet.cells.get((key[1], key[2]))
        if raw is None:
            return "blank"
        if raw.formula is not None:
            return "formula"
        return "constant"

    # -- formula ASTs ---------------------------------------------------
    def ast_for(self, key: CellKey):
        cached = self._asts.get(key)
        if cached is not None:
            return cached
        sheet = self._sheet_by_name[key[0]]
        raw = sheet.cells.get((key[1], key[2]))
        if raw is None or raw.formula is None:  # pragma: no cover - guarded
            raise WorkbookFormulaError(
                "coordinate has no formula", key[0], address(key[1], key[2])
            )
        if raw.shared_ast is not None:
            # Members carry an AST translated from the master; the address
            # used in diagnostics is the member's own cell.
            ast = raw.shared_ast
        else:
            ast = parse_formula(
                raw.formula, sheet=sheet.name, address=address(raw.row, raw.col)
            )
        validate_ast(ast, sheet=sheet.name, address=address(raw.row, raw.col))
        self._asts[key] = ast
        return ast

    # ------------------------------------------------------------------
    # phase 1: reference check, cycle detection and touch accounting
    # ------------------------------------------------------------------
    def check_reachable(self, roots) -> TouchReport:
        """Validate every reachable formula and reject dependency cycles.

        ``roots`` may be any iterable of selected coordinates; it is consumed
        once, so a large band does not have to be materialised as a list.
        """
        report = TouchReport()
        state: Dict[CellKey, int] = {}
        path: List[CellKey] = []
        position: Dict[CellKey, int] = {}
        for root in roots:
            if not in_grid(root[1], root[2]):
                continue
            if self.cell_kind(root) == "merged":
                continue
            if root in state:
                continue
            self._touch(root, report)
            if not self.has_formula(root):
                state[root] = _DONE
                continue
            state[root] = _ACTIVE
            position[root] = len(path)
            path.append(root)
            stack = [(root, self._edges(root))]
            while stack:
                key, edges = stack[-1]
                dep = next(edges, _NO_DEP)
                if dep is _NO_DEP:
                    stack.pop()
                    state[key] = _DONE
                    position.pop(key, None)
                    path.pop()
                    continue
                flag = state.get(dep)
                if flag == _ACTIVE:
                    chain = [self.label(item) for item in path[position[dep]:]]
                    chain.append(self.label(dep))
                    raise WorkbookCycle(chain)
                if flag == _DONE:
                    continue
                self._touch(dep, report)
                if self.has_formula(dep):
                    state[dep] = _ACTIVE
                    position[dep] = len(path)
                    path.append(dep)
                    stack.append((dep, self._edges(dep)))
                else:
                    state[dep] = _DONE
        report.missing_sheets = {
            key: list(value)
            for key, value in self.missing_sheet_references.items()
        }
        return report

    def _touch(self, key: CellKey, report: TouchReport) -> None:
        kind = self.cell_kind(key)
        if kind == "merged":
            report.merged_covered_cells += 1
        elif kind == "formula":
            report.formula_cells += 1
        elif kind == "constant":
            report.constant_cells += 1
        else:
            report.blank_cells += 1
        entry = report.by_sheet.setdefault(
            key[0],
            {"formula_cells": 0, "constant_cells": 0, "blank_cells": 0,
             "merged_covered_cells": 0},
        )
        bucket = {
            "formula": "formula_cells",
            "constant": "constant_cells",
            "blank": "blank_cells",
            "merged": "merged_covered_cells",
        }[kind]
        entry[bucket] += 1

    def _edges(self, key: CellKey) -> Iterator[CellKey]:
        """Dependency coordinates of one formula, in formula order."""
        ast = self.ast_for(key)
        current_sheet = key[0]
        sheet_name, _label, _key = self._context(key)
        for ref in iter_references(ast):
            if isinstance(ref, BadReference):
                continue
            target = ref.sheet if ref.sheet is not None else current_sheet
            canonical = self._casefold.get(target.lower())
            if canonical is None:
                names = self.missing_sheet_references.setdefault(
                    self.label(key), []
                )
                if target not in names:
                    names.append(target)
                continue
            if isinstance(ref, CellRef):
                self.budget.charge_access(qualified(canonical, ref.row, ref.col))
                yield (canonical, ref.row, ref.col)
            else:
                for row in range(ref.top, ref.bottom + 1):
                    for col in range(ref.left, ref.right + 1):
                        self.budget.charge_range_cell(
                            qualified(canonical, row, col)
                        )
                        yield (canonical, row, col)

    def _context(self, key: CellKey):
        """``(sheet, qualified_label, key)`` for error chains and messages.

        Error-chain labels are always workbook-qualified (``Sheet!A1``) so a
        cross-sheet dependency is unambiguous in the delivered JSONL.
        """
        return key[0], qualified(key[0], key[1], key[2]), key

    # ------------------------------------------------------------------
    # phase 2: lazy value computation
    # ------------------------------------------------------------------
    def value_of(self, key: CellKey) -> Scalar:
        """Memoized value of one coordinate, charged as one access."""
        self.budget.charge_access(self.label(key))
        return _drive(self._eval_coordinate(key))

    def _eval_coordinate(self, key: CellKey):
        if key in self._values:
            return self._values[key]
        sheet = self._sheet_by_name[key[0]]
        if not in_grid(key[1], key[2]) or (key[1], key[2]) in sheet.covered:
            self._values[key] = BLANK
            return BLANK
        raw = sheet.cells.get((key[1], key[2]))
        if raw is None:
            self._values[key] = BLANK
            return BLANK
        if raw.formula is None:
            self._values[key] = raw.value
            return raw.value
        return (yield self._eval_formula(key, raw))

    def _eval_formula(self, key: CellKey, raw: RawCell):
        ast = self.ast_for(key)
        self.budget.formula_evaluations += 1
        sheet_name, label, _ = self._context(key)
        value = yield self._eval_node(ast, (sheet_name, label))
        self._values[key] = value
        return value

    def _eval_node(self, node, context):
        """Generator for one AST node; errors leaving a reference are prefixed."""
        sheet_name, label = context
        if isinstance(node, Literal):
            kind, payload = node.value
            if kind == "number":
                return Number(payload)
            if kind == "text":
                return Text(payload)
            if kind == "bool":
                return Bool(payload)
            raise WorkbookFormulaError(  # pragma: no cover - parser rejects
                f"literal kind {kind!r} is not supported", sheet_name, label
            )
        if isinstance(node, BadReference):
            return make_error("#REF!", node.reason, label)
        if isinstance(node, CellRef):
            target_sheet = node.sheet if node.sheet is not None else sheet_name
            canonical = self._casefold.get(target_sheet.lower())
            if canonical is None:
                return make_error(
                    "#REF!",
                    f"sheet {target_sheet!r} does not exist in this workbook",
                    label,
                )
            value = yield self._eval_coordinate((canonical, node.row, node.col))
            if isinstance(value, FormulaError):
                return value.with_prefix(label)
            return value
        if isinstance(node, Unary):
            operand = yield self._eval_node(node.operand, context)
            return apply_unary(node.op, operand, label)
        if isinstance(node, Binary):
            left = yield self._eval_node(node.left, context)
            right = yield self._eval_node(node.right, context)
            return apply_binary(node.op, left, right, label)
        if isinstance(node, FunctionCall):
            return (yield self._eval_function(node, context))
        raise WorkbookFormulaError(  # pragma: no cover - defensive
            f"unsupported formula node {type(node).__name__}", sheet_name, label
        )

    # -- functions ------------------------------------------------------
    def _eval_function(self, call: FunctionCall, context):
        if call.name == "IF":
            return (yield self._eval_if(call, context))
        if call.name == "IFERROR":
            return (yield self._eval_iferror(call, context))
        return (yield self._eval_aggregate(call, context))

    def _eval_if(self, call: FunctionCall, context):
        label = context[1]
        condition = yield self._eval_node(call.args[0], context)
        if isinstance(condition, FormulaError):
            return condition
        if not isinstance(condition, Bool):
            return make_error(
                "#VALUE!", "IF condition must be TRUE or FALSE", label
            )
        if condition.value:
            return (yield self._eval_node(call.args[1], context))
        if len(call.args) == 3:
            return (yield self._eval_node(call.args[2], context))
        return Bool(False)

    def _eval_iferror(self, call: FunctionCall, context):
        first = yield self._eval_node(call.args[0], context)
        if isinstance(first, FormulaError):
            return (yield self._eval_node(call.args[1], context))
        return first

    def _eval_aggregate(self, call: FunctionCall, context):
        """SUM/MIN/MAX/COUNT with scalar arguments or rectangular ranges.

        Only numbers contribute.  Blank, text and bool values are ignored
        (directly or inside a range); the first error propagates in argument
        order and row-major order within a range.  With no numbers, COUNT is
        0 and SUM/MIN/MAX are 0 by this subset's convention.
        """
        sheet_name, label = context
        total = Decimal(0)
        count = 0
        minimum = None
        maximum = None
        for argument in call.args:
            if isinstance(argument, BadReference):
                return make_error("#REF!", argument.reason, label)
            if isinstance(argument, RangeRef):
                target_sheet = (argument.sheet if argument.sheet is not None
                                else sheet_name)
                canonical = self._casefold.get(target_sheet.lower())
                if canonical is None:
                    return make_error(
                        "#REF!",
                        f"sheet {target_sheet!r} does not exist in this workbook",
                        label,
                    )
                for row in range(argument.top, argument.bottom + 1):
                    for col in range(argument.left, argument.right + 1):
                        self.budget.charge_range_cell(
                            qualified(canonical, row, col)
                        )
                        value = yield self._eval_coordinate(
                            (canonical, row, col)
                        )
                        if isinstance(value, FormulaError):
                            return value.with_prefix(label)
                        if isinstance(value, Number):
                            number = value.value
                            total = ARITHMETIC_CONTEXT.add(total, number)
                            count += 1
                            if minimum is None or number < minimum:
                                minimum = number
                            if maximum is None or number > maximum:
                                maximum = number
            else:
                value = yield self._eval_node(argument, context)
                if isinstance(value, FormulaError):
                    return value
                if isinstance(value, Number):
                    number = value.value
                    total = ARITHMETIC_CONTEXT.add(total, number)
                    count += 1
                    if minimum is None or number < minimum:
                        minimum = number
                    if maximum is None or number > maximum:
                        maximum = number
        if call.name == "COUNT":
            return Number(Decimal(count))
        if count == 0:
            return Number(Decimal(0))
        if call.name == "SUM":
            return Number(total)
        if call.name == "MIN":
            return Number(minimum)
        if call.name == "MAX":
            return Number(maximum)
        raise WorkbookFormulaError(  # pragma: no cover - parser restricts
            f"function {call.name} is not supported", sheet_name, label
        )


class _NoDep:
    pass


_NO_DEP = _NoDep()
