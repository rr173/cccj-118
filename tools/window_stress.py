"""Scaling and bounded-memory driver for the window processor.

This is a measurement tool rather than an assertion test: it generates three
stress shapes, runs the real CLI in a child process and reports wall time,
rows read and peak RSS (Linux ``/proc/<pid>/status`` VmHWM).  Each shape is
run at N and 2N with a deliberately small SQLite cache and Python batch so
the input is provably larger than the available cache.

Shapes
------
* ``long_peer``  - one partition, every row shares one timestamp, with a
  RANGE frame covering the whole peer (exercises spilled monotonic deques);
* ``whole_group`` - one large partition with a ROWS frame spanning the group;
* ``many_groups`` - a large number of tiny partitions.

Usage::

    python tools/window_stress.py --rows 20000 --out-dir /tmp/win-stress
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

DECLARATION = {
    "partition_by": ["g"],
    "order_by": "ts",
    "value": "amount",
    "lag_lead_k": 3,
    "windows": {
        "peer_all_range": {"mode": "RANGE", "start": -(2 ** 40),
                           "end": 2 ** 40, "exclude": "none"},
        "whole_rows": {"mode": "ROWS", "start": -(2 ** 31),
                       "end": 2 ** 31, "exclude": "none"},
        "sliding": {"mode": "ROWS", "start": -5, "end": 5,
                    "exclude": "group"},
    },
}


def generate(shape: str, rows: int, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for source_row in range(1, rows + 1):
            if shape == "long_peer":
                obj = {"g": "only", "ts": 7,
                       "amount": None if source_row % 11 == 0 else source_row}
            elif shape == "whole_group":
                obj = {"g": "only", "ts": source_row,
                       "amount": (source_row % 97) - 48}
            else:  # many_groups: groups of four rows
                obj = {"g": f"g{source_row % max(1, rows // 4)}",
                       "ts": source_row,
                       "amount": source_row - rows // 2}
            handle.write(json.dumps(obj) + "\n")


def _sample_peak_rss(proc: subprocess.Popen,
                     poll_seconds: float = 0.05) -> int:
    """Return the child's peak resident set (KiB) by polling VmHWM.

    VmHWM is a high-water mark maintained by the kernel.  Liveness is checked
    with ``proc.poll()`` and every read is followed by a sleep, so this never
    busy-waits; one final read is attempted after exit (the status file is
    normally still present until the parent calls communicate()).
    """
    peak = 0
    status_path = f"/proc/{proc.pid}/status"

    def read_hwm() -> int:
        try:
            with open(status_path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1])
        except FileNotFoundError:
            pass
        return 0

    while proc.poll() is None:
        peak = max(peak, read_hwm())
        time.sleep(poll_seconds)
    peak = max(peak, read_hwm())
    return peak


def run_child(input_path: str, output_path: str, decl_path: str,
              batch_size: int, spill: int, cache_kib: int) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    argv = [
        sys.executable, "-m", "distagg", "window", "run",
        input_path, output_path, "--declaration", decl_path,
        "--batch-size", str(batch_size),
        "--spill-threshold", str(spill),
        "--sqlite-cache-size-kib", str(cache_kib),
    ]
    started = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    peak_rss = _sample_peak_rss(proc)
    stdout, stderr = proc.communicate()
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        raise RuntimeError(f"child failed: {stdout}\n{stderr}")
    return {
        "elapsed_seconds": round(elapsed, 4),
        "peak_rss_kib": peak_rss,
        "stdout": json.loads(stdout),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--spill-threshold", type=int, default=64)
    parser.add_argument("--sqlite-cache-size-kib", type=int, default=1024)
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="window-stress-")
    os.makedirs(out_dir, exist_ok=True)
    decl_path = os.path.join(out_dir, "declaration.json")
    with open(decl_path, "w", encoding="utf-8") as handle:
        json.dump(DECLARATION, handle)

    report = {"shapes": {}, "config": vars(args), "out_dir": out_dir}
    for shape in ("long_peer", "whole_group", "many_groups"):
        shape_results = []
        for multiplier in (1, 2):
            rows = args.rows * multiplier
            input_path = os.path.join(out_dir, f"{shape}-{rows}.jsonl")
            output_path = os.path.join(out_dir, f"{shape}-{rows}.out.jsonl")
            generate(shape, rows, input_path)
            measurement = run_child(
                input_path, output_path, decl_path,
                args.batch_size, args.spill_threshold,
                args.sqlite_cache_size_kib,
            )
            measurement["rows"] = rows
            measurement["input_bytes"] = os.path.getsize(input_path)
            shape_results.append(measurement)
        report["shapes"][shape] = shape_results

    report_path = os.path.join(out_dir, "stress-report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps(report, indent=2))
    print(f"\nreport written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
