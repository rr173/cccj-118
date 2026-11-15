"""Differential test: evaluator vs. an independent, tiny Python model.

The model is a separate recursive-descent evaluator over the same grammar,
written with Python ``Fraction``/``Decimal`` arithmetic and following the
documented semantics independently of ``distagg.workbook.evaluator``.  Random
formulas are generated from a small grammar, placed into a hand-built
workbook with fixed input cells, and both implementations must agree on the
string form of every result -- including typed errors.

The reference model does not reuse the package's parser, AST, value classes
or error propagation code.
"""

import json
import random
import re
import unittest
from decimal import Decimal

from workbook_fixtures import WorkbookBuilder
from workbook_helpers import WorkbookTestCase, read_jsonl


# ---------------------------------------------------------------------------
# independent reference model
# ---------------------------------------------------------------------------

class RefError(Exception):
    def __init__(self, code):
        self.code = code


PREC = 34


_CONTEXT = None


def _context():
    global _CONTEXT
    if _CONTEXT is None:
        from decimal import Context, ROUND_HALF_EVEN

        _CONTEXT = Context(prec=PREC, rounding=ROUND_HALF_EVEN)
    return _CONTEXT


def _round(value: Decimal) -> Decimal:
    return _context().plus(value)


def _divide(a: Decimal, b: Decimal) -> Decimal:
    return _context().divide(a, b)


class Model:
    """A deliberately separate evaluator for the same documented subset."""

    def __init__(self, cells, sheets):
        self.cells = cells          # (sheet,row,col) -> ("num", text)/("text", s)
        self.sheets = sheets
        self.memo = {}

    def cell(self, sheet, row, col):
        key = (sheet, row, col)
        if key in self.memo:
            return self.memo[key]
        raw = self.cells.get(key)
        if raw is None:
            value = ("blank", None)
        elif raw[0] == "formula":
            value = self.eval_formula(raw[1], sheet)
        else:
            value = raw
        self.memo[key] = value
        return value

    def eval_formula(self, text, sheet):
        tokens = _tokenize(text)
        parser = _Parser(tokens)
        tree = parser.parse()
        return self.eval(tree, sheet)

    # -- evaluation -----------------------------------------------------
    def eval(self, node, sheet):
        kind = node[0]
        if kind == "num":
            return ("num", node[1])
        if kind == "text":
            return ("text", node[1])
        if kind == "bool":
            return ("bool", node[1])
        if kind == "ref":
            _, ref_sheet, row, col = node
            target = ref_sheet or sheet
            if target.lower() not in self.sheets:
                return ("error", "#REF!")
            return self.cell(self.sheets[target.lower()], row, col)
        if kind == "unary":
            value = self.eval(node[2], sheet)
            if value[0] == "error":
                return value
            number = _as_number(value)
            if isinstance(number, str):
                return ("error", number)
            return ("num", -number if node[1] == "-" else number)
        if kind == "binary":
            op = node[1]
            left = self.eval(node[2], sheet)
            right = self.eval(node[3], sheet)
            return self.binop(op, left, right)
        if kind == "call":
            return self.call(node[1], node[2], sheet)
        raise AssertionError(node)

    def binop(self, op, left, right):
        if left[0] == "error":
            return left
        if right[0] == "error":
            return right
        if op in ("=", "<>", "<", "<=", ">", ">="):
            return self.compare(op, left, right)
        a = _as_number(left)
        if isinstance(a, str):
            return ("error", a)
        b = _as_number(right)
        if isinstance(b, str):
            return ("error", b)
        # Arithmetic must run *inside* the 34-digit context: the global
        # default context is precision 28, so ``a + b`` would already have
        # lost digits before any explicit rounding.
        context = _context()
        if op == "+":
            return ("num", context.add(a, b))
        if op == "-":
            return ("num", context.subtract(a, b))
        if op == "*":
            return ("num", context.multiply(a, b))
        if b == 0:
            return ("error", "#DIV/0!")
        return ("num", _divide(a, b))

    def compare(self, op, left, right):
        lk, lv = left
        rk, rv = right
        if lk == "blank" and rk == "blank":
            pair = (0, 0)
        elif lk == rk and lk in ("num", "text", "bool"):
            pair = (lv, rv)
        else:
            return ("error", "#VALUE!")
        a, b = pair
        result = {
            "=": a == b, "<>": a != b, "<": a < b, "<=": a <= b,
            ">": a > b, ">=": a >= b,
        }[op]
        return ("bool", result)

    def call(self, name, args, sheet):
        if name == "IF":
            condition = self.eval(args[0], sheet)
            if condition[0] == "error":
                return condition
            if condition[0] != "bool":
                return ("error", "#VALUE!")
            if condition[1]:
                return self.eval(args[1], sheet)
            if len(args) == 3:
                return self.eval(args[2], sheet)
            return ("bool", False)
        if name == "IFERROR":
            first = self.eval(args[0], sheet)
            if first[0] == "error":
                return self.eval(args[1], sheet)
            return first
        # aggregate
        numbers = []
        for argument in args:
            if argument[0] == "range":
                _, ref_sheet, top, left, bottom, right = argument
                target = ref_sheet or sheet
                if target.lower() not in self.sheets:
                    return ("error", "#REF!")
                target = self.sheets[target.lower()]
                for row in range(top, bottom + 1):
                    for col in range(left, right + 1):
                        value = self.cell(target, row, col)
                        if value[0] == "error":
                            return value
                        if value[0] == "num":
                            numbers.append(value[1])
            else:
                value = self.eval(argument, sheet)
                if value[0] == "error":
                    return value
                if value[0] == "num":
                    numbers.append(value[1])
        if name == "COUNT":
            return ("num", Decimal(len(numbers)))
        if not numbers:
            return ("num", Decimal(0))
        if name == "SUM":
            total = Decimal(0)
            context = _context()
            for number in numbers:
                total = context.add(total, number)
            return ("num", total)
        if name == "MIN":
            return ("num", min(numbers))
        if name == "MAX":
            return ("num", max(numbers))
        raise AssertionError(name)


