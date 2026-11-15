"""Cycles, budgets, strict/annotate delivery and very deep chains."""

import json
import os
import resource
import time
import unittest

from workbook_fixtures import WorkbookBuilder
from workbook_helpers import WorkbookTestCase, read_jsonl, run_cli


def fields(*names_columns):
    return [{"name": name, "column": column} for name, column in names_columns]


def recipe_for(rows, sheet="S", names=("v", "A")):
    return {"sheets": [{
        "sheet": sheet, "first_row": 1, "last_row": rows,
        "fields": fields(names),
    }]}


class CycleTest(WorkbookTestCase):
    def test_direct_self_cycle(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="A1+1")
        self.save(builder)
        self.recipe(recipe_for(1))
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_cycle")
        self.assertEqual(payload["chain"], ["S!A1", "S!A1"])

    def test_mutual_cycle_reports_real_chain(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="B1+1")
        sheet.cell("B1", formula="C1+1")
        sheet.cell("C1", formula="A1+1")
        self.save(builder)
        self.recipe(recipe_for(1))
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_cycle")
        self.assertEqual(payload["chain"],
                         ["S!A1", "S!B1", "S!C1", "S!A1"])
        self.assertFalse(os.path.exists(self.output_path))

    def test_cycle_hidden_in_unselected_if_branch_is_still_refused(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="IF(TRUE,1,B1)")
        sheet.cell("B1", formula="A1+1")
        self.save(builder)
        self.recipe(recipe_for(1))
        for mode in ("annotate", "strict"):
            proc = self.recalc(mode=mode)
            self.assertEqual(proc.returncode, 1, mode)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error"], "workbook_cycle")
            self.assertEqual(payload["chain"], ["S!A1", "S!B1", "S!A1"])

    def test_cross_sheet_cycle(self):
        builder = self.builder()
        one = builder.sheet("One")
        two = builder.sheet("Two")
        one.cell("A1", formula="'Two'!A1+1")
        two.cell("A1", formula="One!A1+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "One", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "A")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["chain"],
                         ["One!A1", "Two!A1", "One!A1"])

    def test_cycle_outside_selected_band_is_not_an_error(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="1")
        sheet.cell("B1", formula="B1+1")   # unreferenced cycle
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "A")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)


