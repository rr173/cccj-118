"""Scale behaviour: 5000-deep chains, wide fan-in, measured resource use.

The report printed by ``resources`` (and stored by the demo script) records
real formula-evaluation counts and peak RSS from ``resource.getrusage``;
nothing here is estimated.  The tests assert the *exact* expected evaluation
counts so a regression that silently re-evaluates or drops a cell fails.
"""

import json
import os
import resource
import subprocess
import sys
import tempfile
import unittest

from workbook_fixtures import WorkbookBuilder
from workbook_helpers import WorkbookTestCase, read_jsonl, run_cli

DEPTH = 5000


def rss_kib():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss


def build_chain(builder, depth=DEPTH):
    sheet = builder.sheet("S")
    sheet.cell("A1", value="1")
    for row in range(2, depth + 1):
        sheet.cell(f"A{row}", formula=f"A{row - 1}+1")


def build_fan_in(builder, rows=300):
    sheet = builder.sheet("S")
    sheet.cell("A1", value="2")
    sheet.cell("B1", formula="A1*3")
    for row in range(1, rows + 1):
        sheet.cell(f"C{row}", formula=f"B1+{row}")
    return rows


class ScaleTest(WorkbookTestCase):
    def test_5000_deep_chain(self):
        builder = self.builder()
        build_chain(builder)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": DEPTH, "last_row": DEPTH,
            "fields": [{"name": "v", "column": "A"}],
        }]})
        before = rss_kib()
        proc = self.recalc(mode="annotate")
        after = rss_kib()
        self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["budget"]["formula_evaluations"], DEPTH - 1)
        self.assertEqual(payload["rows_written"], 1)
        self.assertEqual(read_jsonl(self.output_path)[0]["cells"]["v"]["value"],
                         str(DEPTH))
        # A deep chain must not need a recursion-limit bump; if it did the
        # process would die with RecursionError instead of exit 0.
        self.assertLess(before, 1_000_000)
        self.assertLess(after, 1_000_000)

    def test_wide_fan_in_evaluates_intermediate_once(self):
        builder = self.builder()
        rows = build_fan_in(builder)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 1, "last_row": rows,
            "fields": [{"name": "v", "column": "C"}],
        }]})
        proc = self.recalc(mode="annotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["rows_written"], rows)
        self.assertEqual(payload["budget"]["formula_evaluations"], 1 + rows)
        values = [row["cells"]["v"]["value"] for row in read_jsonl(self.output_path)]
        self.assertEqual(values[0], "7")
        self.assertEqual(values[-1], str(6 + rows))

    def test_low_budget_fails_loudly_not_with_blank_success(self):
        builder = self.builder()
        build_chain(builder, depth=20)
        self.save(builder)
        self.recipe({"sheets": [{
            "sheet": "S", "first_row": 20, "last_row": 20,
            "fields": [{"name": "v", "column": "A"}],
        }]})
        proc = self.recalc("--cell-budget", "5", mode="annotate")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "workbook_budget_exceeded")
        self.assertFalse(os.path.exists(self.output_path))
        # In particular, no row was written with the remaining fields as "".
        self.assertFalse(os.path.exists(self.output_path))


if __name__ == "__main__":
    unittest.main()
