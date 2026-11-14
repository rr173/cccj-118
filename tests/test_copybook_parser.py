"""Parser and compiler tests for the restricted copybook subset."""

import unittest

from distagg.copybook import (
    CopybookSyntaxError,
    LayoutError,
    SelectorConfigError,
    UnsupportedError,
    compile_copybook,
)
from distagg.copybook.parser import parse_copybook


def compile_ok(source, selectors=None):
    return compile_copybook(source, selectors=selectors)


def compile_bad(source, selectors=None):
    return failing(source, selectors)


def failing(source, selectors=None):
    try:
        compile_copybook(source, selectors=selectors)
    except Exception as exc:  # noqa: BLE001 - tests inspect the type/message
        return exc
    raise AssertionError("copybook unexpectedly compiled")


class FormatTest(unittest.TestCase):
    def test_free_format_and_case_insensitivity(self):
        source = (
            "01 rec.\n"
            "  05 a pic x(2).  05 B\n"
            "     pic 9(3).\n"
            "05 c usage comp pic 9(2).\n"
        )
        layout = compile_ok(source)
        paths = [f["path"] for f in layout.describe()["fields"]]
        self.assertEqual(
            paths,
            ["REC", "REC.A", "REC.B", "REC.C"],
        )

    def test_comments_strip_to_end_of_line(self):
        source = (
            "01 REC. *> a comment with a period. and keywords PIC VALUE\n"
            "   05 A PIC X(1). *> trailing comment\n"
        )
        layout = compile_ok(source)
        self.assertEqual(layout.record_name, "REC")

    def test_empty_copybook_rejected(self):
        exc = failing("   \n*> only a comment\n")
        self.assertIsInstance(exc, CopybookSyntaxError)

    def test_unterminated_declaration_rejected(self):
        exc = failing("01 REC.\n  05 A PIC X(1)\n")
        self.assertIsInstance(exc, CopybookSyntaxError)
        self.assertIn("period", str(exc))

    def test_stray_period_rejected(self):
        exc = failing("01 REC..\n  05 A PIC X(1).\n")
        self.assertIsInstance(exc, CopybookSyntaxError)

    def test_declaration_without_level_rejected(self):
        exc = failing("REC.\n")
        self.assertIsInstance(exc, CopybookSyntaxError)

    def test_pic_repeat_forms(self):
        layout = compile_ok(
            "01 REC.\n"
            "  05 A PIC XXXX.\n"
            "  05 B PIC 999V99.\n"
            "  05 C PIC S9(3)V9(2) COMP-3.\n"
        )
        kinds = {f["path"]: (f["kind"], f["span"]["bytes"])
                 for f in layout.describe()["fields"]}
        self.assertEqual(kinds["REC.A"], ("text", 4))
        self.assertEqual(kinds["REC.B"], ("decimal", 5))
        self.assertEqual(kinds["REC.C"], ("decimal", 3))

    def test_usage_synonyms_and_bare_usage(self):
        layout = compile_ok(
            "01 REC.\n"
            "  05 A PIC 9(2) COMPUTATIONAL.\n"
            "  05 B PIC 9(2) BINARY.\n"
            "  05 C PIC 9(3) PACKED-DECIMAL.\n"
            "  05 D PIC 9(3) USAGE COMPUTATIONAL-3.\n"
            "  05 E PIC 9(2) COMP.\n"
        )
        spans = {f["path"]: f["span"]["bytes"]
                 for f in layout.describe()["fields"]}
        self.assertEqual(spans["REC.A"], 2)
        self.assertEqual(spans["REC.B"], 2)
        self.assertEqual(spans["REC.C"], 2)
        self.assertEqual(spans["REC.D"], 2)
        self.assertEqual(spans["REC.E"], 2)