class StrictAndAnnotateTest(WorkbookTestCase):
    def build_two_rows(self, builder):
        sheet = builder.sheet("S")
        sheet.cell("A1", value="1")
        sheet.cell("A2", formula="1/0")
        sheet.cell("A3", value="3")

    def test_strict_stops_without_publishing_a_file(self):
        builder = self.builder()
        self.build_two_rows(builder)
        self.save(builder)
        self.recipe(recipe_for(3))
        proc = self.recalc(mode="strict")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_strict_stop")
        self.assertEqual(payload["sheet"], "S")
        self.assertEqual(payload["row"], 2)
        self.assertEqual(payload["address"], "A2")
        self.assertEqual(payload["error_type"], "#DIV/0!")
        self.assertIs(payload["complete"], False)
        self.assertEqual(payload["rows_written"], 1)
        # Nothing was published at the destination...
        self.assertFalse(os.path.exists(self.output_path))
        # ...and the row that had already been emitted is preserved under an
        # explicitly "partial" name instead of being silently dropped.
        partial = payload["partial_output"]
        self.assertTrue(partial.endswith(".partial"))
        self.assertEqual([r["row"] for r in read_jsonl(partial)], [1])
        self.assertIn("NOT a complete handover", payload["partial_note"])
        leftovers = [name for name in os.listdir(self.work)
                     if name.startswith(".distagg-workbook-")]
        self.assertEqual(leftovers, [])

    def test_annotate_delivers_every_row_and_counts(self):
        builder = self.builder()
        self.build_two_rows(builder)
        self.save(builder)
        self.recipe(recipe_for(3))
        proc = self.recalc(mode="annotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIs(payload["complete"], True)
        self.assertEqual(payload["rows_written"], 3)
        self.assertEqual(payload["ok_fields"], 2)
        self.assertEqual(payload["error_fields"], 1)
        self.assertEqual(payload["blank_fields"], 0)
        self.assertEqual(payload["fields"], 3)
        rows = read_jsonl(self.output_path)
        self.assertEqual([r["row"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[1]["cells"]["v"]["status"], "error")

    def test_cycle_in_strict_mode_leaves_no_partial_file(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="A1+1")
        self.save(builder)
        self.recipe(recipe_for(1))
        proc = self.recalc(mode="strict")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "workbook_cycle")
        self.assertFalse(os.path.exists(self.output_path))
        self.assertFalse(os.path.exists(self.output_path + ".partial"))

    def test_budget_exhaustion_is_a_job_failure_not_blanks(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="B1+1")
        sheet.cell("B1", formula="C1+1")
        sheet.cell("C1", formula="D1+1")
        sheet.cell("D1", value="1")
        self.save(builder)
        self.recipe(recipe_for(1))
        proc = self.recalc("--cell-budget", "2", mode="annotate")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_budget_exceeded")
        self.assertEqual(payload["budget"], 2)
        self.assertIs(payload["complete"], False)
        self.assertFalse(os.path.exists(self.output_path))

    def test_generous_budget_succeeds(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="B1+1")
        sheet.cell("B1", value="1")
        self.save(builder)
        self.recipe(recipe_for(1))
        proc = self.recalc("--cell-budget", "1000", mode="annotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read_jsonl(self.output_path)[0]["cells"]["v"]["value"],
                         "2")

    def test_max_formula_cells_guard(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        for row in range(1, 6):
            sheet.cell(f"A{row}", formula=f"{row}+1")
        self.save(builder)
        self.recipe(recipe_for(5))
        proc = self.recalc("--max-formula-cells", "3", mode="annotate")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_error")
        self.assertIn("max_formula_cells", payload["message"])


class DeepChainTest(WorkbookTestCase):
    DEPTH = 5000

    def test_deep_chain_does_not_recurse(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        # A1 = 1; A2 = A1+1; ... A5000 = A4999+1 -> 5000
        sheet.cell("A1", value="1")
        for row in range(2, self.DEPTH + 1):
            sheet.cell(f"A{row}", formula=f"A{row - 1}+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": self.DEPTH, "last_row": self.DEPTH,
            "fields": fields(("v", "A")),
        }]})
        proc = self.recalc(mode="annotate")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["budget"]["formula_evaluations"],
                         self.DEPTH - 1)
        row = read_jsonl(self.output_path)[0]
        self.assertEqual(row["cells"]["v"]["value"], str(self.DEPTH))

    def test_deep_error_chain_is_capped_and_marked(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="1/0")
        for row in range(2, 121):
            sheet.cell(f"A{row}", formula=f"A{row - 1}+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 120, "last_row": 120,
            "fields": fields(("v", "A")),
        }]})
        proc = self.recalc(mode="annotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cell = read_jsonl(self.output_path)[0]["cells"]["v"]
        self.assertEqual(cell["status"], "error")
        self.assertEqual(cell["chain_length"], 120)
        self.assertEqual(len(cell["chain"]), 64)
        self.assertTrue(cell["chain_truncated"])
        self.assertEqual(cell["chain"][0], "S!A1")
        self.assertEqual(cell["chain"][-1], "S!A64")

    def test_wide_fan_in_shared_intermediate(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="3")
        sheet.cell("B1", formula="A1*7")
        # 200 output rows all reference the same B1.
        for row in range(1, 201):
            sheet.cell(f"C{row}", formula=f"B1+{row}")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 200,
            "fields": fields(("v", "C")),
        }]})
        proc = self.recalc(mode="annotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        # B1 evaluated once, plus 200 output formulas.
        self.assertEqual(payload["budget"]["formula_evaluations"], 201)
        rows = read_jsonl(self.output_path)
        self.assertEqual(rows[0]["cells"]["v"]["value"], "22")
        self.assertEqual(rows[199]["cells"]["v"]["value"], "221")


if __name__ == "__main__":
    unittest.main()
