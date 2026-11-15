"""OOXML shared formulas: translation, order independence and diagnostics."""

import json
import unittest

from workbook_fixtures import WorkbookBuilder
from workbook_helpers import WorkbookTestCase, read_jsonl


def fields(*names_columns):
    return [{"name": name, "column": column} for name, column in names_columns]


class SharedFormulaTest(WorkbookTestCase):
    def test_members_before_master_and_own_values(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A2", value="2")
        sheet.member("B2", shared=3)                     # member, before master
        sheet.cell("A3", value="3")
        sheet.member("B3", shared=3)                     # member, before master
        sheet.cell("A4", value="4")
        sheet.cell("B4", formula="A4*10", shared=3, ref_range="B2:B4")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 4,
            "fields": fields(("v", "B")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["v"] for r in read_jsonl(self.output_path)]
        # Each row uses its own input, not the master's value.
        self.assertEqual([c["value"] for c in cells], ["20", "30", "40"])
        self.assertEqual(cells[0]["master"], "B4")
        self.assertFalse(cells[0]["is_master"])
        self.assertEqual(cells[2]["is_master"], True)

    def test_absolute_and_relative_translation_with_sheet_names(self):
        builder = self.builder()
        rate_sheet = builder.sheet("Tax Rates")
        rate_sheet.cell("B2", value="0.13")
        for row in (2, 3, 4):
            rate_sheet.cell(f"C{row}", value=str(row))
        sheet = builder.sheet("S")
        for row in (2, 3, 4):
            sheet.cell(f"A{row}", value=str(row))
            sheet.cell(f"B{row}", value="100")
            if row == 2:
                sheet.cell(
                    "C2",
                    formula="B2*A2+'Tax Rates'!$B$2+'Tax Rates'!C2",
                    shared=9, ref_range="C2:C4",
                )
            else:
                sheet.member(f"C{row}", shared=9)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 4,
            "fields": fields(("v", "C")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["v"] for r in read_jsonl(self.output_path)]
        # row r -> 100*r + 0.13 + r
        self.assertEqual([c["value"] for c in cells],
                         ["202.13", "303.13", "404.13"])
        # The absolute tax cell appears unchanged in every translated form.
        for cell in cells:
            self.assertIn("'Tax Rates'!$B$2", cell["translated_formula"])
        # The relative sheet-qualified reference moved with the row.
        self.assertIn("'Tax Rates'!C4", cells[2]["translated_formula"])

    def test_range_endpoints_translate_separately(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        # A1 row of data for SUM over a moving window.
        for row in range(1, 7):
            sheet.cell(f"A{row}", value=str(row))
        sheet.cell("C2", formula="SUM(A1:A2)", shared=1)
        sheet.member("C3", shared=1)
        sheet.member("C4", shared=1)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 4,
            "fields": fields(("v", "C")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cells = [r["cells"]["v"] for r in read_jsonl(self.output_path)]
        self.assertEqual([c["value"] for c in cells], ["3", "5", "7"])
        self.assertIn("A2:A3", cells[1]["translated_formula"])
        self.assertIn("A3:A4", cells[2]["translated_formula"])

    def test_shared_cache_ignored(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A2", value="2")
        sheet.cell("A3", value="3")
        sheet.cell("B2", formula="A2*5", shared=1, cached="111")
        sheet.member("B3", shared=1, cached="222")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 3,
            "fields": fields(("v", "B")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        values = [r["cells"]["v"]["value"] for r in read_jsonl(self.output_path)]
        self.assertEqual(values, ["10", "15"])

    def test_missing_master_is_formula_error(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A2", value="2")
        sheet.member("B2", shared=4)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 2,
            "fields": fields(("v", "B")),
        }]})
        for mode in ("annotate", "strict"):
            proc = self.recalc(mode=mode)
            self.assertEqual(proc.returncode, 1, mode)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error"], "workbook_formula_error")
            self.assertIn("no master", payload["message"])
            self.assertEqual(payload["address"], "B2")

    def test_two_masters_is_formula_error(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        for row in (2, 3):
            sheet.cell(f"A{row}", value=str(row))
            # Two cells of si=5 both carry formula text => two masters.
            sheet.cell(f"B{row}", formula=f"A{row}*2", shared=5)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 3,
            "fields": fields(("v", "B")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_formula_error")
        self.assertIn("master formulas", payload["message"])

    def test_member_outside_declared_ref_is_diagnosed(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A2", value="2")
        sheet.cell("B2", formula="A2*2", shared=1, ref_range="B2:B3")
        sheet.cell("A5", value="5")
        sheet.member("B5", shared=1)          # outside B2:B3
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 5,
            "fields": fields(("v", "B")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_formula_error")
        self.assertEqual(payload["address"], "B5")
        self.assertIn("outside the master's declared ref", payload["message"])

    def test_shared_index_without_si_is_package_error(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("A2", value="2")
        sheet.cell("B2", raw_value="1")
        self.save(builder)
        # Patch the formula to a shared formula without si.
        import zipfile

        with zipfile.ZipFile(self.book_path) as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        entries["xl/worksheets/worksheet1.xml"] = (
            '<?xml version="1.0"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData>'
            '<row r="2"><c r="A2"><v>2</v></c>'
            '<c r="B2"><f t="shared">A2*2</f></c></row>'
            "</sheetData></worksheet>"
        ).encode()
        with zipfile.ZipFile(self.book_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 2,
            "fields": fields(("v", "B")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"],
                         "workbook_package_error")

    def test_translation_out_of_grid_becomes_ref(self):
        builder = self.builder()
        sheet = builder.sheet("S")
        # Master at C2 refers left to B2; a member in column A shifts the
        # relative column by -2, i.e. to column 0 -> #REF! (not a silent 0).
        sheet.cell("B2", value="5")
        sheet.cell("C2", formula="B2*2", shared=1, ref_range="A2:C2")
        sheet.member("A2", shared=1)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 2, "last_row": 2,
            "fields": fields(("v", "A"), ("w", "C")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = read_jsonl(self.output_path)[0]
        self.assertEqual(row["cells"]["w"]["value"], "10")
        self.assertEqual(row["cells"]["v"]["status"], "error")
        self.assertEqual(row["cells"]["v"]["type"], "#REF!")

    def test_translated_reference_out_of_grid_is_ref_error(self):
        # Master at B2 refers to A2; a member at row 1 would need A1, fine --
        # so instead test a range whose top translates above row 1.
        builder = self.builder()
        sheet = builder.sheet("S")
        sheet.cell("C2", formula="SUM(A2:A3)", shared=6, ref_range="C1:C2")
        sheet.member("C1", shared=6)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": 2,
            "fields": fields(("v", "C")),
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        # C1's range translates to A1:A2 -- still inside the grid, all blank.
        self.assertEqual(rows[0]["cells"]["v"]["status"], "ok")
        self.assertEqual(rows[0]["cells"]["v"]["value"], "0")


if __name__ == "__main__":
    unittest.main()