def _as_number(value):
    if value[0] == "num":
        return value[1]
    if value[0] == "blank":
        return Decimal(0)
    return "#VALUE!"


# -- tokenizer/parser for the model (independent of the package) -----------

_TOKEN = re.compile(
    r'\s*(?:(\d+(?:\.\d+)?)|"((?:[^"]|"")*)"'
    r'|(?:([A-Za-z][A-Za-z0-9_ ]*)!)?(\$?[A-Za-z]{1,3}\$?\d+)'
    r'|([A-Za-z][A-Za-z0-9_]*)\s*\()'
    r'|(<=|>=|<>|[()+*/<>=,:-])|(TRUE|FALSE)'
)


def _tokenize(text):
    text = text.lstrip("=")
    tokens = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        match = _TOKEN.match(text, pos)
        if not match:
            raise AssertionError(f"model cannot tokenize {text[pos:]!r}")
        if match.group(1) is not None:
            tokens.append(("num", Decimal(match.group(1))))
        elif match.group(2) is not None:
            tokens.append(("text", match.group(2).replace('""', '"')))
        elif match.group(4) is not None:
            tokens.append(("cellq", match.group(3), match.group(4)))
        elif match.group(5) is not None:
            tokens.append(("func", match.group(5).upper()))
            tokens.append(("op", "("))
        elif match.group(7) is not None:
            tokens.append(("bool", match.group(7) == "TRUE"))
        else:
            tokens.append(("op", match.group(6)))
        pos = match.end()
    tokens.append(("end", None))
    return tokens


