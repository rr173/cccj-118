"""Scalar values and exact decimal arithmetic for workbook recalculation.

Every number that enters the evaluator is a :class:`decimal.Decimal` built
directly from the XML text; no IEEE-754 ``float`` is ever created on the path
from a cell to a delivered value.  Arithmetic uses one fixed context --
precision 34, ``ROUND_HALF_EVEN`` -- which is the documented recalculation
caliber of this package (see ``docs/WORKBOOK.md``).  Values are serialized as
plain decimal strings that parse back to the identical ``Decimal``.

The scalar universe is deliberately small:

======================  =================================================
kind                    meaning
======================  =================================================
``number``              a ``Decimal`` inside the 34-digit context
``text``                a Python ``str`` (code points, no trimming)
``bool``                ``True`` / ``False``
``blank``               an empty cell (distinct from the empty string)
``error``               a spreadsheet error value such as ``#DIV/0!``
======================  =================================================
"""

from __future__ import annotations

import decimal
from decimal import Decimal, ROUND_HALF_EVEN

#: Precision and rounding used for every arithmetic operation.
PRECISION = 34

ARITHMETIC_CONTEXT = decimal.Context(
    prec=PRECISION,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[],
)

#: Error literals that may appear in an OOXML ``t="e"`` cell.
KNOWN_ERROR_CODES = frozenset({
    "#NULL!",
    "#DIV/0!",
    "#VALUE!",
    "#REF!",
    "#NAME?",
    "#NUM!",
    "#N/A",
    "#GETTING_DATA",
})


class Scalar:
    """Base class; ``kind`` is one of number/text/bool/blank/error."""

    __slots__ = ()
    kind = ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self!s}>"


class Number(Scalar):
    __slots__ = ("value",)
    kind = "number"

    def __init__(self, value: Decimal):
        self.value = value

    def __str__(self) -> str:
        return decimal_text(self.value)



class Text(Scalar):
    __slots__ = ("value",)
    kind = "text"

    def __init__(self, value: str):
        self.value = value

    def __str__(self) -> str:
        return self.value


class Bool(Scalar):
    __slots__ = ("value",)
    kind = "bool"

    def __init__(self, value: bool):
        self.value = bool(value)

    def __str__(self) -> str:
        return "TRUE" if self.value else "FALSE"


class _Blank(Scalar):
    __slots__ = ()
    kind = "blank"

    def __str__(self) -> str:
        return ""


BLANK = _Blank()


class FormulaError(Scalar):
    """A spreadsheet error value carrying its reference chain.

    ``chain`` is a linked list ``(label, parent)`` so that a deep dependency
    chain can prepend itself in O(1) instead of copying a tuple per level.
    Labels are full cell addresses such as ``'Costs'!F4``; the innermost
    label is the cell where the error was created.
    """

    __slots__ = ("code", "message", "_link")
    kind = "error"

    def __init__(self, code: str, message: str = "", link=None):
        self.code = code
        self.message = message
        self._link = link

    def with_prefix(self, label: str) -> "FormulaError":
        # ``chain()`` lists the innermost origin first, then each referencing
        # cell outward; the label this error is *seen from* goes at the end.
        # The linked list stores prefix-first order, so chain() reverses it.
        return FormulaError(self.code, self.message, (label, self._link))

    def chain(self) -> list:
        """Reference chain from where the error arose outwards.

        Example: an error created at ``S!A2`` and returned through ``S!B1``
        becomes ``["S!A2", "S!B1"]`` -- the first entry is the origin, the
        last is the cell whose value the handover row shows.
        """
        out = []
        node = self._link
        while node is not None:
            out.append(node[0])
            node = node[1]
        out.reverse()
        return out

    def origin(self):
        node = self._link
        if node is None:
            return None
        while node[1] is not None:
            node = node[1]
        return node[0]

    def __str__(self) -> str:
        return self.code


def make_error(code: str, message: str, label: str) -> FormulaError:
    """Create an error value whose chain starts at ``label``."""
    return FormulaError(code, message, (label, None))


def decimal_text(value: Decimal) -> str:
    """Serialize a finite ``Decimal`` exactly, using plain decimal notation.

    ``format(value, 'f')`` never goes through a binary float, keeps every
    significant (and trailing) digit, and ``Decimal(text)`` restores the
    identical value.  Zero is canonicalised so a negative zero produced by
    e.g. ``0 * -1`` is reported as ``"0"`` (as a spreadsheet displays it).
    """
    if value.is_zero():
        return "0"
    return format(value, "f")


