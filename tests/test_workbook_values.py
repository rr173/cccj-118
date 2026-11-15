"""Unit tests for decimal semantics, comparisons and the formula parser."""

import unittest
from decimal import Decimal

from distagg.workbook.errors import WorkbookFormulaError, WorkbookUnsupported
from distagg.workbook.formula import (
    Binary,
    CellRef,
    FunctionCall,
    Literal,
    RangeRef,
    Unary,
    parse_formula,
    translate_ref,
    unparse,
)
from distagg.workbook.values import (
    ARITHMETIC_CONTEXT,
    BLANK,
    Bool,
    FormulaError,
    Number,
    Text,
    apply_binary,
    apply_unary,
    decimal_text,
    parse_decimal_literal,
)


def number(text):
    return Number(parse_decimal_literal(text))


class DecimalModelTest(unittest.TestCase):
    def test_raw_xml_text_becomes_decimal_exactly(self):
        value = parse_decimal_literal("9007199254740993")
        self.assertEqual(value, Decimal(9007199254740993))
        self.assertTrue(decimal_text(value).endswith("0993"))
        long_text = "0.1234567890123456789012345678901234"
        self.assertEqual(decimal_text(parse_decimal_literal(long_text)), long_text)

    def test_serialization_round_trips(self):
        for text in ("0", "-0", "1", "1.5", "0.130", "1E-7", "12345678901234567890.5"):
            value = parse_decimal_literal(text)
            restored = Decimal(decimal_text(value))
            self.assertEqual(restored, value)
            self.assertEqual(decimal_text(restored), decimal_text(value))

    def test_precision_34_half_even(self):
        third = apply_binary("/", number("1"), number("3"), "L")
        self.assertEqual(decimal_text(third.value),
                         "0." + "3" * 34)
        # 35 significant digits in, 34 out: the last digit is dropped by the
        # context, not silently kept from the literal.
        result = apply_binary("*", number("1.2345678901234567890123456789012345"),
                              number("1"), "L")
        self.assertEqual(decimal_text(result.value),
                         "1.234567890123456789012345678901234")
        exact = apply_binary("+", number("0.1"), number("0.2"), "L")
        self.assertEqual(decimal_text(exact.value), "0.3")
        # A value exactly on the rounding boundary rounds to even.
        halved = decimal_text(ARITHMETIC_CONTEXT.divide(
            Decimal("1"), Decimal("2")).normalize())
        self.assertEqual(halved, "0.5")

    def test_big_integer_keeps_every_digit(self):
        value = apply_binary("*", number("9007199254740993"), number("3"), "L")
        self.assertEqual(decimal_text(value.value), "27021597764222979")

    def test_division_by_zero_is_error_not_exception(self):
        value = apply_binary("/", number("1"), number("0"), "L")
        self.assertIsInstance(value, FormulaError)
        self.assertEqual(value.code, "#DIV/0!")
        self.assertEqual(value.chain(), ["L"])

    def test_arithmetic_operand_rules(self):
        self.assertEqual(apply_binary("+", BLANK, number("2"), "L").value,
                         Decimal(2))
        self.assertEqual(apply_binary("+", number("2"), BLANK, "L").value,
                         Decimal(2))
        self.assertEqual(apply_binary("*", Text("2"), number("2"), "L").code,
                         "#VALUE!")
        self.assertEqual(apply_binary("*", Bool(True), number("2"), "L").code,
                         "#VALUE!")
        error = FormulaError("#N/A", "", None).with_prefix("X")
        self.assertEqual(apply_binary("+", error, number("1"), "L").chain(),
                         ["X"])

    def test_unary(self):
        self.assertEqual(apply_unary("-", number("2.5"), "L").value,
                         Decimal("-2.5"))
        self.assertEqual(apply_unary("-", BLANK, "L").value, Decimal(0))
        self.assertEqual(apply_unary("-", Text("x"), "L").code, "#VALUE!")
        self.assertEqual(decimal_text(apply_unary("-", number("0"), "L").value),
                         "0")

    def test_comparisons(self):
        self.assertTrue(apply_binary("<", number("2"), number("10"), "L").value)
        self.assertTrue(apply_binary("=", number("2"), number("2.0"), "L")
                        .value)  # numeric equality ignores scale
        self.assertTrue(apply_binary("=", Text("a"), Text("a"), "L").value)
        self.assertTrue(apply_binary("<", Text("Z"), Text("a"), "L").value)
        self.assertTrue(apply_binary("=", Bool(True), Bool(True), "L").value)
        self.assertTrue(apply_binary("=", BLANK, BLANK, "L").value)
        self.assertEqual(apply_binary("=", BLANK, number("0"), "L").code,
                         "#VALUE!")
        self.assertEqual(apply_binary("<", Text("1"), number("2"), "L").code,
                         "#VALUE!")
        self.assertEqual(apply_binary("=", number("1"), number("1.0"), "L").value,
                         True)

    def test_comparison_text_is_code_point_order(self):
        self.assertTrue(apply_binary("<", Text("a"), Text("b"), "L").value)
        self.assertTrue(apply_binary("<", Text("A"), Text("a"), "L").value)
        self.assertTrue(apply_binary("<", Text("a"), Text("aa"), "L").value)


