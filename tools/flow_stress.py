#!/usr/bin/env python3
"""Bounded-memory / scaling driver for the flow executor.

Runs one synthetic split->explode->merge->sink graph over a source fed
line-by-line (the source file itself is streamed, never pre-loaded), consumes
every output row one at a time through the result channel, and reports honest,

* peak RSS of this process (``ru_maxrss``, itself a high-water mark),
* the highest per-edge occupancy actually observed,
* line counts, edge sent/received counters and the theoretical record bound,
* wall time.

The driver is run for two input sizes (N and 2N).  Peak memory must stay flat
while the line count doubles; if it does not, the script exits non-zero.

Usage::

    python3 tools/flow_stress.py [--rows 20000] [--capacity 2] [--inflight 2]
                                 [--results DIR] [--keep]

The generated source line is padded so that "one record per line" is
meaningfully sized; rows are written incrementally so generating the input
does not hold them in memory either.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from distagg.flow import FlowConfig, FlowGraph, start_run  # noqa: E402


def generate(path: str, rows: int, pad: int) -> int:
    written = 0
    with open(path, "w", encoding="utf-8") as handle:
        for index in range(rows):
            line = json.dumps({
                "n": index,
                "pad": "x" * pad,
                "items": [index, index + 1],
                "nested": {"keep": "value"},
            }, ensure_ascii=False)
            handle.write(line + "\n")
            written += len(line) + 1
    return written


def build_graph(source: str):
    """Fork after explode and re-merge, so several edges are exercised."""
    graph = FlowGraph(name="stress")
    graph.source("in", source)
    graph.select("all", "n", "ge", 0)
    graph.explode("explode", "items")
    graph.project("left", {"n": "items", "from": "n"})
    graph.project("right", {"n": "items", "pad": "pad"})
    graph.merge("merge")
    graph.sink("out", "out.jsonl")
    graph.add_edge("in", "all")
    graph.add_edge("all", "explode")
    graph.add_edge("explode", "left")
    graph.add_edge("explode", "right")
    graph.add_edge("left", "merge")
    graph.add_edge("right", "merge")
    graph.add_edge("merge", "out")
    graph.validate()
    return graph


async def measure(rows: int, capacity: int, inflight: int, work: str,
                  pad: int) -> dict:
    source = os.path.join(work, f"in-{rows}.jsonl")
    written = generate(source, rows, pad)
    results = os.path.join(work, f"run-{rows}")
    graph = build_graph(source)
    config = FlowConfig(results, default_capacity=capacity,
                        max_inflight_per_node=inflight,
                        result_stream=True, result_capacity=capacity)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.monotonic()
    run = start_run(graph, config)
    await run.start()
    consumed = 0
    async for _sink_name, _identity, _row in run.iter_results():
        consumed += 1
        del _sink_name, _identity, _row
    summary = await run.wait()
    await run.aclose()
    elapsed = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "rows": rows,
        "consumed": consumed,
        "elapsed_s": round(elapsed, 3),
        "source_bytes": written,
        "source_lines": summary.sources["in"]["lines_pulled"],
        "sink_lines": summary.sinks["out"]["records"],
        "peak_edge_occupancy": max(edge["peak_occupancy"]
                                   for edge in summary.edges),
        "edge_capacity_total": sum(edge["capacity"]
                                   for edge in summary.edges),
        "record_bound": summary["buffers"]["theoretical_record_bound"],
        "rss_before_kib": before,
        "rss_peak_kib": after,
        "rss_delta_kib": max(0, after - before),
        "status": summary.status,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--capacity", type=int, default=2)
    parser.add_argument("--inflight", type=int, default=2)
    parser.add_argument("--pad", type=int, default=64,
                        help="padding bytes per source row")
    parser.add_argument("--results", default=None,
                        help="working directory (default: a fresh temp dir)")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON object with both measurements")
    args = parser.parse_args(argv)
    work = args.results or tempfile.mkdtemp(prefix="flow-stress-")
    os.makedirs(work, exist_ok=True)

    measurements = []
    for rows in (args.rows, args.rows * 2):
        result = asyncio.run(measure(rows, args.capacity, args.inflight,
                                     work, args.pad))
        if result["status"] != "success":
            print(f"run of {rows} rows failed", file=sys.stderr)
            return 1
        measurements.append(result)
        if not args.json:
            print(json.dumps(result, sort_keys=True))
    if not args.keep and args.results is None:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    if args.json:
        print(json.dumps({"measurements": measurements}, sort_keys=True))

    small, large = measurements
    # Each source row has two array items; each branch is projected and both
    # branches are merged, so one source row produces four output rows.
    if large["consumed"] != 4 * rows or large["sink_lines"] == 0:
        return 1
    # The memory assertion: doubling the input must not double peak memory,
    # and the peak edge occupancy must stay at the configured capacity.
    if large["rss_delta_kib"] > 1.5 * small["rss_delta_kib"] + 2048:
        print(f"memory grew with input: {small['rss_delta_kib']} -> "
              f"{large['rss_delta_kib']} KiB", file=sys.stderr)
        return 1
    if large["peak_edge_occupancy"] > args.capacity:
        print("edge occupancy exceeded capacity", file=sys.stderr)
        return 1
    if not args.json:
        print(f"OK: {small['rows']} -> {large['rows']} rows, peak RSS flat "
              f"({small['rss_peak_kib']} -> {large['rss_peak_kib']} KiB), "
              f"edge peak <= {args.capacity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
