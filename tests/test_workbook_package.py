"""OOXML relationship handling and container fault diagnostics."""

import json
import os
import unittest

from workbook_fixtures import DEFAULT_PARTS, WorkbookBuilder, write_raw_parts
from workbook_helpers import WorkbookTestCase, read_jsonl


SIMPLE_RECIPE = {
    "sheets": [{
        "sheet": "Sheet1", "first_row": 1, "last_row": 1,
        "fields": [{"name": "a", "column": "A"}],
    }]
}


class RelationshipTest(WorkbookTestCase):
    def test_sheet_parts_found_through_relationships_not_names(self):
        # The workbook names "Second" first but its relationship points at
        # worksheet_b.xml, while the physically-first part is worksheet_a.xml.
        builder = self.builder()
        first = builder.sheet("First")
        first.cell("A1", inline="first-value")
        second = builder.sheet("Second")
        second.cell("A1", inline="second-value")
        self.save(builder)
        # Rewrite workbook.xml to map sheet names to the opposite parts.
        import zipfile

        with zipfile.ZipFile(self.book_path) as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        entries["xl/workbook.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets>'
            '<sheet name="First" sheetId="1" r:id="rId2"/>'
            '<sheet name="Second" sheetId="2" r:id="rId1"/>'
            "</sheets></workbook>"
        ).encode()
        with zipfile.ZipFile(self.book_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        self.recipe({"sheets": [
            {"sheet": "First", "first_row": 1, "last_row": 1,
             "fields": [{"name": "a", "column": "A"}]},
            {"sheet": "Second", "first_row": 1, "last_row": 1,
             "fields": [{"name": "a", "column": "A"}]},
        ]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        self.assertEqual(rows[0]["sheet"], "First")
        self.assertEqual(rows[0]["cells"]["a"]["value"], "second-value")
        self.assertEqual(rows[1]["cells"]["a"]["value"], "first-value")

    def test_missing_workbook_part(self):
        write_raw_parts(self.book_path, drop=["xl/workbook.xml"])
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_package_error")
        self.assertIn("missing", payload["message"].lower())

    def test_missing_relationship_target(self):
        write_raw_parts(self.book_path, drop=["xl/worksheets/sheet1.xml"])
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("missing", payload["message"])

    def test_external_uri_relationship_rejected(self):
        write_raw_parts(self.book_path, {
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.'
                'org/officeDocument/2006/relationships/worksheet" '
                'Target="http://example.invalid/sheet1.xml" '
                'TargetMode="External"/></Relationships>'
            )
        })
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_package_error")
        self.assertIn("external", payload["message"].lower())

    def test_path_traversal_relationship_rejected(self):
        write_raw_parts(self.book_path, {
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.'
                'org/officeDocument/2006/relationships/worksheet" '
                'Target="../../../../etc/passwd"/></Relationships>'
            )
        })
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("escapes the package",
                      json.loads(proc.stdout)["message"])

    def test_absolute_network_path_rejected(self):
        write_raw_parts(self.book_path, {
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.'
                'org/officeDocument/2006/relationships/worksheet" '
                'Target="//server/share/sheet1.xml"/></Relationships>'
            )
        })
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)

    def test_cell_without_coordinate_is_refused_not_guessed(self):
        write_raw_parts(self.book_path, {
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData><row r="1">'
                '<c><v>7</v></c></row></sheetData></worksheet>'
            )
        })
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"],
                         "workbook_package_error")

    def test_row_without_r_is_legal_when_cells_carry_coordinates(self):
        # OOXML makes <row r=...> optional; the cell coordinate is the
        # authority.  What is refused is a *cell* without one, or a cell whose
        # coordinate contradicts its row.
        write_raw_parts(self.book_path, {
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData>'
                '<row><c r="A1"><v>7</v></c></row></sheetData></worksheet>'
            )
        })
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = __import__("workbook_helpers").read_jsonl(self.output_path)[0]
        self.assertEqual(row["cells"]["a"]["value"], "7")

    def test_cell_contradicting_its_row_is_refused(self):
        write_raw_parts(self.book_path, {
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData>'
                '<row r="2"><c r="A1"><v>7</v></c></row>'
                "</sheetData></worksheet>"
            )
        })
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_package_error")
        self.assertIn("inside <row", payload["message"])

    def test_malformed_xml_is_package_error(self):
        write_raw_parts(self.book_path, {
            "xl/worksheets/sheet1.xml": b"<worksheet><sheetData><row>"
        })
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_package_error")
        self.assertIn("malformed XML", payload["message"])

    def test_doctype_is_refused(self):
        write_raw_parts(self.book_path, {
            "xl/worksheets/sheet1.xml": (
                b'<?xml version="1.0"?><!DOCTYPE worksheet ['
                b'<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                b'<worksheet xmlns="http://schemas.openxmlformats.org/'
                b'spreadsheetml/2006/main"><sheetData/></worksheet>'
            )
        })
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_package_error")
        self.assertIn("document type", payload["message"].lower())

    def test_not_a_zip(self):
        with open(self.book_path, "wb") as handle:
            handle.write(b"this is not a workbook")
        self.recipe(SIMPLE_RECIPE)
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"],
                         "workbook_package_error")

    def test_case_colliding_sheet_names_rejected(self):
        builder = self.builder()
        builder.sheet("Data").cell("A1", value="1")
        builder.sheet("data").cell("A1", value="2")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "Data", "first_row": 1, "last_row": 1,
            "fields": [{"name": "a", "column": "A"}],
        }]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_package_error")
        self.assertIn("case folding", payload["message"])

    def test_output_cannot_overwrite_inputs(self):
        builder = self.builder()
        builder.sheet("Sheet1").cell("A1", value="1")
        self.save(builder)
        self.recipe(SIMPLE_RECIPE)
        from workbook_helpers import run_cli

        proc = run_cli("recalc", self.book_path, self.recipe_path,
                       self.book_path, "--mode", "annotate")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("overwrite", payload["message"])
        # The workbook is untouched.
        import zipfile

        with zipfile.ZipFile(self.book_path) as archive:
            self.assertIn("xl/workbook.xml", archive.namelist())

    def test_check_reports_sheets_and_touched_counts(self):
        builder = self.builder()
        sheet = builder.sheet("Sheet1")
        sheet.cell("A1", value="2")
        sheet.cell("A2", value="3")
        sheet.cell("B1", formula="A1+A2")
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "Sheet1", "first_row": 1, "last_row": 2,
            "fields": [{"name": "total", "column": "B"}],
        }]})
        from workbook_helpers import run_cli

        proc = run_cli("check", self.book_path, self.recipe_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["sheets"][0]["name"], "Sheet1")
        self.assertEqual(payload["selections"][0]["rows"], 2)
        self.assertEqual(payload["selections"][0]["formula_cells"], 1)
        # A2 is blank as a *selected* coordinate, A1 is a constant.
        self.assertEqual(payload["selections"][0]["constant_cells"], 0)
        self.assertEqual(payload["selections"][0]["blank_cells"], 1)
        touched = payload["touched"]
        self.assertEqual(touched["by_sheet"]["Sheet1"]["formula_cells"], 1)
        self.assertEqual(touched["by_sheet"]["Sheet1"]["constant_cells"], 2)


if __name__ == "__main__":
    unittest.main()
