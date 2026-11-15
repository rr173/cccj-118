"""Formula semantics: aggregate functions, IF/IFERROR, references and caches."""

import json
import unittest

from workbook_fixtures import WorkbookBuilder
from workbook_helpers import WorkbookTestCase, read_jsonl, run_cli


def fields(*names_columns):
    return [{"name": name, "column": column} for name, column in names_columns]


class FormulaSemanticsTest(WorkbookTestCase):
    def sheet(self, builder, name="S"):
        return builder.sheet(name)

    def run_values(self, builder, recipe_doc, mode="annotate"):
        self.save(builder)
        self.recipe(recipe_doc)
        proc = self.recalc(mode=mode)
        return proc

    def test_arithmetic_precedence_and_unary(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="2")
        sheet.cell("B1", value="3")
        sheet.cell("C1", formula="A1+B1*4")
        sheet.cell("D1", formula="-(A1+B1)*-2")
        sheet.cell("E1", formula="2*-3")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("c", "C"), ("d", "D"), ("e", "E")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = read_jsonl(self.output_path)[0]["cells"]
        self.assertEqual(cells["c"]["value"], "14")
        self.assertEqual(cells["d"]["value"], "10")
        self.assertEqual(cells["e"]["value"], "-6")

    def test_cached_value_never_wins(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="2")
        sheet.cell("B1", formula="A1*10", cached="999999")
        sheet.cell("B2", formula="A1*10")
        sheet.cell("B3", formula="A1*10", cached="not-even-numeric")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 3,
            "fields": fields(("v", "B")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        values = [r["cells"]["v"]["value"] for r in read_jsonl(self.output_path)]
        self.assertEqual(values, ["20", "20", "20"])

    def test_sum_ignores_text_bool_blank(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="1")
        sheet.cell("A2", kind="s", value=str(builder.shared_string("text")))
        sheet.cell("A3", kind="b", value="1")
        # A4 blank
        sheet.cell("A5", value="2.5")
        sheet.cell("B1", formula="SUM(A1:A5)")
        sheet.cell("B2", formula="MIN(A1:A5)")
        sheet.cell("B3", formula="MAX(A1:A5)")
        sheet.cell("B4", formula="COUNT(A1:A5)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 4,
            "fields": fields(("sum", "B"),),
        }]})
        # Different formulas per row cannot share one column; use rows.
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 4,
            "fields": fields(("s", "B"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["s"] for r in read_jsonl(self.output_path)]
        self.assertEqual([c["value"] for c in cells],
                         ["3.5", "1", "2.5", "2"])

    def test_aggregate_error_propagates_first_in_order(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="1")
        sheet.cell("A2", formula="1/0")
        sheet.cell("A3", kind="e", value="#N/A")
        sheet.cell("B1", formula="SUM(A1:A3)")
        sheet.cell("B2", formula="SUM(A3,A2)")
        sheet.cell("B3", formula="COUNT(A1:A3)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 3,
            "fields": fields(("v", "B"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        first = rows[0]["cells"]["v"]
        self.assertEqual(first["status"], "error")
        self.assertEqual(first["type"], "#DIV/0!")
        # Chain: error created at S!A2, seen through B1 which references it.
        self.assertEqual(first["chain"], ["S!A2", "S!B1"])
        second = rows[1]["cells"]["v"]
        self.assertEqual(second["type"], "#N/A")  # A3 comes first
        self.assertEqual(second["chain"], ["S!A3", "S!B2"])
        # COUNT propagates errors too (same order rule); it only counts
        # numbers when no error is met.
        count_cell = rows[2]["cells"]["v"]
        self.assertEqual(count_cell["status"], "error")
        self.assertEqual(count_cell["type"], "#DIV/0!")  # A2 precedes A3
        self.assertEqual(count_cell["chain"], ["S!A2", "S!B3"])

    def test_aggregates_with_no_numbers_are_zero(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", kind="s", value=str(builder.shared_string("x")))
        sheet.cell("C1", formula="SUM(A1)")
        sheet.cell("C2", formula="MIN(A1)")
        sheet.cell("C3", formula="MAX(A1)")
        sheet.cell("C4", formula="COUNT(A1)")
        sheet.cell("C5", formula="SUM()")
        sheet.cell("C6", formula="COUNT()")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 6,
            "fields": fields(("v", "C"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        values = [r["cells"]["v"]["value"] for r in read_jsonl(self.output_path)]
        self.assertEqual(values, ["0", "0", "0", "0", "0", "0"])

    def test_if_is_lazy_and_condition_must_be_bool(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="1")
        sheet.cell("B1", formula="IF(A1=1,42,1/0)")
        sheet.cell("B2", formula="IF(A1<>1,1/0,7)")
        sheet.cell("B3", formula="IF(1,2,3)")
        sheet.cell("B4", formula="IF(TRUE,1/0,2)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 4,
            "fields": fields(("v", "B"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["v"] for r in read_jsonl(self.output_path)]
        self.assertEqual(cells[0]["value"], "42")
        self.assertEqual(cells[1]["value"], "7")
        self.assertEqual(cells[2]["type"], "#VALUE!")
        self.assertEqual(cells[3]["type"], "#DIV/0!")

    def test_if_without_false_branch_returns_false(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="0")
        sheet.cell("B1", formula="IF(A1=0,TRUE)")
        sheet.cell("B2", formula="IF(A1=0,FALSE)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 2,
            "fields": fields(("v", "B"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["v"] for r in read_jsonl(self.output_path)]
        self.assertIs(cells[0]["value"], True)
        self.assertIs(cells[1]["value"], False)

    def test_iferror_only_evaluates_fallback_on_error(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="0")
        sheet.cell("B1", formula="IFERROR(10/A1,-1)")
        sheet.cell("B2", formula="IFERROR(10,1/0)")
        sheet.cell("B3", formula="IFERROR(1/0,IFERROR(1/0,5))")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 3,
            "fields": fields(("v", "B"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        values = [r["cells"]["v"]["value"] for r in read_jsonl(self.output_path)]
        self.assertEqual(values, ["-1", "10", "5"])

    def test_cross_sheet_and_quoted_sheet_names(self):
        builder = self.builder()
        rates = builder.sheet("P&L 2024")
        rates.cell("A1", value="0.13")
        other = builder.sheet("it's")
        other.cell("B2", value="7")
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="'P&L 2024'!A1")
        sheet.cell("A2", formula="'it''s'!B2*2")
        sheet.cell("A3", formula="'missing'!A1")
        sheet.cell("A4", formula="=SUM('p&l 2024'!A1:A1)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 4,
            "fields": fields(("v", "A"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["v"] for r in read_jsonl(self.output_path)]
        self.assertEqual(cells[0]["value"], "0.13")
        self.assertEqual(cells[1]["value"], "14")
        self.assertEqual(cells[2]["type"], "#REF!")
        self.assertIn("missing", cells[2]["message"])
        self.assertEqual(cells[3]["value"], "0.13")

    def test_missing_sheet_is_ref_not_blank(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="Nowhere!Z99+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "A"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cell = read_jsonl(self.output_path)[0]["cells"]["v"]
        self.assertEqual(cell["status"], "error")
        self.assertEqual(cell["type"], "#REF!")

    def test_out_of_grid_reference_is_ref(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="XFE1+1")
        sheet.cell("A2", formula="=SUM(XFD1048577)")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 2,
            "fields": fields(("v", "A"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["v"] for r in read_jsonl(self.output_path)]
        self.assertEqual(cells[0]["type"], "#REF!")
        self.assertEqual(cells[1]["type"], "#REF!")

    def test_missing_sheet_reference_is_reported_by_check(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", formula="Nowhere!A1+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "A")),
        }]})
        proc = run_cli("check", self.book_path, self.recipe_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["touched"]["missing_sheets"],
                         {"S!A1": ["Nowhere"]})

    def test_diamond_dependency_evaluates_shared_node_once(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="10")
        sheet.cell("B1", formula="A1*2")
        sheet.cell("B2", formula="A1*3")
        sheet.cell("C1", formula="B1+B2")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "C"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        # A1, B1, B2, C1 -- each formula exactly once.
        self.assertEqual(payload["budget"]["formula_evaluations"], 3)
        row = read_jsonl(self.output_path)[0]
        self.assertEqual(row["cells"]["v"]["value"], "50")

    def test_shared_node_across_output_rows_is_evaluated_once(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        for row in range(1, 11):
            sheet.cell(f"A{row}", value="5")
            sheet.cell(f"B{row}", formula=f"A{row}*$A$1")
        sheet.cell("C1", formula="SUM(B1:B10)")
        self.save(builder)
        self.recipe({"sheets": [
            {"sheet": "S", "first_row": 1, "last_row": 10,
             "fields": fields(("v", "B"))},
            {"sheet": "S", "first_row": 1, "last_row": 1,
             "fields": fields(("t", "C"))},
        ]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        # B1..B10 and C1 = 11 formula cells, no repeats.
        self.assertEqual(payload["budget"]["formula_evaluations"], 11)
        rows = read_jsonl(self.output_path)
        self.assertEqual(rows[-1]["cells"]["t"]["value"], "250")

    def test_text_and_bool_in_arithmetic_are_value_errors(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", kind="s", value=str(builder.shared_string("2")))
        sheet.cell("A2", kind="b", value="1")
        sheet.cell("B1", formula="A1+1")
        sheet.cell("B2", formula="A2+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 2,
            "fields": fields(("v", "B"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["v"] for r in read_jsonl(self.output_path)]
        self.assertEqual(cells[0]["type"], "#VALUE!")
        self.assertEqual(cells[1]["type"], "#VALUE!")

    def test_concatenation_is_unsupported_job_failure(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="1")
        sheet.cell("B1", formula="A1&\"x\"")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "B"),),
        }]})
        for mode in ("annotate", "strict"):
            proc = self.recalc(mode=mode)
            self.assertEqual(proc.returncode, 1, mode)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error"], "workbook_unsupported")
            self.assertEqual(payload["construct"], "&")
            self.assertFalse(
                __import__("os").path.exists(self.output_path)
            )

    def test_array_formula_is_rejected_with_location(self):
        import zipfile

        builder = self.builder()
        builder.sheet("S").cell("A1", value="1")
        self.save(builder)
        with zipfile.ZipFile(self.book_path) as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        entries["xl/worksheets/worksheet1.xml"] = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData><row r="1">'
            '<c r="A1"><f t="array" ref="A1">1+1</f><v>2</v></c>'
            "</row></sheetData></worksheet>"
        ).encode()
        with zipfile.ZipFile(self.book_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
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
        self.assertEqual(payload["construct"], "array formula")

    def test_range_as_scalar_is_unsupported(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A1", value="1")
        sheet.cell("A2", value="2")
        sheet.cell("C1", formula="A1:A2+1")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 1,
            "fields": fields(("v", "C"),),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_unsupported")
        self.assertIn("range", payload["message"].lower())


if __name__ == "__main__":
    unittest.main()
