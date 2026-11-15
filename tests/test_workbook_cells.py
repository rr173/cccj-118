"""Cell decoding: shared/inline strings, blanks, booleans, errors, merges.

Every XML fixture here is assembled by hand; the expectations are literal.
"""

import decimal
import json
import os
import unittest

from workbook_fixtures import WorkbookBuilder, write_raw_parts
from workbook_helpers import WorkbookTestCase, read_jsonl, run_cli, write_recipe


def selection(first=1, last=5, fields=None, name="Sheet1"):
    return {
        "sheet": name,
        "first_row": first,
        "last_row": last,
        "fields": fields or [
            {"name": "a", "column": "A"},
            {"name": "b", "column": "B"},
        ],
    }


class CellDecodingTest(WorkbookTestCase):
    def test_shared_string_distinct_from_blank_and_inline_blank(self):
        builder = self.builder()
        sheet = builder.sheet("Sheet1")
        sheet.cell("A1", kind="s", value=str(builder.shared_string("hello")))
        sheet.cell("A6", kind="s", value=str(builder.shared_string(" ")))
        sheet.cell("A2", kind="s", value=str(builder.shared_string("")))
        sheet.cell("A3", inline="")
        # A4 is absent entirely; A5 has a cell element with no value.
        sheet.cell("A5")
        self.save(builder)
        self.recipe({"sheets": [selection(1, 6, [
            {"name": "a", "column": "A"}])]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        payloads = [row["cells"]["a"] for row in rows]
        self.assertEqual([p["status"] for p in payloads],
                         ["ok", "ok", "ok", "blank", "blank", "ok"])
        self.assertEqual(payloads[0]["value"], "hello")
        # A whitespace-only shared string is real text, not blank.
        self.assertEqual(payloads[5]["value"], " ")
        self.assertEqual(payloads[1]["value"], "")
        self.assertEqual(payloads[2]["value"], "")
        self.assertEqual(payloads[1]["origin"], "text")
        self.assertEqual(payloads[3]["origin"], "blank")
        self.assertNotIn("value", payloads[3])

    def test_numbers_are_raw_decimals_without_float(self):
        builder = self.builder()
        sheet = builder.sheet("Sheet1")
        sheet.cell("A1", value="9007199254740993")
        sheet.cell("A2", value="0.1234567890123456789012345678901234")
        sheet.cell("A3", value="-3.5")
        self.save(builder)
        self.recipe({"sheets": [selection(1, 3, [{"name": "a", "column": "A"}])]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        self.assertEqual([r["cells"]["a"]["value"] for r in rows], [
            "9007199254740993",
            "0.1234567890123456789012345678901234",
            "-3.5",
        ])

    def test_booleans_and_error_cells(self):
        builder = self.builder()
        sheet = builder.sheet("Sheet1")
        sheet.cell("A1", kind="b", value="1")
        sheet.cell("A2", kind="b", value="0")
        sheet.cell("A3", kind="e", value="#N/A")
        self.save(builder)
        self.recipe({"sheets": [selection(1, 3, [{"name": "a", "column": "A"}])]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        self.assertIs(rows[0]["cells"]["a"]["value"], True)
        self.assertIs(rows[1]["cells"]["a"]["value"], False)
        error = rows[2]["cells"]["a"]
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["type"], "#N/A")
        self.assertEqual(error["chain"], ["Sheet1!A3"])

    def test_styles_are_not_applied(self):
        # s="1" is a date style in real files; the raw serial number must be
        # delivered unchanged because this package does no date conversion.
        builder = self.builder()
        sheet = builder.sheet("Sheet1")
        sheet.cell("A1", value="45000", style="1")
        self.save(builder)
        self.recipe({"sheets": [selection(1, 1, [{"name": "a", "column": "A"}])]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = read_jsonl(self.output_path)[0]
        self.assertEqual(row["cells"]["a"]["value"], "45000")
        self.assertEqual(row["cells"]["a"]["origin"], "number")

    def test_merged_cells_only_anchor_has_value(self):
        builder = self.builder()
        sheet = builder.sheet("Sheet1")
        sheet.cell("A1", inline="anchor")
        # A stale cell under the merge must still read as blank.
        sheet.cell("B2", inline="stale")
        sheet.merge("A1:B2")
        sheet.cell("A4", inline="after")
        self.save(builder)
        self.recipe({"sheets": [selection(1, 4, [
            {"name": "a", "column": "A"},
            {"name": "b", "column": "B"},
        ])]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = read_jsonl(self.output_path)
        self.assertEqual(rows[0]["cells"]["a"]["value"], "anchor")
        for row in rows:
            for name in ("a", "b"):
                payload = row["cells"][name]
                if (row["row"], name) in ((1, "a"), (4, "a")):
                    continue
                self.assertEqual(payload["status"], "blank", (row["row"], name))
        self.assertEqual(rows[1]["cells"]["b"]["origin"], "merged-covered")
        self.assertEqual(rows[1]["cells"]["b"]["anchor"], "A1")
        self.assertEqual(rows[1]["cells"]["a"]["origin"], "merged-covered")
        self.assertEqual(rows[0]["cells"]["b"]["origin"], "merged-covered")
        # A coordinate inside the band with no <c> at all is plain blank,
        # *not* reported as merged-covered.
        self.assertEqual(rows[2]["cells"]["a"]["origin"], "blank")

    def test_shared_string_out_of_range_is_package_error(self):
        builder = self.builder()
        sheet = builder.sheet("Sheet1")
        sheet.cell("A1", kind="s", value="5")
        builder.shared_string("only")
        self.save(builder)
        self.recipe({"sheets": [selection(1, 1, [{"name": "a", "column": "A"}])]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_package_error")
        self.assertIn("out of range", payload["message"])

    def test_duplicate_cell_coordinate_is_package_error(self):
        builder = self.builder()
        sheet = builder.sheet("Sheet1")
        sheet.cell("A1", value="1")
        self.save(builder)
        # Rewrite the worksheet with the same coordinate twice.
        import zipfile

        target = self.book_path
        with zipfile.ZipFile(target) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        broken = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1"><v>1</v></c>'
            '<c r="A1"><v>2</v></c></row></sheetData></worksheet>'
        )
        entries["xl/worksheets/worksheet1.xml"] = broken.encode()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        self.recipe({"sheets": [selection(1, 1, [{"name": "a", "column": "A"}])]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_package_error")
        self.assertIn("duplicate cell coordinate", payload["message"])

    def test_invalid_numeric_text_is_package_error(self):
        write_raw_parts(self.book_path, {
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData><row r="1">'
                '<c r="A1"><v>not-a-number</v></c>'
                "</row></sheetData></worksheet>"
            )
        })
        self.recipe({"sheets": [selection(1, 1, [{"name": "a", "column": "A"}])]})
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"],
                         "workbook_package_error")


if __name__ == "__main__":
    unittest.main()