def _cell_parts(text):
    match = re.match(r"(\$?)([A-Za-z]{1,3})(\$?)(\d+)", text)
    abs_col, letters, abs_row, digits = match.groups()
    col = 0
    for ch in letters.upper():
        col = col * 26 + ord(ch) - 64
    return int(digits), col


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.index = 0

    def peek(self):
        return self.tokens[self.index]

    def take(self):
        token = self.tokens[self.index]
        self.index += 1
        return token

    def parse(self):
        return self.comparison()

    def comparison(self):
        left = self.additive()
        while self.peek()[0] == "op" and self.peek()[1] in (
                "=", "<>", "<", "<=", ">", ">="):
            op = self.take()[1]
            left = ("binary", op, left, self.additive())
        return left

    def additive(self):
        left = self.multiplicative()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            left = ("binary", op, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            left = ("binary", op, left, self.unary())
        return left

    def unary(self):
        if self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            return ("unary", op, self.unary())
        return self.primary()

    def primary(self):
        token = self.take()
        if token[0] == "num":
            return ("num", token[1])
        if token[0] == "text":
            return ("text", token[1])
        if token[0] == "bool":
            return ("bool", token[1])
        if token[0] == "cellq":
            return self.reference(token[1], token[2])
        if token[0] == "func":
            args = []
            # The tokenizer emits an explicit "(" after every function name.
            assert self.take() == ("op", "(")
            if self.peek() == ("op", ")"):
                self.take()
            else:
                while True:
                    args.append(self.comparison())
                    if self.peek() == ("op", ","):
                        self.take()
                        continue
                    break
                assert self.take() == ("op", ")"), self.peek()
            return ("call", token[1], args)
        if token == ("op", "("):
            node = self.comparison()
            assert self.take() == ("op", ")")
            return node
        raise AssertionError(token)

    def reference(self, sheet_name, cell_text):
        row, col = _cell_parts(cell_text)
        if self.peek() == ("op", ":"):
            self.take()
            second = self.take()
            assert second[0] == "cellq"
            row2, col2 = _cell_parts(second[2])
            return ("range", sheet_name, min(row, row2), min(col, col2),
                    max(row, row2), max(col, col2))
        return ("ref", sheet_name, row, col)


# ---------------------------------------------------------------------------
# the differential test
# ---------------------------------------------------------------------------

INPUTS = {
    ("S", 1, 1): ("num", Decimal("2")),
    ("S", 2, 1): ("num", Decimal("0")),
    ("S", 3, 1): ("text", "abc"),
    ("S", 4, 1): ("blank", None),
    ("S", 5, 1): ("num", Decimal("-3.5")),
    ("S", 6, 1): ("bool", True),
    ("R", 1, 1): ("num", Decimal("0.13")),
}

_SHEETS = {"s": "S", "r": "R"}


def _random_formula(rng, depth=0):
    if depth >= 3:
        choice = rng.random()
        if choice < 0.5:
            return rng.choice(["A1", "A2", "A3", "A4", "A5", "$A$5", "R!A1"])
        if choice < 0.75:
            return rng.choice(["1", "2.5", "0", "TRUE", "FALSE", '"x"'])
        return rng.choice(["SUM(A1:A5)", "COUNT(A1:A5)", "MIN(A1:A5)",
                           "MAX(A1:A5)"])
    kind = rng.random()
    if kind < 0.40:
        op = rng.choice(["+", "-", "*", "/"])
        return f"{_random_formula(rng, depth + 1)}{op}{_random_formula(rng, depth + 1)}"
    if kind < 0.55:
        op = rng.choice(["=", "<>", "<", "<=", ">", ">="])
        return f"{_random_formula(rng, depth + 1)}{op}{_random_formula(rng, depth + 1)}"
    if kind < 0.65:
        return f"-{_random_formula(rng, depth + 1)}"
    if kind < 0.75:
        return f"({_random_formula(rng, depth + 1)})"
    if kind < 0.85:
        return (f"IF({_random_formula(rng, depth + 1)},"
                f"{_random_formula(rng, depth + 1)},"
                f"{_random_formula(rng, depth + 1)})")
    if kind < 0.92:
        return (f"IFERROR({_random_formula(rng, depth + 1)},"
                f"{_random_formula(rng, depth + 1)})")
    name = rng.choice(["SUM", "MIN", "MAX", "COUNT"])
    return name + "(" + _random_formula(rng, depth + 1) + ")"


def _format(value):
    if value[0] == "error":
        return value[1]
    if value[0] == "bool":
        return "TRUE" if value[1] else "FALSE"
    if value[0] == "num":
        if value[1].is_zero():
            return "0"
        return format(value[1], "f")
    if value[0] == "text":
        return value[1]
    return ""


class DifferentialTest(WorkbookTestCase):
    FORMULAS = 40

    def test_random_formulas_match_reference_model(self):
        for seed in (20241115, 7, 99, 1234, 555):
            with self.subTest(seed=seed):
                self._run_seed(seed)

    def _run_seed(self, seed):
        rng = random.Random(seed)
        formulas = [_random_formula(rng) for _ in range(self.FORMULAS)]
        builder = self.builder()
        sheet = builder.sheet("S")
        rates = builder.sheet("R")
        for (sheet_name, row, col), payload in INPUTS.items():
            target = sheet if sheet_name == "S" else rates
            cell = f"A{row}"
            if payload[0] == "num":
                target.cell(cell, value=format(payload[1], "f"))
            elif payload[0] == "text":
                target.cell(cell, inline=payload[1])
            elif payload[0] == "bool":
                target.cell(cell, kind="b", value="1" if payload[1] else "0")
            # a blank input is simply an absent cell
        for index, formula in enumerate(formulas, start=1):
            sheet.cell(f"C{index}", formula=formula)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": len(formulas),
            "fields": [{"name": "v", "column": "C"}],
        }]})

        model_cells = dict(INPUTS)
        for index, formula in enumerate(formulas, start=1):
            model_cells[("S", index, 3)] = ("formula", formula)
        model = Model(model_cells, _SHEETS)

        proc = self.recalc(mode="annotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        for index, formula in enumerate(formulas, start=1):
            expected = model.cell("S", index, 3)
            actual = rows[index - 1]["cells"]["v"]
            with self.subTest(formula=formula):
                if expected[0] == "error":
                    self.assertEqual(actual["status"], "error", formula)
                    self.assertEqual(actual["type"], expected[1], formula)
                elif expected[0] == "blank":
                    self.assertEqual(actual["status"], "blank", formula)
                else:
                    self.assertEqual(actual["status"], "ok", formula)
                    if expected[0] == "bool":
                        self.assertIs(actual["value"], expected[1], formula)
                    else:
                        self.assertEqual(str(actual["value"]),
                                         _format(expected), formula)


if __name__ == "__main__":
    unittest.main()