class LevelAndStructureTest(unittest.TestCase):
    def test_special_levels_rejected(self):
        for level in ("66", "77", "88"):
            exc = failing(f"01 REC.\n  {level} A PIC X(1).\n")
            self.assertIsInstance(exc, UnsupportedError, level)
            self.assertEqual(exc.line, 2)

    def test_out_of_range_level_rejected(self):
        exc = failing("01 REC.\n  50 A PIC X(1).\n")
        self.assertIsInstance(exc, CopybookSyntaxError)

    def test_first_declaration_must_be_01(self):
        exc = failing("05 A PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)

    def test_only_one_root(self):
        exc = failing("01 REC.\n  05 A PIC X(1).\n01 OTHER.\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertEqual(exc.line, 3)

    def test_root_must_be_group(self):
        exc = failing("01 REC PIC X(4).\n")
        self.assertIsInstance(exc, CopybookSyntaxError)

    def test_root_cannot_have_occurs(self):
        exc = failing("01 REC OCCURS 2.\n  05 A PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)

    def test_child_under_pic_item_rejected(self):
        exc = failing("01 REC.\n  05 A PIC X(1).\n    10 B PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("line 2", str(exc))

    def test_empty_group_rejected(self):
        exc = failing("01 REC.\n  05 G.\n  05 H PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("no subordinate", str(exc))

    def test_filler_group_rejected(self):
        exc = failing("01 REC.\n  05 FILLER.\n    10 A PIC X(1).\n")
        self.assertIsInstance(exc, UnsupportedError)

    def test_duplicate_names_in_group_rejected(self):
        exc = failing(
            "01 REC.\n"
            "  05 G.\n"
            "    10 A PIC X(1).\n"
            "    10 A PIC 9(1).\n"
        )
        self.assertIsInstance(exc, LayoutError)
        self.assertEqual(exc.line, 4)
        self.assertIn("line 3", str(exc))

    def test_same_name_in_different_groups_ok(self):
        layout = compile_ok(
            "01 REC.\n"
            "  05 G1.\n    10 ID PIC X(2).\n"
            "  05 G2.\n    10 ID PIC X(3).\n"
        )
        paths = [f["path"] for f in layout.describe()["fields"]]
        self.assertIn("REC.G1.ID", paths)
        self.assertIn("REC.G2.ID", paths)


class UnsupportedConstructTest(unittest.TestCase):
    def test_value_clause_rejected_with_line_and_field(self):
        exc = failing("01 REC.\n  05 A PIC X(1) VALUE 'X'.\n")
        self.assertIsInstance(exc, UnsupportedError)
        self.assertEqual(exc.line, 2)
        self.assertEqual(exc.field, "A")
        self.assertIn("VALUE", str(exc))

    def test_sign_clause_rejected(self):
        exc = failing("01 REC.\n  05 A PIC S9(3) SIGN LEADING SEPARATE.\n")
        self.assertIsInstance(exc, UnsupportedError)

    def test_signed_display_rejected(self):
        exc = failing("01 REC.\n  05 A PIC S9(3) USAGE DISPLAY.\n")
        self.assertIsInstance(exc, UnsupportedError)
        self.assertIn("signed DISPLAY", str(exc))

    def test_edited_picture_rejected(self):
        for pic in ("Z(3)9", "9,999", "****9", "99.99", "A(4)", "9(2)B9(2)"):
            exc = failing(f"01 REC.\n  05 A PIC {pic}.\n")
            self.assertIsInstance(exc, UnsupportedError, pic)

    def test_too_many_digits_rejected(self):
        exc = failing("01 REC.\n  05 A PIC 9(19).\n")
        self.assertIsInstance(exc, UnsupportedError)

    def test_zero_repeat_rejected(self):
        exc = failing("01 REC.\n  05 A PIC X(0).\n")
        self.assertIsInstance(exc, CopybookSyntaxError)

    def test_occurs_key_rejected(self):
        exc = failing(
            "01 REC.\n  05 A OCCURS 3 ASCENDING KEY B PIC X(1).\n")
        self.assertIsInstance(exc, UnsupportedError)

    def test_occurs_to_without_depending_rejected(self):
        exc = failing("01 REC.\n  05 A OCCURS 1 TO 3 PIC X(1).\n")
        self.assertIsInstance(exc, CopybookSyntaxError)

    def test_occurs_bad_bounds_rejected(self):
        exc = failing(
            "01 REC.\n  05 N PIC 9(1).\n"
            "  05 A OCCURS 3 TO 1 DEPENDING ON REC.N PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)

    def test_occurs_zero_fixed_rejected(self):
        exc = failing("01 REC.\n  05 A OCCURS 0 PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)

    def test_comp_on_text_rejected(self):
        exc = failing("01 REC.\n  05 A PIC X(2) COMP.\n")
        self.assertIsInstance(exc, UnsupportedError)

    def test_duplicate_clauses_rejected(self):
        exc = failing("01 REC.\n  05 A PIC X(1) PIC X(2).\n")
        self.assertIsInstance(exc, CopybookSyntaxError)
        exc = failing("01 REC.\n  05 A PIC 9(1) COMP COMP.\n")
        self.assertIsInstance(exc, CopybookSyntaxError)
        exc = failing("01 REC.\n  05 A OCCURS 2 OCCURS 3 PIC X(1).\n")
        self.assertIsInstance(exc, CopybookSyntaxError)

    def test_unknown_word_rejected(self):
        exc = failing("01 REC.\n  05 A PIC X(1) FROB.\n")
        self.assertIsInstance(exc, CopybookSyntaxError)
        self.assertEqual(exc.field, "A")


class DependingOnTest(unittest.TestCase):
    def test_unknown_counter_rejected(self):
        exc = failing(
            "01 REC.\n  05 A OCCURS 0 TO 2 DEPENDING ON REC.NOPE PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("REC.NOPE", str(exc))

    def test_counter_must_be_full_path(self):
        exc = failing(
            "01 REC.\n  05 N PIC 9(1).\n"
            "  05 A OCCURS 0 TO 2 DEPENDING ON N PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("full path", str(exc).replace("must start with", "full path must start with"))

    def test_backward_reference_rejected(self):
        exc = failing(
            "01 REC.\n"
            "  05 A OCCURS 0 TO 2 DEPENDING ON REC.N PIC X(1).\n"
            "  05 N PIC 9(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("earlier", str(exc))

    def test_repeating_counter_rejected(self):
        exc = failing(
            "01 REC.\n"
            "  05 G OCCURS 2.\n    10 N PIC 9(1).\n"
            "  05 A OCCURS 0 TO 2 DEPENDING ON REC.G.N PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("repeating", str(exc))

    def test_text_counter_rejected(self):
        exc = failing(
            "01 REC.\n  05 N PIC X(1).\n"
            "  05 A OCCURS 0 TO 2 DEPENDING ON REC.N PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("integer", str(exc))

    def test_decimal_counter_rejected(self):
        exc = failing(
            "01 REC.\n  05 N PIC 9(1)V9(1) COMP-3.\n"
            "  05 A OCCURS 0 TO 2 DEPENDING ON REC.N PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)

    def test_counter_inside_redefines_rejected(self):
        source = (
            "01 REC.\n"
            "  05 K PIC X(1).\n"
            "  05 N PIC 9(1).\n"
            "  05 M REDEFINES N PIC 9(1).\n"
            "  05 A OCCURS 0 TO 2 DEPENDING ON REC.M PIC X(1).\n"
        )
        selectors = {"REC.N": {"using": "REC.K", "map": {"1": "REC.N", "2": "REC.M"}}}
        exc = failing(source, selectors)
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("REDEFINES", str(exc))

    def test_fixed_occurs_inside_variable_group_ok(self):
        layout = compile_ok(
            "01 REC.\n"
            "  05 N PIC 9(1) COMP.\n"
            "  05 G OCCURS 0 TO 3 DEPENDING ON REC.N.\n"
            "    10 TAG PIC X(2) OCCURS 3.\n"
        )
        details = {f["path"]: f for f in layout.describe()["fields"]}
        self.assertEqual(details["REC.G.TAG"]["occurs"],
                         {"kind": "fixed", "times": 3})
        self.assertEqual(details["REC.G"]["span"],
                         {"kind": "variable", "min_bytes": 0, "max_bytes": 18})

    def test_occurs_n_depending_means_zero_to_n(self):
        layout = compile_ok(
            "01 REC.\n"
            "  05 N PIC 9(1) COMP.\n"
            "  05 A OCCURS 4 DEPENDING ON REC.N PIC X(1).\n"
        )
        fields = {f["path"]: f for f in layout.describe()["fields"]}
        self.assertEqual(fields["REC.A"]["occurs"],
                         {"kind": "depending", "min": 0, "max": 4,
                          "on": "REC.N"})

    def test_multiple_fillers_numbered_in_report(self):
        layout = compile_ok(
            "01 REC.\n"
            "  05 FILLER PIC X(1).\n"
            "  05 A PIC X(1).\n"
            "  05 FILLER PIC X(2).\n"
        )
        paths = [f["path"] for f in layout.describe()["fields"]]
        self.assertEqual(paths, ["REC", "REC.FILLER", "REC.A", "REC.FILLER(2)"])


class RedefinesTest(unittest.TestCase):
    SELECTORS = {"REC.B": {"using": "REC.K", "map": {"A": "REC.B", "B": "REC.C"}}}

    def test_unknown_target_rejected(self):
        exc = failing("01 REC.\n  05 A PIC X(1).\n  05 B REDEFINES Z PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)

    def test_must_immediately_follow_target(self):
        exc = failing(
            "01 REC.\n"
            "  05 A PIC X(1).\n"
            "  05 B PIC X(1).\n"
            "  05 C REDEFINES A PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("immediately", str(exc))

    def test_chain_rejected(self):
        exc = failing(
            "01 REC.\n"
            "  05 A PIC X(1).\n"
            "  05 B REDEFINES A PIC X(1).\n"
            "  05 C REDEFINES B PIC X(1).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("chain", str(exc))

    def test_variable_occurs_in_branch_rejected(self):
        source = (
            "01 REC.\n"
            "  05 K PIC X(1).\n"
            "  05 N PIC 9(1).\n"
            "  05 B PIC X(2).\n"
            "  05 C REDEFINES B.\n"
            "    10 D OCCURS 0 TO 2 DEPENDING ON REC.N PIC X(1).\n"
        )
        exc = failing(source, self.SELECTORS)
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("variable-length OCCURS", str(exc))

    def test_redefines_inside_occurs_rejected(self):
        source = (
            "01 REC.\n"
            "  05 K PIC X(1).\n"
            "  05 G OCCURS 2.\n"
            "    10 B PIC X(1).\n"
            "    10 C REDEFINES B PIC X(1).\n"
        )
        selectors = {"REC.G.B": {"using": "REC.K", "map": {"A": "REC.G.B", "B": "REC.G.C"}}}
        exc = failing(source, selectors)
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("OCCURS", str(exc))

    def test_span_is_max_of_branches(self):
        source = (
            "01 REC.\n"
            "  05 K PIC X(1).\n"
            "  05 B PIC X(2).\n"
            "  05 C REDEFINES B.\n"
            "    10 D PIC X(5).\n"
            "  05 TAIL PIC X(1).\n"
        )
        layout = compile_ok(source, self.SELECTORS)
        fields = {f["path"]: f for f in layout.describe()["fields"]}
        self.assertEqual(fields["REC.B"]["offset"], 1)
        self.assertEqual(fields["REC.C"]["offset"], 1)
        self.assertEqual(fields["REC.TAIL"]["offset"], 1 + 5)
        self.assertEqual(layout.max_span, 7)

    def test_filler_branch_rejected(self):
        exc = failing(
            "01 REC.\n"
            "  05 B PIC X(2).\n"
            "  05 FILLER REDEFINES B PIC X(3).\n")
        self.assertIsInstance(exc, LayoutError)
        self.assertIn("FILLER", str(exc))

    def test_fixed_occurs_on_branch_ok(self):
        source = (
            "01 REC.\n"
            "  05 K PIC X(1).\n"
            "  05 B PIC X(2) OCCURS 2.\n"
            "  05 C REDEFINES B PIC X(4).\n"
        )
        layout = compile_ok(source, self.SELECTORS)
        fields = {f["path"]: f for f in layout.describe()["fields"]}
        self.assertEqual(fields["REC.B"]["span"]["bytes"], 4)


class SelectorConfigTest(unittest.TestCase):
    SOURCE = (
        "01 REC.\n"
        "  05 K PIC X(1).\n"
        "  05 B PIC X(2).\n"
        "  05 C REDEFINES B PIC X(3).\n"
    )

    def test_missing_selector_config_rejected(self):
        exc = failing(self.SOURCE)
        self.assertIsInstance(exc, SelectorConfigError)
        self.assertIn("REC.B", str(exc))

    def test_unknown_group_in_config_rejected(self):
        exc = failing("01 REC.\n  05 A PIC X(1).\n",
                      {"REC.B": {"using": "REC.A", "map": {"x": "REC.B"}}})
        self.assertIsInstance(exc, SelectorConfigError)

    def test_selector_field_must_exist(self):
        exc = failing(self.SOURCE,
                      {"REC.B": {"using": "REC.NOPE",
                                 "map": {"A": "REC.B", "B": "REC.C"}}})
        self.assertIsInstance(exc, SelectorConfigError)

    def test_selector_field_must_be_text_or_integer(self):
        source = (
            "01 REC.\n"
            "  05 K PIC 9(1)V9(1) COMP-3.\n"
            "  05 B PIC X(2).\n"
            "  05 C REDEFINES B PIC X(3).\n"
        )
        exc = failing(source, {"REC.B": {"using": "REC.K",
                                         "map": {"A": "REC.B", "B": "REC.C"}}})
        self.assertIsInstance(exc, SelectorConfigError)

    def test_selector_must_come_before(self):
        source = (
            "01 REC.\n"
            "  05 B PIC X(2).\n"
            "  05 C REDEFINES B PIC X(3).\n"
            "  05 K PIC X(1).\n"
        )
        exc = failing(source, {"REC.B": {"using": "REC.K",
                                         "map": {"A": "REC.B", "B": "REC.C"}}})
        self.assertIsInstance(exc, SelectorConfigError)

    def test_map_to_non_branch_rejected(self):
        exc = failing(self.SOURCE,
                      {"REC.B": {"using": "REC.K",
                                 "map": {"A": "REC.B", "B": "REC.K"}}})
        self.assertIsInstance(exc, SelectorConfigError)

    def test_unreachable_branch_rejected(self):
        exc = failing(self.SOURCE,
                      {"REC.B": {"using": "REC.K",
                                 "map": {"A": "REC.B", "B": "REC.B"}}})
        self.assertIsInstance(exc, SelectorConfigError)
        self.assertIn("REC.C", str(exc))

    def test_integer_selector_field_ok(self):
        source = (
            "01 REC.\n"
            "  05 K PIC 9(1).\n"
            "  05 B PIC X(2).\n"
            "  05 C REDEFINES B PIC X(3).\n"
        )
        layout = compile_ok(source, {"REC.B": {"using": "REC.K",
                                               "map": {"1": "REC.B", "2": "REC.C"}}})
        self.assertEqual(layout.record_name, "REC")


if __name__ == "__main__":
    unittest.main()