class ParserTest(unittest.TestCase):
    def parse(self, text):
        return parse_formula(text, sheet="S", address="A1")

    def test_precedence_and_associativity(self):
        node = self.parse("=1+2*3")
        self.assertIsInstance(node, Binary)
        self.assertEqual(node.op, "+")
        self.assertEqual(node.right.op, "*")
        tree = self.parse("=1-2-3")
        self.assertEqual(tree.left.op, "-")   # left associative
        self.assertEqual(unparse(tree), "((1-2)-3)")

    def test_comparison_binds_loosest(self):
        tree = self.parse("=1+2=3")
        self.assertEqual(tree.op, "=")
        self.assertEqual(tree.left.op, "+")

    def test_unary_binds_tightest(self):
        tree = self.parse("=-2^" if False else "=-2*3")
        self.assertEqual(tree.op, "*")
        self.assertIsInstance(tree.left, Unary)
        tree = self.parse("=2*-3")
        self.assertIsInstance(tree.right, Unary)

    def test_parentheses(self):
        self.assertEqual(unparse(self.parse("=(1+2)*3")), "((1+2)*3)")

    def test_text_escapes(self):
        node = self.parse('="a""b"')
        self.assertEqual(node.value, ("text", 'a"b'))

    def test_booleans_are_case_insensitive(self):
        self.assertEqual(self.parse("=true").value, ("bool", True))
        self.assertEqual(self.parse("=False").value, ("bool", False))

    def test_references(self):
        node = self.parse("=$A$1+B2+A$3+$C4")
        refs = []
        stack = [node]
        while stack:
            item = stack.pop()
            if isinstance(item, CellRef):
                refs.append(item)
            elif isinstance(item, Binary):
                stack.extend((item.right, item.left))
        flags = [(ref.abs_row, ref.abs_col) for ref in refs]
        self.assertEqual(flags, [(True, True), (False, False), (True, False),
                                 (False, True)])

    def test_quoted_sheet_names(self):
        node = self.parse("='P&L 2024'!A1+'it''s'!B2")
        stack = [node]
        sheets = []
        while stack:
            item = stack.pop()
            if isinstance(item, CellRef):
                sheets.append(item.sheet)
            elif isinstance(item, Binary):
                stack.extend((item.right, item.left))
        self.assertEqual(sheets, ["P&L 2024", "it's"])

    def test_unquoted_sheet_name(self):
        node = self.parse("=Sheet2!A1")
        self.assertEqual(node.sheet, "Sheet2")

    def test_range_shape_and_translation(self):
        node = self.parse("=SUM(A1:$B$2)")
        self.assertIsInstance(node, FunctionCall)
        ref = node.args[0]
        self.assertEqual((ref.top, ref.left, ref.bottom, ref.right), (1, 1, 2, 2))
        translated = translate_ref(node, 3, 1, sheet="S", address="A1")
        moved = translated.args[0]
        self.assertEqual((moved.top, moved.left, moved.bottom, moved.right),
                         (4, 2, 2, 2))

    def test_function_names_are_case_insensitive(self):
        node = self.parse("=sum(1,2)+If(TRUE,1,2)")
        self.assertEqual(node.left.name, "SUM")
        self.assertEqual(node.right.name, "IF")

    def test_full_parse_required(self):
        for text in ("=1+", "=1 2", "=SUM(1,2", '="abc', "=()", "=A1:"):
            with self.subTest(text=text):
                with self.assertRaises((WorkbookFormulaError,
                                        WorkbookUnsupported)):
                    self.parse(text)

    def test_unsupported_constructs_are_named(self):
        cases = {
            "=[1]Sheet1!A1": "external",
            "=SUM(Table1[Amount])": "structured",
            "=SUM(A:A)": "whole-column",
            "=RAND()": "RAND",
            "=NOW()": "NOW",
            "=TODAY()": "TODAY",
            "=OFFSET(A1,1,1)": "OFFSET",
            "=INDIRECT(\"A1\")": "INDIRECT",
            "=VLOOKUP(A1,B1:C2,2,FALSE)": "VLOOKUP",
            "=A1&B1": "&",
            "=SUM(Table1[Amount])": "structured",
            "=SUM(A:A)": "whole-column",
            "=SUM(1,,2)": "empty argument",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaises((WorkbookFormulaError,
                                        WorkbookUnsupported)) as caught:
                    self.parse(text)
                self.assertIn(needle.upper(),
                              str(caught.exception).upper())

    def test_defined_name_rejected(self):
        with self.assertRaises(WorkbookUnsupported) as caught:
            self.parse("=tax_rate*2")
        self.assertEqual(caught.exception.construct, "defined name")

    def test_out_of_grid_reference_becomes_bad_reference(self):
        node = self.parse("=XFE1")
        self.assertEqual(type(node).__name__, "BadReference")

    def test_if_arity(self):
        with self.assertRaises(WorkbookFormulaError):
            self.parse("=IF(TRUE)")
        with self.assertRaises(WorkbookFormulaError):
            self.parse("=IFERROR(1)")
        self.parse("=IF(TRUE,1)")
        self.parse("=IFERROR(1,2)")


if __name__ == "__main__":
    unittest.main()
