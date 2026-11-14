"""Edge-case integration tests driven through the public API and scratch DB."""

import json
import os
import tempfile
import unittest

from distagg.windowing.declaration import parse_declaration
from distagg.windowing.engine import WindowConfig, run_window_job
from distagg.windowing.errors import WindowInputError

from reference_window import reference_run

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

SMALL_CONFIG = WindowConfig(batch_size=2, spill_threshold=1,
                            sqlite_cache_size_kib=128)

DECLARATION = {
    "partition_by": ["g"],
    "order_by": "ts",
    "value": "amount",
    "lag_lead_k": 5,
    "windows": {
        "whole": {"mode": "ROWS", "start": -100, "end": 100, "exclude": "none"},
        "whole_current": {"mode": "ROWS", "start": -100, "end": 100,
                          "exclude": "current"},
        "whole_group": {"mode": "RANGE", "start": -10 ** 20, "end": 10 ** 20,
                        "exclude": "group"},
        "after": {"mode": "ROWS", "start": 1, "end": 3, "exclude": "none"},
        "before": {"mode": "ROWS", "start": -3, "end": -1, "exclude": "none"},
    },
}


class EdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="window-edge-")

    def _run(self, rows, declaration=DECLARATION, config=SMALL_CONFIG):
        in_path = os.path.join(self.work, "in.jsonl")
        out_path = os.path.join(self.work, "out.jsonl")
        with open(in_path, "w", encoding="utf-8") as handle:
            for obj in rows:
                handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
        stats = run_window_job(in_path, out_path,
                               parse_declaration(declaration), config)
        with open(out_path, encoding="utf-8") as handle:
            actual = [json.loads(line) for line in handle if line.strip()]
        return in_path, out_path, stats, actual

    def test_empty_input_produces_empty_output(self):
        in_path = os.path.join(self.work, "empty.jsonl")
        out_path = os.path.join(self.work, "empty.out.jsonl")
        open(in_path, "w").close()
        stats = run_window_job(in_path, out_path, parse_declaration(DECLARATION))
        self.assertEqual((stats.rows_read, stats.rows_written, stats.groups),
                         (0, 0, 0))
        self.assertEqual(os.path.getsize(out_path), 0)

    def test_all_null_values(self):
        rows = [
            {"g": "a", "ts": 1, "amount": None},
            {"g": "a", "ts": 1, "amount": None},
            {"g": "a", "ts": 2, "amount": None},
        ]
        _in, _out, _stats, actual = self._run(rows)
        for row in actual:
            for metrics in row["computed"]["windows"].values():
                self.assertIn(metrics["count_all"], (0, 1, 2, 3))
                self.assertEqual(metrics["count_value"], 0)
                self.assertIsNone(metrics["sum"])
                self.assertIsNone(metrics["min"])
                self.assertIsNone(metrics["max"])
        # Whole group window still counts every member.
        self.assertEqual(actual[0]["computed"]["windows"]["whole"]["count_all"], 3)

    def test_arbitrary_precision_positive_and_negative(self):
        huge_pos = 10 ** 80 + 123
        huge_neg = -(10 ** 77) - 7
        rows = [
            {"g": "a", "ts": 1, "amount": huge_pos},
            {"g": "a", "ts": 2, "amount": huge_neg},
            {"g": "a", "ts": 3, "amount": None},
        ]
        _in, out_path, _stats, actual = self._run(rows)
        whole = [row["computed"]["windows"]["whole"] for row in actual]
        self.assertEqual(whole[0]["sum"], huge_pos + huge_neg)
        self.assertEqual(whole[0]["min"], huge_neg)
        self.assertEqual(whole[0]["max"], huge_pos)
        # Exact integer survives the output file as JSON integer.
        with open(out_path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(str(huge_pos), text)
        self.assertNotIn(str(float(huge_pos)), text)

    def test_timestamps_at_int64_limits_and_range_overflow_math(self):
        rows = [
            {"g": "a", "ts": INT64_MIN, "amount": 4},
            {"g": "a", "ts": INT64_MAX, "amount": 6},
        ]
        # Offsets whose endpoint arithmetic overflows int64; the Python
        # engine must compute t +/- offset with mathematical integers
        # instead of letting SQLite wrap.
        wide = 2 ** 64
        # 2^63 + 2: INT64_MAX minus this offset is -3; an int64-wrapping
        # subtraction would instead produce a large positive-looking value
        # and could incorrectly select the minimum row.
        overflowing = 2 ** 63 + 2
        declaration = {
            "partition_by": ["g"], "order_by": "ts", "value": "amount",
            "lag_lead_k": 1,
            "windows": {
                "huge_range": {"mode": "RANGE", "start": -wide,
                               "end": wide, "exclude": "none"},
                "past_min": {"mode": "RANGE", "start": -wide,
                             "end": 0, "exclude": "none"},
                # Mathematically [-3, -1] from INT64_MAX: no stored row lies
                # in it, and the arithmetic itself overflows int64.
                "negative_only": {"mode": "RANGE", "start": -overflowing,
                                  "end": -(overflowing - 2),
                                  "exclude": "none"},
                # From INT64_MAX the true lower bound is 2^63-1 > every
                # stored timestamp, so the frame is empty; wrapping the
                # addition would yield a negative number and include rows.
                "above_max": {"mode": "RANGE", "start": 2 ** 63,
                              "end": 2 ** 64, "exclude": "none"},
            },
        }
        _in, out_path, _stats, actual = self._run(rows, declaration)
        # From INT64_MIN, [-2^64, +2^64] mathematically covers both rows.
        self.assertEqual(
            actual[0]["computed"]["windows"]["huge_range"]["count_all"], 2
        )
        # [-2^64, 0] from INT64_MIN reaches only the minimum row.
        self.assertEqual(
            actual[0]["computed"]["windows"]["past_min"]["count_all"], 1
        )
        # The mathematical window [-3, -1] excludes both stored timestamps;
        # a 64-bit wrap would mis-locate the interval and return rows.
        self.assertEqual(
            actual[1]["computed"]["windows"]["negative_only"]["count_all"], 0
        )
        # The same negative-only frame from INT64_MIN is entirely above the
        # minimum row, so it is empty there as well.
        self.assertEqual(
            actual[0]["computed"]["windows"]["negative_only"]["count_all"], 0
        )
        # Addition-overflow guard from INT64_MAX.
        self.assertEqual(
            actual[1]["computed"]["windows"]["above_max"]["count_all"], 0
        )
        # From INT64_MIN the same frame mathematically starts at 0, so only
        # the INT64_MAX row (2^63-1 <= 2^63) is a member; a 64-bit wrap of
        # the endpoint arithmetic would compute a different interval.
        self.assertEqual(
            actual[0]["computed"]["windows"]["above_max"]["count_all"], 1
        )

    def test_k_larger_than_group_gives_null_references(self):
        rows = [{"g": "a", "ts": i, "amount": i} for i in range(3)]
        _in, _out, _stats, actual = self._run(rows)
        for row in actual:
            self.assertIsNone(row["computed"]["lag"])
            self.assertIsNone(row["computed"]["lead"])

    def test_interleaved_groups_keep_identity_and_source_order(self):
        # Two groups interleaved in source order, shuffled timestamps, and a
        # fully duplicated record that must remain two distinct rows.
        rows = [
            {"g": "a", "txn": 1, "ts": 30, "amount": 1},
            {"g": "b", "txn": 2, "ts": 5, "amount": 10},
            {"g": "a", "txn": 3, "ts": 10, "amount": 2},
            {"g": "b", "txn": 4, "ts": 9, "amount": 20},
            {"g": "a", "txn": 5, "ts": 10, "amount": 3},
        ]
        duplicate = dict(rows[2])
        rows.append(duplicate)
        in_path, out_path, _stats, actual = self._run(rows)
        self.assertEqual([row["source_row"] for row in actual],
                         list(range(1, 7)))
        expected = reference_run(in_path, DECLARATION)
        self.assertEqual(actual, expected)
        # The duplicated source object appears twice and both copies survive.
        duplicated_rows = [row for row in actual
                           if row["row"].get("txn") == 3]
        self.assertEqual(len(duplicated_rows), 2)
        self.assertEqual({row["source_row"] for row in duplicated_rows}, {3, 6})

    def test_negative_frames_that_leave_the_group_are_empty(self):
        rows = [{"g": "a", "ts": i, "amount": i} for i in range(1, 4)]
        _in, _out, _stats, actual = self._run(rows)
        # ROWS [-3,-1] at the first position is fully outside the group.
        self.assertEqual(
            actual[0]["computed"]["windows"]["before"]["count_all"], 0
        )
        # ROWS [1,3] at the last position is fully outside too.
        self.assertEqual(
            actual[-1]["computed"]["windows"]["after"]["count_all"], 0
        )

    def test_last_line_bad_type_fails_and_leaves_no_partial_output(self):
        in_path = os.path.join(self.work, "bad.jsonl")
        out_path = os.path.join(self.work, "bad.out.jsonl")
        with open(in_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"g": "a", "ts": 1, "amount": 1}) + "\n")
            handle.write(json.dumps({"g": "a", "ts": 2, "amount": 2}) + "\n")
            handle.write('{"g": "a", "ts": "not-int", "amount": 3}\n')
        with self.assertRaises(WindowInputError) as ctx:
            run_window_job(in_path, out_path, parse_declaration(DECLARATION),
                           SMALL_CONFIG)
        self.assertEqual(ctx.exception.line_number, 3)
        self.assertFalse(os.path.exists(out_path))

    def test_blank_line_reports_physical_line(self):
        in_path = os.path.join(self.work, "blank.jsonl")
        out_path = os.path.join(self.work, "blank.out.jsonl")
        with open(in_path, "w", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaises(WindowInputError) as ctx:
            run_window_job(in_path, out_path, parse_declaration(DECLARATION))
        self.assertEqual(ctx.exception.line_number, 1)

    def test_null_partitions_group_together_and_components_stay_bound(self):
        rows = [
            {"g": None, "ts": 1, "amount": 7},
            {"g": None, "ts": 1, "amount": 8},
            {"g": "x", "ts": 1, "amount": 9},
        ]
        _in, _out, _stats, actual = self._run(rows)
        null_group = [r for r in actual if r["row"]["g"] is None]
        self.assertEqual(
            null_group[0]["computed"]["windows"]["whole"]["count_all"], 2
        )
        x_group = [r for r in actual if r["row"]["g"] == "x"]
        self.assertEqual(
            x_group[0]["computed"]["windows"]["whole"]["count_all"], 1
        )


if __name__ == "__main__":
    unittest.main()
