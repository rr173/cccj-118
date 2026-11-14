"""External cross-check using SQLite's own standard window functions.

The production implementation is forbidden from using SQL ``OVER`` / window
extensions; this *reference* program is explicitly allowed to use them and
exists only to independently validate the product on 64-bit-safe data.  It
loads the JSONL into plain tables, computes every metric with standard
window frames, and compares the result set to the product's output.

Values are restricted to the signed 64-bit range here so SQLite SUM can be
used directly; arbitrary-precision cases are covered hand-side by
``reference_window`` and the edge tests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from distagg.windowing.declaration import parse_declaration
from distagg.windowing.engine import WindowConfig, run_window_job

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


def _load_sqlite_reference(input_path: str, declaration: dict) -> dict:
    partition_fields = declaration["partition_by"]
    order_field = declaration["order_by"]
    value_field = declaration["value"]
    k = declaration["lag_lead_k"]
    conn = sqlite3.connect(":memory:")
    columns = ["source_row INTEGER", "ts INTEGER", "value INTEGER"]
    columns += [f"g{i} TEXT" for i in range(len(partition_fields))]
    conn.execute(f"CREATE TABLE rec ({', '.join(columns)})")
    rows = []
    with open(input_path, encoding="utf-8") as handle:
        for source_row, line in enumerate(
            (ln for ln in handle if ln.strip()), start=1
        ):
            obj = json.loads(line)
            rows.append((
                source_row, obj[order_field], obj[value_field],
                *[obj[name] for name in partition_fields],
            ))
    conn.executemany(
        f"INSERT INTO rec VALUES ({', '.join('?' * (3 + len(partition_fields)))})",
        rows,
    )
    part_partition = ", ".join(f"g{i}" for i in range(len(partition_fields)))

    # Ranking: rank/dense_rank peer ONLY on timestamp (the source_row tie
    # breaker must not count as a distinct rank level), while row_number uses
    # the full deterministic (ts, source_row) ordering.
    ranking = {}
    query = f"""
        SELECT source_row,
               ROW_NUMBER() OVER w_full AS rn,
               RANK() OVER w_ts AS rk,
               DENSE_RANK() OVER w_ts AS drk,
               LAG(source_row, {k}) OVER w_full AS lag_sr,
               LAG(value, {k}) OVER w_full AS lag_val,
               LEAD(source_row, {k}) OVER w_full AS lead_sr,
               LEAD(value, {k}) OVER w_full AS lead_val
        FROM rec
        WINDOW
            w_full AS (PARTITION BY {part_partition} ORDER BY ts, source_row),
            w_ts   AS (PARTITION BY {part_partition} ORDER BY ts)
    """
    for (source_row, rn, rk, drk, lag_sr, lag_val, lead_sr,
         lead_val) in conn.execute(query):
        ranking[source_row] = {
            "row_number": rn,
            "rank": rk,
            "dense_rank": drk,
            "lag": (None if lag_sr is None
                    else {"source_row": lag_sr, "value": lag_val}),
            "lead": (None if lead_sr is None
                     else {"source_row": lead_sr, "value": lead_val}),
        }

    windows_out = {name: {} for name in declaration["windows"]}
    for name, spec in declaration["windows"].items():
        frame = _sqlite_frame(spec)
        # All five aggregates over the FULL table: COUNT(*)/SUM/MIN/MAX
        # naturally ignore NULL values, so the frame positions match the
        # product's row-based membership exactly.
        sql = f"""
            SELECT source_row,
                   COUNT(*) OVER w AS c_all,
                   COUNT(value) OVER w AS c_val,
                   SUM(value) OVER w AS s,
                   MIN(value) OVER w AS mn,
                   MAX(value) OVER w AS mx
            FROM rec
            WINDOW w AS ({frame})
        """
        for source_row, c_all, c_val, total, mn, mx in conn.execute(sql):
            windows_out[name][source_row] = {
                "count_all": c_all,
                "count_value": c_val,
                "sum": total,
                "min": mn,
                "max": mx,
            }

    result = {}
    for source_row in ranking:
        result[source_row] = {
            "row_number": ranking[source_row]["row_number"],
            "rank": ranking[source_row]["rank"],
            "dense_rank": ranking[source_row]["dense_rank"],
            "lag": ranking[source_row]["lag"],
            "lead": ranking[source_row]["lead"],
            "windows": {
                name: windows_out[name][source_row]
                for name in declaration["windows"]
            },
        }
    conn.close()
    return result


def _sqlite_frame(spec: dict) -> str:
    # Peers are defined by timestamp alone.  A frame without exclusion is
    # insensitive to the intra-peer ordering, so (ts, source_row) gives the
    # same members; but EXCLUDE CURRENT ROW / EXCLUDE GROUP resolve against
    # the ordering peer, which must be keyed on ts only to match the spec.
    if spec["exclude"] == "none":
        order = "ts, source_row"
    else:
        order = "ts"
    if spec["mode"] == "ROWS":
        partition = f"PARTITION BY g0 ORDER BY {order}"
        bounds = _rows_bounds(spec)
        return f"{partition} ROWS BETWEEN {bounds}"
    # SQLite RANGE offsets require exactly one ORDER BY expression.
    partition = "PARTITION BY g0 ORDER BY ts"
    bounds = _range_bounds(spec)
    return f"{partition} RANGE BETWEEN {bounds}"


def _rows_bounds(spec: dict) -> str:
    def edge(offset: int, side: str) -> str:
        if offset == 0:
            return "CURRENT ROW"
        direction = "PRECEDING" if offset < 0 else "FOLLOWING"
        return f"{abs(offset)} {direction}"

    left = edge(spec["start"], "left")
    right = edge(spec["end"], "right")
    base = f"{left} AND {right}"
    return _apply_exclude(base, spec["exclude"])


def _range_bounds(spec: dict) -> str:
    def edge(offset: int) -> str:
        if offset == 0:
            return "CURRENT ROW"
        direction = "PRECEDING" if offset < 0 else "FOLLOWING"
        return f"{abs(offset)} {direction}"

    base = f"{edge(spec['start'])} AND {edge(spec['end'])}"
    return _apply_exclude(base, spec["exclude"])


def _apply_exclude(frame_tail: str, exclude: str) -> str:
    if exclude == "none":
        return frame_tail
    if exclude == "current":
        return frame_tail + " EXCLUDE CURRENT ROW"
    return frame_tail + " EXCLUDE GROUP"


class SqliteWindowCrossCheckTest(unittest.TestCase):
    def test_matches_sqlite_window_functions(self):
        work = tempfile.mkdtemp(prefix="window-sqlite-")
        declaration = {
            "partition_by": ["g"],
            "order_by": "ts",
            "value": "amount",
            "lag_lead_k": 2,
            "windows": {
                "rows_wide": {"mode": "ROWS", "start": -3, "end": 2,
                              "exclude": "none"},
                "rows_current": {"mode": "ROWS", "start": -3, "end": 2,
                                 "exclude": "current"},
                "rows_group": {"mode": "ROWS", "start": -3, "end": 2,
                               "exclude": "group"},
                "range_self": {"mode": "RANGE", "start": 0, "end": 0,
                               "exclude": "none"},
                "range_group": {"mode": "RANGE", "start": -5, "end": 5,
                                "exclude": "group"},
            },
        }
        in_path = os.path.join(work, "in.jsonl")
        out_path = os.path.join(work, "out.jsonl")
        # Deterministic but varied 64-bit-safe data with ties and nulls.
        import random
        rng = random.Random(20261114)
        with open(in_path, "w", encoding="utf-8") as handle:
            for i in range(1, 400):
                obj = {
                    # Non-null partitions only: SQL treats NULLs as distinct
                    # groups whereas the spec groups NULL with NULL; that
                    # rule is covered by the dedicated edge-case tests.
                    "g": rng.choice(["p", "q", "r"]),
                    "ts": rng.randint(-1000, 1000),
                    "amount": (None if rng.random() < 0.2
                               else rng.randint(-10000, 10000)),
                    "seq": i,
                }
                handle.write(json.dumps(obj) + "\n")

        reference = _load_sqlite_reference(in_path, declaration)
        run_window_job(in_path, out_path, parse_declaration(declaration),
                       WindowConfig(batch_size=37, spill_threshold=13))
        with open(out_path, encoding="utf-8") as handle:
            for line in handle:
                actual = json.loads(line)
                source_row = actual["source_row"]
                expected_computed = reference[source_row]
                self.assertEqual(
                    actual["computed"]["row_number"],
                    expected_computed["row_number"],
                )
                self.assertEqual(actual["computed"]["rank"],
                                 expected_computed["rank"])
                self.assertEqual(actual["computed"]["dense_rank"],
                                 expected_computed["dense_rank"])
                self.assertEqual(actual["computed"]["lag"],
                                 expected_computed["lag"])
                self.assertEqual(actual["computed"]["lead"],
                                 expected_computed["lead"])
                for name in declaration["windows"]:
                    self.assertEqual(
                        actual["computed"]["windows"][name],
                        expected_computed["windows"][name],
                        f"window {name} source_row {source_row}",
                    )


if __name__ == "__main__":
    unittest.main()
