"""Built-in record operators.

Every operator works on one *arriving* record and calls ``emit(record)`` for
each output record — zero, one or many — so no operator ever needs the whole
input list.  ``explode`` therefore streams children one at a time instead of
materialising the array.

Semantics that are easy to get wrong and are pinned down here:

* Field access distinguishes **absent** from **present but null**.  Public
  helpers :func:`is_absent` and :func:`get_field` are used by tests to tell
  the two apart unambiguously.
* ``select`` with ``eq``/``ne`` distinguishes booleans from integers
  (``True != 1``), because ``bool`` is a subclass of ``int`` in Python.
  Ordinary comparisons never pass when the field is absent or null; only
  ``is_null`` selects null (never an absent field).
* Ordering comparisons (``lt``/``le``/``gt``/``ge``) accept only two integers
  (bools excluded) or two strings.  Every other pairing is an operator
  misconfiguration and fails the run before the source is opened.
* ``project`` copies only the named input fields.  A missing field is an
  error, never guessed; a null value is copied as null.  Copying is deep, so
  a downstream branch that mutates a nested object cannot affect another
  branch's copy of the same source row.
* ``explode`` requires an array and reports a non-array as a record error.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

from .errors import FlowFailure
from .record import Record


# --------------------------------------------------------------------------- #
# field helpers
# --------------------------------------------------------------------------- #

_ABSENT = object()


def is_absent(row: Dict[str, Any], name: str) -> bool:
    return name not in row


def get_field(row: Dict[str, Any], name: str) -> Any:
    """Return the field value, or the module-level ``_ABSENT`` sentinel."""
    return row[name] if name in row else _ABSENT


def describe_value(value: Any) -> str:
    if value is _ABSENT:
        return "absent"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean {value!r}"
    if isinstance(value, int):
        return f"integer {value}"
    if isinstance(value, str):
        return f"string {value!r}"
    return type(value).__name__


# --------------------------------------------------------------------------- #
# select
# --------------------------------------------------------------------------- #

COMPARATORS = ("eq", "ne", "lt", "le", "gt", "ge", "is_null")

_SCALAR_TYPES = (bool, int, str, type(None))


def validate_condition(field: str, op: str, value: Any) -> None:
    """Static check of one select condition (used before opening sources)."""
    from .errors import GraphError

    problems: List[str] = []
    if not isinstance(field, str) or not field:
        problems.append("select.field must be a non-empty string")
    if op not in COMPARATORS:
        problems.append(
            "select.condition must be one of " + ", ".join(COMPARATORS)
            + f", got {op!r}"
        )
    if op != "is_null":
        if not any(isinstance(value, t) for t in _SCALAR_TYPES):
            problems.append(
                "select.value must be an integer, string, boolean or null, "
                f"got {type(value).__name__}"
            )
    if problems:
        raise GraphError(problems)


def _satisfies(row_has_field: bool, field_value: Any, op: str,
               expected: Any) -> bool:
    if op == "is_null":
        # Absent is *not* null: only a present field whose value is null.
        return row_has_field and field_value is None

    if not row_has_field or field_value is None:
        # Ordinary comparisons never pass with an absent field or a null value.
        return False

    actual = field_value
    if op in ("lt", "le", "gt", "ge"):
        both_bool_or_non_scalar = (
            isinstance(actual, bool) or isinstance(expected, bool)
            or not isinstance(actual, (int, str))
            or not isinstance(expected, (int, str))
        )
        if both_bool_or_non_scalar or type(actual) is not type(expected):
            raise FlowFailure(
                "select",
                f"ordering comparison {op!r} needs two integers or two strings;"
                f" field {describe_value(actual)} cannot be compared with"
                f" {describe_value(expected)}",
            )
        if op == "lt":
            return actual < expected
        if op == "le":
            return actual <= expected
        if op == "gt":
            return actual > expected
        return actual >= expected

    if op == "eq":
        return _strict_equal(actual, expected)
    if op == "ne":
        return not _strict_equal(actual, expected)
    raise AssertionError(op)  # pragma: no cover


def _strict_equal(left: Any, right: Any) -> bool:
    """Equality that keeps booleans apart from integers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return left == right


def make_select(field: str, op: str, value: Any) -> Callable[[Record], List[Record]]:
    def select(record: Record) -> List[Record]:
        row = record.row
        has_field = field in row
        field_value = row.get(field)
        if _satisfies(has_field, field_value, op, value):
            return [record]
        return []
    return select


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #

def validate_projection(output_field: str, input_field: Any) -> None:
    from .errors import GraphError

    problems: List[str] = []
    if not isinstance(output_field, str) or not output_field:
        problems.append("project output field names must be non-empty strings")
    if not isinstance(input_field, str) or not input_field:
        problems.append(
            f"project target for {output_field!r} must name an input field "
            "(a non-empty string); guessing is not supported"
        )
    if problems:
        raise GraphError(problems)


def make_project(mapping: Dict[str, str]) -> Callable[[Record], List[Record]]:
    items = list(mapping.items())

    def project(record: Record) -> List[Record]:
        row = record.row
        new_row: Dict[str, Any] = {}
        for out_name, in_name in items:
            if in_name not in row:
                raise FlowFailure(
                    "project",
                    f"input field {in_name!r} is absent; project never guesses "
                    "a missing field",
                    identity=record.identity(),
                )
            new_row[out_name] = copy.deepcopy(row[in_name])
        return [Record(record.source, record.line, record.expansion_path,
                       new_row)]
    return project


# --------------------------------------------------------------------------- #
# explode
# --------------------------------------------------------------------------- #

def make_explode(field: str) -> Callable[[Record], List[Record]]:
    def explode(record: Record):
        row = record.row
        if field not in row:
            raise FlowFailure(
                "explode",
                f"field {field!r} is absent (an absent field is not an empty "
                "array)",
                identity=record.identity(),
            )
        value = row[field]
        if not isinstance(value, list):
            raise FlowFailure(
                "explode",
                f"field {field!r} must be an array, got {type(value).__name__}",
                identity=record.identity(),
            )
        # One child record at a time: the generator never materialises the
        # whole list; the runtime consumes it with ``for out in fn(record)``.
        for index in range(len(value)):
            child_row = dict(row)  # other fields are preserved shallowly
            child_row[field] = value[index]
            yield record.child(index, child_row)
    return explode


# --------------------------------------------------------------------------- #
# custom Python operators
# --------------------------------------------------------------------------- #

class CustomOperator:
    """Wraps a caller-provided co-operative async one-to-many operator.

    The callable is ``async def operator(record, emit)`` where
    ``emit(record)`` is an async one-shot emitter.  A record is delivered
    immediately when ``emit`` is awaited; the operator may yield zero, one or
    many outputs per input and may await whatever it likes in between.  It
    must not return a whole batch list — the executor never accumulates one
    on its behalf.  Returning an async iterable of ``Record`` instead of
    calling ``emit`` is also accepted (and is closed with ``aclose()`` on
    any exit path).

    The callable may be a plain function returning an awaitable; either form
    is accepted, and ``asyncio.CancelledError`` is never swallowed by the
    runtime (only a custom operator that deliberately does so can defeat
    cancellation of its own turn).
    """

    is_custom = True

    def __init__(self, name: str, fn: Callable):
        self.name = name
        self.fn = fn

    async def __call__(self, record: Record, emit) -> None:
        result = self.fn(record, emit)
        if hasattr(result, "__await__"):
            await result


def make_custom(name: str, fn: Callable) -> CustomOperator:
    return CustomOperator(name, fn)
