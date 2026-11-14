"""Independent, deliberately naive reference for the offline window job.

This module shares *no* implementation with ``distagg.windowing``: no SQLite,
no monotonic deques, no streaming cursors.  It loads the whole stream into
Python lists (acceptable for small generated test inputs) and evaluates every
frame by directly enumerating the frame members exactly as the specification
defines them, using unbounded Python integers throughout.

The product test suite compares the product's on-disk output against this
reference on randomized small datasets.  A second, optional cross-check using
SQLite's standard window functions lives in
``tests/test_window_sqlite_crosscheck.py`` and is restricted to 64-bit-safe
values; this reference handles arbitrary precision itself.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


def _strict_load(path: str):
    records = []
    with open(path, "rb") as handle:
        for line_number, raw in enumerate(
            (line.rstrip(b"\n") for line in handle), start=1
        ):
            if not raw or not raw.strip():
                raise AssertionError(f"reference got blank line at {line_number}")
            records.append((line_number, json.loads(raw.decode("utf-8"))))
    return records


def reference_run(input_path: str, declaration: Dict[str, Any]) -> List[dict]:
    """Return the expected output objects in source order."""
    records = _strict_load(input_path)
    partition_fields = declaration["partition_by"]
    order_field = declaration["order_by"]
    value_field = declaration["value"]
    k = declaration["lag_lead_k"]
    window_specs = declaration["windows"]

    groups: Dict[Tuple, List[dict]] = {}
    for source_row, obj in records:
        key = tuple(obj[name] for name in partition_fields)
        groups.setdefault(key, []).append(
            {
                "source_row": source_row,
                "obj": obj,
                "ts": obj[order_field],
                "value": obj[value_field],
            }
        )

    expected_by_source: Dict[int, dict] = {}
    # Group processing order is irrelevant: results are delivered in source
    # order via expected_by_source, so no ordering of null-containing keys
    # is needed here.
    for key in groups:
        members = sorted(
            groups[key], key=lambda item: (item["ts"], item["source_row"])
        )
        size = len(members)
        for index, item in enumerate(members):
            item["pos"] = index + 1
        # Tie groups (same timestamp).
        peer_starts: List[int] = []
        dense_lookup: Dict[int, int] = {}
        dense = 0
        index = 0
        while index < size:
            j = index
            while j + 1 < size and members[j + 1]["ts"] == members[index]["ts"]:
                j += 1
            dense += 1
            start_pos = index + 1
            end_pos = j + 1
            peer_starts.append((start_pos, end_pos))
            for p in range(start_pos, end_pos + 1):
                dense_lookup[p] = dense
            index = j + 1

        min_ts = members[0]["ts"]
        max_ts = members[-1]["ts"]

        for idx in range(size):
            pos = idx + 1
            item = members[idx]
            ts = item["ts"]
            peer_start, peer_end = next(
                (s, e) for (s, e) in peer_starts if s <= pos <= e
            )

            def value_at(position: int):
                if position < 1 or position > size:
                    return None
                target = members[position - 1]
                return {
                    "source_row": target["source_row"],
                    "value": target["value"],
                }

            windows_out: Dict[str, dict] = {}
            for name, spec in window_specs.items():
                if spec["mode"] == "ROWS":
                    lo = pos + spec["start"]
                    hi = pos + spec["end"]
                    if hi < 1 or lo > size:
                        chosen: List[dict] = []
                    else:
                        lo = max(1, lo)
                        hi = min(size, hi)
                        chosen = list(members[lo - 1:hi])
                else:
                    t_lo = ts + spec["start"]
                    t_hi = ts + spec["end"]
                    if t_hi < min_ts or t_lo > max_ts:
                        chosen = []
                    else:
                        chosen = [
                            member
                            for member in members
                            if t_lo <= member["ts"] <= t_hi
                        ]

                exclude = spec["exclude"]
                if exclude == "current":
                    chosen = [m for m in chosen if m["pos"] != pos]
                elif exclude == "group":
                    chosen = [
                        m for m in chosen
                        if not (peer_start <= m["pos"] <= peer_end)
                    ]

                values = [m["value"] for m in chosen if m["value"] is not None]
                windows_out[name] = {
                    "count_all": len(chosen),
                    "count_value": len(values),
                    "sum": sum(values) if values else None,
                    "min": min(values) if values else None,
                    "max": max(values) if values else None,
                }

            expected_by_source[item["source_row"]] = {
                "source_row": item["source_row"],
                "row": item["obj"],
                "computed": {
                    "row_number": pos,
                    "rank": peer_start,
                    "dense_rank": dense_lookup[pos],
                    "lag": value_at(pos - k),
                    "lead": value_at(pos + k),
                    "windows": windows_out,
                },
            }

    return [expected_by_source[n] for n in sorted(expected_by_source)]
