"""Operator semantics: select, project, explode, projection, absent vs null.

These are pure unit tests of the operator callables; the end-to-end timing and
memory behaviour is covered by the integration suite.
"""

import unittest

from distagg.flow.errors import FlowFailure
from distagg.flow.ops import (
    get_field,
    is_absent,
    make_explode,
    make_project,
    make_select,
)
from distagg.flow.record import Record


def rec(row, line=1, path=()):
    return Record("src", line, path, row)


class SelectTest(unittest.TestCase):
    def test_eq_bool_vs_int(self):
        rows = [
            {"v": True}, {"v": 1}, {"v": False}, {"v": 0},
            {"v": "1"}, {"v": None}, {},
        ]
        eq_true = make_select("v", "eq", True)
        self.assertEqual([r.row["v"] for r in eq_true(rec(rows[0]))], [True])
        self.assertEqual(eq_true(rec(rows[1])), [])
        eq_one = make_select("v", "eq", 1)
        self.assertEqual([r.row["v"] for r in eq_one(rec(rows[1]))], [1])
        self.assertEqual(eq_one(rec(rows[0])), [])
        # ne is the exact complement for present, non-null values.
        ne_true = make_select("v", "ne", True)
        self.assertEqual(ne_true(rec(rows[0])), [])
        self.assertEqual(len(ne_true(rec(rows[1]))), 1)
        self.assertEqual(len(ne_true(rec(rows[2]))), 1)

    def test_null_and_absent_never_pass_ordinary_comparisons(self):
        for op, value in (("eq", 1), ("eq", None), ("ne", 1), ("ne", None),
                          ("lt", 2), ("le", 2), ("gt", 0), ("ge", 0)):
            select = make_select("v", op, value)
            with self.subTest(op=op, value=value):
                self.assertEqual(select(rec({"v": None})), [])
                self.assertEqual(select(rec({})), [])

    def test_is_null_selects_null_but_not_absent(self):
        select = make_select("v", "is_null", None)
        self.assertEqual(len(select(rec({"v": None}))), 1)
        self.assertEqual(select(rec({})), [])
        self.assertEqual(select(rec({"v": 0})), [])

    def test_ordering_only_same_type_int_or_str(self):
        gt = make_select("v", "gt", 2)
        self.assertEqual(len(gt(rec({"v": 3}))), 1)
        # int/str combinations at runtime are operator errors.
        for row, value in (({"v": "3"}, 2), ({"v": 3}, "2"),
                           ({"v": True}, 2), ({"v": 3}, True)):
            with self.subTest(row=row):
                with self.assertRaises(FlowFailure):
                    make_select("v", "gt", value)(rec(row))
        # Two strings compare lexicographically, two ints numerically.
        self.assertEqual(len(make_select("v", "lt", "b")(rec({"v": "a"}))), 1)
        self.assertEqual(len(make_select("v", "le", 3)(rec({"v": 3}))), 1)

    def test_ordering_with_null_expected_value_is_never_true(self):
        # Static validation forbids a runtime float, but a null *field* with a
        # numeric expected value must not raise; it simply does not pass.
        select = make_select("v", "lt", 10)
        self.assertEqual(select(rec({"v": None})), [])


class ProjectTest(unittest.TestCase):
    def test_projection_renames_and_keeps_null(self):
        project = make_project({"a": "x", "b": "y"})
        out = project(rec({"x": 1, "y": None, "z": 9}))[0]
        self.assertEqual(out.row, {"a": 1, "b": None})

    def test_missing_input_field_is_an_error(self):
        with self.assertRaises(FlowFailure) as ctx:
            make_project({"a": "x"})(rec({"y": 1}))
        self.assertIn("absent", str(ctx.exception))
        self.assertEqual(ctx.exception.identity["line"], 1)

    def test_deep_copy_isolates_nested_objects(self):
        source = {"nested": {"v": 1}}
        out = make_project({"n": "nested"})(rec(source))[0]
        source["nested"]["v"] = 99
        self.assertEqual(out.row["n"]["v"], 1)
        out.row["n"]["v"] = 7
        self.assertEqual(source["nested"]["v"], 99)


class ExplodeTest(unittest.TestCase):
    def test_index_then_child_appends_path_and_replaces_field(self):
        explode = make_explode("items")
        record = rec({"id": 1, "items": ["a", "b"]}, line=4, path=(2,))
        out = list(explode(record))
        self.assertEqual([r.expansion_path for r in out], [(2, 0), (2, 1)])
        self.assertEqual([r.row["items"] for r in out], ["a", "b"])
        self.assertEqual([r.row["id"] for r in out], [1, 1])
        self.assertEqual([r.line for r in out], [4, 4])
        # The original row is untouched and the children share no list.
        self.assertEqual(record.row["items"], ["a", "b"])

    def test_empty_array_emits_nothing(self):
        self.assertEqual(list(make_explode("items")(rec({"items": []}))), [])

    def test_duplicate_values_are_distinct_items(self):
        out = list(make_explode("items")(rec({"items": [7, 7]})))
        self.assertEqual([r.expansion_path for r in out], [(0,), (1,)])
        self.assertEqual([r.row["items"] for r in out], [7, 7])

    def test_non_array_or_absent_is_an_error(self):
        for row in ({"items": None}, {"items": "no"}, {}, {"items": 3}):
            with self.subTest(row=row):
                with self.assertRaises(FlowFailure):
                    list(make_explode("items")(rec(row)))

    def test_nested_explode_paths_keep_appending(self):
        first = list(make_explode("items")(rec({"items": [[1, 2], [3]]})))
        records = []
        for child in first:
            records.extend(make_explode("items")(rec(child.row, line=1,
                                                     path=child.expansion_path)))
        self.assertEqual([r.expansion_path for r in records],
                         [(0, 0), (0, 1), (1, 0)])
        self.assertEqual([r.row["items"] for r in records], [1, 2, 3])


class FieldHelpersTest(unittest.TestCase):
    def test_absent_and_null_are_distinguishable(self):
        row = {"a": None}
        self.assertTrue(is_absent(row, "b"))
        self.assertFalse(is_absent(row, "a"))
        self.assertIsNone(get_field(row, "a"))
        sentinel = get_field(row, "b")
        self.assertIsNotNone(sentinel)
        self.assertFalse(sentinel is None)


if __name__ == "__main__":
    unittest.main()
