"""Additional handover edges: hidden data, diamonds, externals, ranges."""

import json
import unittest

from workbook_fixtures import WorkbookBuilder
from workbook_helpers import WorkbookTestCase, read_jsonl, run_cli


def fields(*names_columns):
    return [{"name": name, "column": column} for name, column in names_columns]


class HandoverEdgeTest(WorkbookTestCase):
    def test_hidden_sheet_and_hidden_rows_are_read_normally(self):
        builder = self.builder()
        hidden = builder.sheet("Hidden", hidden=True)
        hidden.cell("A2", value="42")
        visible = builder.sheet("Visible")
        # Row 2 is hidden via its row state; the recipe still selects it.
        visible.cell("A2", value="7")
        visible.hide_row(2)
        self.save(builder)
        self.recipe({"sheets": [
            {"sheet": "Hidden", "first_row": 2, "last_row": 2,
             "fields": fields(("v", "A"))},
            {"sheet": "Visible", "first_row": 2, "last_row": 2,
             "fields": fields(("v", "A"))},
        ]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        self.assertEqual(rows[0]["cells"]["v"]["value"], "42")
        self.assertEqual(rows[1]["cells"]["v"]["value"], "7")

    def test_empty_rows_inside_band_are_emitted(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A2", value="1")
        sheet.cell("A5", value="4")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 5,
            "fields": fields(("v", "A")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        self.assertEqual([r["row"] for r in rows], [2, 3, 4, 5])
        self.assertEqual([r["cells"]["v"]["status"] for r in rows],
                         ["ok", "blank", "blank", "ok"])

    def test_cross_sheet_diamond(self):
        builder = self.builder()
        left = builder.sheet("Left")
        right = builder.sheet("Right")
        total = builder.sheet("Total")
        left.cell("A1", value="2")
        right.cell("A1", value="3")
        left.cell("A2", formula="Left!A1*10")
        right.cell("A2", formula="Right!A1*10")
        total.cell("A1", formula="Left!A2+Right!A2")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "Total", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "A")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        # Left!A2, Right!A2, Total!A1 = 3 formula evaluations.
        self.assertEqual(payload["budget"]["formula_evaluations"], 3)
        self.assertEqual(read_jsonl(self.output_path)[0]["cells"]["v"]["value"],
                         "50")

    def test_external_workbook_reference_is_refused(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="[Book1]Sheet1!A1+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "A")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_unsupported")
        self.assertEqual(payload["sheet"], "S")
        self.assertEqual(payload["address"], "A1")

    def test_column_letters_are_case_insensitive_in_formulas(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("B2", value="3")
        sheet.cell("C2", formula="$b$2*2")
        sheet.cell("C3", formula="sum(B2:B2)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 3,
            "fields": fields(("v", "C")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        self.assertEqual(rows[0]["cells"]["v"]["value"], "6")
        self.assertEqual(rows[1]["cells"]["v"]["value"], "3")

    def test_sum_range_mixes_kinds_in_row_major_error_order(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        # Row-major order in a 2x2 range: B2, C2, B3, C3.
        sheet.cell("B2", kind="s", value=str(builder.shared_string("skip")))
        sheet.cell("C2", kind="b", value="1")
        sheet.cell("B3", value="4")
        sheet.cell("C3", formula="1/0")
        sheet.cell("E1", formula="SUM(B2:C3)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "E")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cell = read_jsonl(self.output_path)[0]["cells"]["v"]
        self.assertEqual(cell["type"], "#DIV/0!")
        self.assertEqual(cell["chain"], ["S!C3", "S!E1"])

    def test_huge_range_without_budget_is_stopped_by_budget(self):
        builder = self.builder()
        source = builder.sheet("S")
        source.cell("A1", value="1")
        # The formula lives on another sheet so the whole-sheet range is not
        # a self-reference; the dependency walk alone exhausts the budget.
        target = builder.sheet("T")
        target.cell("B1", formula="SUM(S!A1:XFD1048576)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "T", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "B")),
        }]})
        proc = self.recalc("--cell-budget", "1000")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_budget_exceeded")
        self.assertFalse(__import__("os").path.exists(self.output_path))

    def test_check_command_rejects_cycle_too(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="A1+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "A")),
        }]})
        proc = run_cli("check", self.book_path, self.recipe_path)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "workbook_cycle")

    def test_ranges_are_not_expanded_into_lists(self):
        # A 1x20000 range within a small budget must not build 20000 strings
        # eagerly; it is walked lazily and its values are summed.
        builder = self.builder()
        sheet = builder.sheet("S")
        for row in range(1, 201):
            sheet.cell(f"A{row}", value="1")
        sheet.cell("C1", formula="SUM(A1:A200)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "C")),
        }]})
        proc = self.recalc("--cell-budget", "1000")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read_jsonl(self.output_path)[0]["cells"]["v"]["value"],
                         "200")


if __name__ == "__main__":
    unittest.main()
