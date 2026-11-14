"""Randomized cross-check of the disk-backed engine vs. the naive reference.

Each case generates a declaration (a mix of ROWS/RANGE frames, positive and
negative offsets and all three exclusions) and a small JSONL stream with
interleaved partitions, out-of-order timestamps, duplicate records, null
values and arbitrary-precision integers.  The product output must match
``tests.reference_window`` field-for-field while running with deliberately
tiny batch/spill settings so the SQLite spill path is exercised on small
groups.
"""

import json
import os
import random
import tempfile
import unittest

from distagg.windowing.declaration import parse_declaration
from distagg.windowing.engine import WindowConfig, run_window_job

from reference_window import reference_run

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


def make_case(seed: int):
    rng = random.Random(seed)
    n_partition_fields = rng.randint(1, 3)
    partition_values_pool = [None, "alpha", "beta", "gamma", "日本語", ""]
    exclusions = ["none", "current", "group"]
    modes = ["ROWS", "RANGE"]

    windows = {}
    for index in range(rng.randint(2, 5)):
        mode = rng.choice(modes)
        if mode == "ROWS":
            start = rng.randint(-4, 1)
            end = rng.randint(0, 4)
        else:
            start = rng.randint(-50, 0)
            end = rng.randint(0, 50)
        if start > end:
            start, end = end, start
        windows[f"w{index}"] = {
            "mode": mode,
            "start": start,
            "end": end,
            "exclude": rng.choice(exclusions),
        }

    declaration = {
        "partition_by": [f"g{i}" for i in range(n_partition_fields)],
        "order_by": "ts",
        "value": "amount",
        "lag_lead_k": rng.randint(1, 4),
        "windows": windows,
    }

    rows = []
    for source_row in range(1, rng.randint(20, 80) + 1):
        obj = {}
        for i in range(n_partition_fields):
            obj[f"g{i}"] = rng.choice(partition_values_pool)
        # Mostly small timestamps, occasionally near the int64 limits.
        roll = rng.random()
        if roll < 0.05:
            obj["ts"] = rng.choice([INT64_MIN, INT64_MAX,
                                    INT64_MIN + 1, INT64_MAX - 1])
        else:
            obj["ts"] = rng.randint(-100, 100)
        if rng.random() < 0.25:
            obj["amount"] = None
        else:
            # Mix small values, negatives and numbers far beyond 64 bits.
            obj["amount"] = rng.choice([
                rng.randint(-1000, 1000),
                rng.randint(-10, 10),
                (10 ** rng.randint(19, 40)) * rng.choice([-1, 1]),
            ])
        obj["payload"] = {"source": source_row, "kept": [1, 2, 3]}
        rows.append(obj)

    # Insert exact duplicates (still two records) and shuffle source order.
    if rows and rng.random() < 0.5:
        rows.append(dict(rows[rng.randrange(len(rows))]))
    rng.shuffle(rows)
    return declaration, rows


class RandomCrossCheckTest(unittest.TestCase):
    def test_many_random_cases(self):
        work = tempfile.mkdtemp(prefix="window-random-")
        configs = [
            WindowConfig(batch_size=4, spill_threshold=2,
                         sqlite_cache_size_kib=256),
            WindowConfig(batch_size=1000, spill_threshold=512),
        ]
        for seed in range(40):
            with self.subTest(seed=seed):
                declaration_data, rows = make_case(seed)
                in_path = os.path.join(work, f"case-{seed}.jsonl")
                out_path = os.path.join(work, f"case-{seed}.out.jsonl")
                with open(in_path, "w", encoding="utf-8") as handle:
                    for obj in rows:
                        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")

                expected = reference_run(in_path, declaration_data)
                config = configs[seed % 2]
                stats = run_window_job(
                    in_path, out_path, parse_declaration(declaration_data),
                    config,
                )
                self.assertEqual(stats.rows_read, len(rows))
                with open(out_path, encoding="utf-8") as handle:
                    actual = [json.loads(line) for line in handle
                              if line.strip()]
                self.assertEqual(
                    [row["source_row"] for row in actual],
                    list(range(1, len(rows) + 1)),
                )
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
