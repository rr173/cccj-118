"""The packaged hand-computed demo, asserted value by value."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DEMO = os.path.join(ROOT, "demo", "windowing")
INPUT = os.path.join(DEMO, "hand_example.jsonl")
DECLARATION = os.path.join(DEMO, "declaration.json")


def run_cli(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True, env=env)


class HandExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="window-hand-")
        cls.output = os.path.join(cls.work, "out.jsonl")
        proc = run_cli("window", "run", INPUT, cls.output,
                       "--declaration", DECLARATION)
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout + proc.stderr)
        with open(cls.output, encoding="utf-8") as handle:
            cls.rows = [json.loads(line) for line in handle if line.strip()]

    def test_row_count_and_delivery_order(self):
        self.assertEqual([row["source_row"] for row in self.rows], [1, 2, 3])
        # Source order preserved, nested object and big integer untouched.
        self.assertEqual(self.rows[2]["row"]["nested"]["big"], 9007199254740993)

    def test_ranking_and_ties(self):
        computed = [row["computed"] for row in self.rows]
        self.assertEqual([c["row_number"] for c in computed], [1, 2, 3])
        self.assertEqual([c["rank"] for c in computed], [1, 1, 3])
        self.assertEqual([c["dense_rank"] for c in computed], [1, 1, 2])

    def test_lag_lead_keeps_null_value_references(self):
        computed = [row["computed"] for row in self.rows]
        self.assertIsNone(computed[0]["lag"])
        self.assertEqual(computed[0]["lead"],
                         {"source_row": 2, "value": None})
        # k=1 lag of row 3 points at row 2 whose value is null, and that is
        # still a valid reference (no skipping).
        self.assertEqual(computed[2]["lag"],
                         {"source_row": 2, "value": None})
        self.assertIsNone(computed[2]["lead"])

    def test_range_self_none_includes_the_whole_tie(self):
        first = self.rows[0]["computed"]["windows"]["range_self_none"]
        self.assertEqual(first, {
            "count_all": 2, "count_value": 1,
            "sum": 5, "min": 5, "max": 5,
        })

    def test_range_self_group_becomes_empty(self):
        for row in self.rows[:2]:
            metrics = row["computed"]["windows"]["range_self_group"]
            self.assertEqual(metrics, {
                "count_all": 0, "count_value": 0,
                "sum": None, "min": None, "max": None,
            })

    def test_rows_around_current_excludes_only_current(self):
        second = self.rows[1]["computed"]["windows"]["rows_around_current"]
        self.assertEqual(second, {
            "count_all": 2, "count_value": 2,
            "sum": 3, "min": -2, "max": 5,
        })

    def test_rows_future_beyond_group_is_empty_not_clamped(self):
        third = self.rows[2]["computed"]["windows"]["rows_future"]
        self.assertEqual(third["count_all"], 0)
        self.assertIsNone(third["sum"])


if __name__ == "__main__":
    unittest.main()
