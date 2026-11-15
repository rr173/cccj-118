"""The packaged supplier-cost handover: asserted value by value.

The workbook is rebuilt from ``build_workbook.py`` (raw OOXML, not a
spreadsheet engine) and compared against ``hand_expected.json``, whose
numbers were derived by hand in the comments of that builder.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DEMO = os.path.join(ROOT, "demo", "workbook")
EXPECTED = os.path.join(DEMO, "hand_expected.json")

sys.path.insert(0, DEMO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_workbook  # noqa: E402
from workbook_helpers import read_jsonl  # noqa: E402


def run_cli(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "distagg", "workbook", *args],
        capture_output=True, text=True, env=env, cwd=tempfile.gettempdir(),
    )


class SupplierCostDemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="wb-demo-")
        cls.book = os.path.join(cls.work, "supplier_cost.xlsx")
        cls.recipe_path = os.path.join(cls.work, "recipe.json")
        build_workbook.build_demo(cls.book)
        with open(cls.recipe_path, "w", encoding="utf-8") as handle:
            json.dump(build_workbook.DEMO_RECIPE, handle)
        cls.output = os.path.join(cls.work, "out.jsonl")
        proc = run_cli("recalc", cls.book, cls.recipe_path, cls.output,
                       "--mode", "annotate")
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout + proc.stderr)
        cls.summary = json.loads(proc.stdout)
        with open(cls.output, encoding="utf-8") as handle:
            cls.rows = [json.loads(line) for line in handle if line.strip()]
        with open(EXPECTED, encoding="utf-8") as handle:
            cls.expected = json.load(handle)

    def test_row_order_follows_recipe_then_row_number(self):
        actual = [(row["sheet"], row["row"]) for row in self.rows]
        expected = [(item["sheet"], item["row"]) for item in self.expected["rows"]]
        self.assertEqual(actual, expected)

    def test_hand_computed_totals(self):
        by_row = {row["row"]: row for row in self.rows if row["sheet"] == "Costs"}
        for item in self.expected["rows"]:
            if item["sheet"] != "Costs":
                continue
            total = by_row[item["row"]]["cells"]["total"]
            if item.get("error"):
                self.assertEqual(total["status"], "error", item["row"])
                self.assertEqual(total["type"], item["error"])
                self.assertEqual(total["chain"], ["Costs!F9"])
            else:
                self.assertEqual(total["status"], "ok", item["row"])
                self.assertEqual(total["value"], item["total"], item["why"])

    def test_part_and_quantities(self):
        by_row = {row["row"]: row for row in self.rows if row["sheet"] == "Costs"}
        for item in self.expected["rows"]:
            if item["sheet"] != "Costs":
                continue
            cells = by_row[item["row"]]["cells"]
            self.assertEqual(cells["part"]["value"], item["part"])
            if "quantity" in item:
                self.assertEqual(cells["quantity"]["value"], item["quantity"])
            if "discount" in item and item["discount"] is not None:
                self.assertEqual(cells["discount"]["value"], item["discount"])

    def test_blank_discount_is_blank_not_zero_text(self):
        row6 = [r for r in self.rows if r["sheet"] == "Costs" and r["row"] == 6][0]
        self.assertEqual(row6["cells"]["discount"]["status"], "blank")
        self.assertNotIn("value", row6["cells"]["discount"])
        # Row 8's D8 cell does not exist at all in the XML: blank, not "".
        row8 = [r for r in self.rows if r["sheet"] == "Costs" and r["row"] == 8][0]
        self.assertEqual(row8["cells"]["discount"]["status"], "blank")
        self.assertEqual(row8["cells"]["discount"]["origin"], "blank")

    def test_stale_caches_are_not_used(self):
        by_row = {row["row"]: row for row in self.rows if row["sheet"] == "Costs"}
        # F2 cache says 999999, F5 says 999, F7 says 77.
        self.assertEqual(by_row[2]["cells"]["total"]["value"], "20.340")
        self.assertEqual(by_row[5]["cells"]["total"]["value"], "6.780")
        self.assertEqual(by_row[7]["cells"]["total"]["value"], "0")
        # F3 has no cached <v> and must still evaluate.
        self.assertEqual(by_row[3]["cells"]["total"]["value"], "13.560")

    def test_shared_formula_translation_is_per_row(self):
        by_row = {row["row"]: row for row in self.rows if row["sheet"] == "Costs"}
        member = by_row[4]["cells"]["total"]
        master = by_row[6]["cells"]["total"]
        self.assertEqual(member["origin"], "shared-formula")
        self.assertFalse(member["is_master"])
        self.assertEqual(member["master"], "F6")
        self.assertIn("B4*C4", member["translated_formula"])
        self.assertIn("$B$2", member["translated_formula"])
        self.assertTrue(master["is_master"])
        # Distinct per-row results, not the master's value copied down.
        self.assertNotEqual(member["value"], master["value"])

    def test_tax_rate_row_from_space_named_sheet(self):
        tax = [row for row in self.rows if row["sheet"] == "Tax Rates"][0]
        self.assertEqual(tax["cells"]["tax_rate"]["value"],
                         self.expected["tax_rate"])
        self.assertEqual(tax["cells"]["rate_label"]["value"], "VAT")

    def test_summary_counts(self):
        self.assertEqual(self.summary["rows_written"], 9)
        self.assertEqual(self.summary["error_fields"], 1)
        self.assertEqual(self.summary["blank_fields"], 2)
        self.assertEqual(self.summary["fields"], 39)
        self.assertEqual(self.summary["formula_cells"], 8)

    def test_strict_mode_refuses_to_publish(self):
        out = os.path.join(self.work, "strict.jsonl")
        proc = run_cli("recalc", self.book, self.recipe_path, out)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_strict_stop")
        self.assertIs(payload["complete"], False)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(payload["rows_written"], 7)
        partial = payload["partial_output"]
        self.assertEqual(len(read_jsonl(partial)), 7)

    def test_checked_in_fixture_matches_builder(self):
        checked_in = os.path.join(DEMO, "supplier_cost.xlsx")
        if not os.path.exists(checked_in):
            self.skipTest("checked-in fixture absent")
        rebuilt = os.path.join(self.work, "rebuilt.xlsx")
        build_workbook.build_demo(rebuilt)
        import zipfile

        with zipfile.ZipFile(checked_in) as left, zipfile.ZipFile(rebuilt) as right:
            self.assertEqual(sorted(left.namelist()), sorted(right.namelist()))
            for name in left.namelist():
                self.assertEqual(left.read(name), right.read(name), name)


if __name__ == "__main__":
    unittest.main()