def parse_decimal_literal(text: str) -> Decimal:
    """Build a ``Decimal`` from XML/formula text without touching ``float``."""
    if not isinstance(text, str) or text == "":
        raise ValueError("empty numeric literal")
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError(f"non-finite numeric literal {text!r}")
    return value


def is_error(value: Scalar) -> bool:
    return isinstance(value, FormulaError)


def is_number(value: Scalar) -> bool:
    return isinstance(value, Number)


# ---------------------------------------------------------------------------
# arithmetic
# ---------------------------------------------------------------------------

def _finite(value: Decimal) -> bool:
    return value.is_finite()


def _guard(result: Decimal, label: str) -> Scalar:
    if not _finite(result):
        return make_error("#NUM!", "numeric result is not finite", label)
    return Number(result)


def numeric_operand(value: Scalar, label: str):
    """Return ``(Decimal, None)`` or ``(None, error)`` for arithmetic.

    ``blank`` acts as 0; text and bool are ``#VALUE!``; an existing error is
    returned unchanged (the caller prepends its own label when needed).
    """
    if isinstance(value, Number):
        return value.value, None
    if value.kind == "blank":
        return Decimal(0), None
    if isinstance(value, FormulaError):
        return None, value
    return None, make_error(
        "#VALUE!", f"{value.kind} value cannot be used in arithmetic", label
    )


def apply_unary(op: str, value: Scalar, label: str) -> Scalar:
    number, error = numeric_operand(value, label)
    if error is not None:
        return error
    try:
        if op == "-":
            result = ARITHMETIC_CONTEXT.minus(number)
        else:
            result = ARITHMETIC_CONTEXT.plus(number)
    except decimal.DecimalException as exc:  # pragma: no cover - defensive
        return make_error("#NUM!", f"arithmetic failure: {exc}", label)
    return _guard(result, label)


def apply_binary(op: str, left: Scalar, right: Scalar, label: str) -> Scalar:
    if isinstance(left, FormulaError):
        return left
    if isinstance(right, FormulaError):
        return right
    if op in ("=", "<>", "<", "<=", ">", ">="):
        return _compare(op, left, right, label)

    a, error = numeric_operand(left, label)
    if error is not None:
        return error
    b, error = numeric_operand(right, label)
    if error is not None:
        return error
    try:
        if op == "+":
            result = ARITHMETIC_CONTEXT.add(a, b)
        elif op == "-":
            result = ARITHMETIC_CONTEXT.subtract(a, b)
        elif op == "*":
            result = ARITHMETIC_CONTEXT.multiply(a, b)
        elif op == "/":
            if b.is_zero():
                return make_error("#DIV/0!", "division by zero", label)
            result = ARITHMETIC_CONTEXT.divide(a, b)
        else:  # pragma: no cover - parser only emits known operators
            return make_error("#VALUE!", f"unknown operator {op}", label)
    except decimal.DecimalException as exc:
        return make_error("#NUM!", f"arithmetic failure: {exc}", label)
    return _guard(result, label)


def _compare(op: str, left: Scalar, right: Scalar, label: str) -> Scalar:
    comparable, a, b = _comparable_pair(left, right)
    if not comparable:
        return make_error(
            "#VALUE!",
            f"cannot compare {left.kind} with {right.kind}",
            label,
        )
    if op == "=":
        return Bool(a == b)
    if op == "<>":
        return Bool(a != b)
    if op == "<":
        return Bool(a < b)
    if op == "<=":
        return Bool(a <= b)
    if op == ">":
        return Bool(a > b)
    return Bool(a >= b)


def _comparable_pair(left: Scalar, right: Scalar):
    """Return ``(ok, a, b)`` with values that can be ordered.

    Numbers compare with numbers, text with text (code point order) and bool
    with bool.  Blank only compares with blank; any mixed pair is a
    ``#VALUE!``.  Both sides are already known not to be errors.
    """
    if left.kind == "blank" or right.kind == "blank":
        if left.kind == "blank" and right.kind == "blank":
            return True, 0, 0
        return False, None, None
    if isinstance(left, Number) and isinstance(right, Number):
        return True, left.value, right.value
    if isinstance(left, Text) and isinstance(right, Text):
        return True, left.value, right.value
    if isinstance(left, Bool) and isinstance(right, Bool):
        return True, left.value, right.value
    return False, None, None
