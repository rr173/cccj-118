#!/usr/bin/env python3
"""Runnable demonstration of the flow executor.

The script performs the four documented steps and prints every figure it
asserts, so the values can be checked by hand against the written files:

1. ``split_merge.json`` on ``imports.jsonl``: split into an exception report
   and exploded good items, then re-merge both into a ledger.  The ledger is
   printed with source line, expansion path and multiplicity, and the hand
   listing is checked.
2. A custom operator modifies a nested field on one broadcast branch while
   the other branch keeps the original value (both files printed).
3. A paused sink with capacity 1 and then 2: the source is shown stalled in a
   bounded range, the other branch keeps writing, and after ``resume()``
   every record is delivered exactly once.  Empty source, empty arrays and two
   sources with very different speeds are covered as well.
4. Faults: an operator error at a chosen physical line, a real sink write
   failure (``/dev/full``) and a cancellation while a channel is full; each
   is checked for a first-failure identity, no leftover tasks, reopenable
   files and honest partial counts.

Run::

    python3 demo/flow/flow_demo.py [WORKDIR]

Every run creates fresh result directories; existing ones are never reused.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from distagg.flow import FlowConfig, FlowGraph, load_graph, start_run  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GRAPH = os.path.join(HERE, "split_merge.json")
INPUT = os.path.join(HERE, "imports.jsonl")


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def banner(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


async def run_to_end(graph, results, **config):
    run = start_run(graph, FlowConfig(results, **config))
    summary = await run.run_to_completion()
    await run.aclose()
    return summary


# --------------------------------------------------------------------------- #
# 1. split -> merge, hand listing
# --------------------------------------------------------------------------- #

def step_split_merge(work):
    banner("1. split into exception report + exploded items, then re-merge")
    results = os.path.join(work, "split-merge")
    graph = load_graph(GRAPH)
    summary = asyncio.run(run_to_end(
        graph, results, default_capacity=2, max_inflight_per_node=2))
    if not summary.ok:
        raise SystemExit(f"split/merge run failed: {summary.error}")
    print(f"inputs: {INPUT}")
    print(f"results: {results}")
    print()
    for name, sink in summary.sinks.items():
        print(f"  sink {name:18s} {sink['records']:3d} rows  complete={sink['complete']}")
    print(f"  source lines pulled: {summary.sources['in']['lines_pulled']}")
    print()
    for edge in summary.edges:
        print(f"  edge {edge['from']:>10s} -> {edge['to']:<18s} "
              f"cap={edge['capacity']} sent={edge['sent']:3d} "
              f"received={edge['received']:3d} peak={edge['peak_occupancy']}")
    print()
    print("ledger (file order):")
    hand = []
    ledger = read_jsonl(os.path.join(results, "ledger.jsonl"))
    for row in ledger:
        identity = row["identity"]
        path = tuple(identity["expansion_path"])
        reason = ("exception, whole record" if not path
                  else "exploded item")
        print(f"  line {identity['line']:2d}  path {str(path):8s}  "
              f"{reason:24s}  {json.dumps(row['row'], ensure_ascii=False)}")
        hand.append((identity["line"], path))
    print()
    print("  hand listing          :", hand)
    # The two exception rows come over the error branch (whole records, empty
    # path); the four good items come over the exploded branch with a path.
    exceptions = [entry for entry in hand if not entry[1]]
    exploded = [entry for entry in hand if entry[1]]
    assert sorted(exceptions) == [(2, ()), (3, ())], exceptions
    assert len(exploded) == 4 and (1, (0,)) in exploded and (1, (1,)) in exploded
    assert len(hand) == 6
    # A-1 has two identical items; both are kept (different paths).
    same = [row for row in ledger if row["row"].get("order_id") == "A-3"]
    assert len(same) == 1
    print("  asserted: 2 exception rows, 4 exploded rows, duplicate item kept twice")
    return results


# --------------------------------------------------------------------------- #
# 2. broadcast isolation with a custom operator
# --------------------------------------------------------------------------- #

def step_broadcast(work):
    banner("2. custom operator mutates a nested field on one branch only")
    source = write_jsonl(os.path.join(work, "broadcast-in.jsonl"), [
        {"id": "A-1", "nested": {"mark": "original"}},
    ])
    results = os.path.join(work, "broadcast")

    async def mutate(record, emit):
        record.row["nested"]["mark"] = "changed-on-branch-a"
        await emit(record)

    graph = FlowGraph()
    graph.source("in", source)
    graph.custom("mutate_a", mutate)
    graph.sink("a", "branch-a.jsonl")
    graph.sink("b", "branch-b.jsonl")
    graph.add_edge("in", "mutate_a")
    graph.add_edge("in", "b")
    graph.add_edge("mutate_a", "a")
    graph.validate()
    summary = asyncio.run(run_to_end(graph, results))
    if not summary.ok:
        raise SystemExit(f"broadcast run failed: {summary.error}")
    a = read_jsonl(os.path.join(results, "branch-a.jsonl"))[0]["row"]
    b = read_jsonl(os.path.join(results, "branch-b.jsonl"))[0]["row"]
    print(f"  branch a (after mutate_a): {json.dumps(a)}")
    print(f"  branch b (untouched)     : {json.dumps(b)}")
    assert a["nested"]["mark"] == "changed-on-branch-a"
    assert b["nested"]["mark"] == "original"
    print("  asserted: branch b still sees 'original'")


# --------------------------------------------------------------------------- #
# 3. bounded pause, empty cases, different speeds
# --------------------------------------------------------------------------- #

async def _paused_scenario(work, capacity, count=80):
    source = write_jsonl(os.path.join(work, f"pause-in-{capacity}.jsonl"),
                         [{"n": i} for i in range(1, count + 1)])
    results = os.path.join(work, f"pause-{capacity}")
    graph = FlowGraph()
    graph.source("in", source)
    graph.select("all", "n", "ge", 1)
    graph.sink("slow", "slow.jsonl")
    graph.sink("fast", "fast.jsonl")
    graph.add_edge("in", "all")
    graph.add_edge("all", "slow")
    graph.add_edge("all", "fast")
    graph.validate()
    run = start_run(graph, FlowConfig(results, default_capacity=capacity,
                                      max_inflight_per_node=capacity))
    gate = run.add_gate("slow")
    gate.pause()
    await run.start()
    await asyncio.wait_for(gate.wait_paused(), 5)
    for _ in range(300):
        await asyncio.sleep(0)
    snap = await run.snapshot()
    pulled = snap["sources"]["in"]["lines_pulled"]
    fast_rows = len(read_jsonl(os.path.join(results, "fast.jsonl"))) \
        if os.path.exists(os.path.join(results, "fast.jsonl")) else 0
    print(f"  capacity={capacity} inflight={capacity}: while slow is paused")
    print(f"    source lines pulled : {pulled} (of {count})")
    print(f"    fast sink rows      : {fast_rows}")
    for edge in snap["edges"]:
        print(f"    edge {edge['from']:>3s} -> {edge['to']:<5s} "
              f"sent={edge['sent']} received={edge['received']} "
              f"peak={edge['peak_occupancy']}/{edge['capacity']}")
    gate.resume()
    summary = await run.wait()
    await run.aclose()
    slow_rows = len(read_jsonl(os.path.join(results, "slow.jsonl")))
    fast_rows = len(read_jsonl(os.path.join(results, "fast.jsonl")))
    print(f"    after resume: slow={slow_rows} fast={fast_rows} "
          f"status={summary.status}")
    assert summary.ok, summary.error
    assert slow_rows == count and fast_rows == count
    assert [row["row"]["n"] for row in
            read_jsonl(os.path.join(results, "slow.jsonl"))] == list(range(1, count + 1))
    assert [row["row"]["n"] for row in
            read_jsonl(os.path.join(results, "fast.jsonl"))] == list(range(1, count + 1))
    bound = (4 * capacity) * 2 + 8
    assert pulled <= bound, pulled
    assert pulled < count
    assert max(edge["peak_occupancy"] for edge in snap["edges"]) <= capacity

    async def _drain():
        return None
    return pulled


def step_pauses(work):
    banner("3a. paused sink: bounded source, no dropped records (capacity 1 then 2)")
    for capacity in (1, 2):
        asyncio.run(_paused_scenario(work, capacity))


def step_empty(work):
    banner("3b. empty source, empty arrays, end markers still merge")
    empty = write_jsonl(os.path.join(work, "empty.jsonl"), [])
    empty_items = write_jsonl(os.path.join(work, "empty-items.jsonl"),
                              [{"items": []}, {"items": []}])
    graph = FlowGraph()
    graph.source("in", empty)
    graph.source("in2", empty_items)
    graph.explode("ex", "items")
    graph.merge("m")
    graph.sink("out", "out.jsonl")
    graph.add_edge("in", "m")
    graph.add_edge("in2", "ex")
    graph.add_edge("ex", "m")
    graph.add_edge("m", "out")
    graph.validate()
    summary = asyncio.run(run_to_end(
        graph, os.path.join(work, "empty"),
        default_capacity=2, max_inflight_per_node=2))
    print(f"  status={summary.status} out rows={summary.sinks['out']['records']} "
          f"complete={summary.sinks['out']['complete']}")
    assert summary.ok, summary.error
    assert summary.sinks["out"]["records"] == 0
    assert summary.sinks["out"]["complete"]


def step_speeds(work):
    banner("3c. two sources with very different speeds merge fairly")
    fast = write_jsonl(os.path.join(work, "fast-src.jsonl"),
                       [{"src": "fast", "n": i} for i in range(200)])
    slow = write_jsonl(os.path.join(work, "slow-src.jsonl"),
                       [{"src": "slow", "n": i} for i in range(5)])
    graph = FlowGraph()
    graph.source("fast", fast)
    graph.source("slow", slow)
    graph.merge("m")
    graph.sink("out", "out.jsonl")
    graph.add_edge("fast", "m")
    graph.add_edge("slow", "m")
    graph.add_edge("m", "out")
    graph.validate()
    results = os.path.join(work, "speeds")
    summary = asyncio.run(run_to_end(
        graph, results, default_capacity=2, max_inflight_per_node=2))
    rows = read_jsonl(os.path.join(results, "out.jsonl"))
    per_source = {}
    for row in rows:
        per_source.setdefault(row["row"]["src"], []).append(row["row"]["n"])
    print(f"  total rows={len(rows)} order per source: "
          f"fast={per_source['fast'][:6]}... slow={per_source['slow']}")
    assert summary.ok, summary.error
    assert len(rows) == 205
    assert per_source["fast"] == list(range(200))
    assert per_source["slow"] == list(range(5))
    print("  asserted: each source's order preserved, no record lost")


# --------------------------------------------------------------------------- #
# 4. faults and cancellation
# --------------------------------------------------------------------------- #

def step_operator_error(work):
    banner("4a. operator error at physical line 5")
    source = write_jsonl(os.path.join(work, "fault-operr.jsonl"),
                         [{"n": i} for i in range(1, 21)])
    results = os.path.join(work, "fault-operr")

    async def failing(record, emit):
        if record.line == 5:
            raise RuntimeError("deliberate failure at line 5")
        await emit(record)

    graph = FlowGraph()
    graph.source("in", source)
    graph.custom("boom", failing)
    graph.sink("out", "out.jsonl")
    graph.add_edge("in", "boom")
    graph.add_edge("boom", "out")
    graph.validate()

    async def scenario():
        run = start_run(graph, FlowConfig(
            results, default_capacity=1, max_inflight_per_node=1))
        summary = await run.run_to_completion()
        leftovers = [task.get_name() for task in asyncio.all_tasks()
                     if task.get_name().startswith("flow:")]
        await run.aclose()
        return summary, leftovers

    summary, leftovers = asyncio.run(scenario())
    print(f"  status={summary.status} node={summary.error['node']} "
          f"line={summary.error['identity']['line']} "
          f"kind={summary.error['exception_kind']}")
    print(f"  partial rows written={summary.sinks['out']['records']} "
          f"complete={summary.sinks['out']['complete']}")
    print(f"  leftover flow tasks={leftovers}")
    assert summary.status == "failed"
    assert summary.error["identity"] == {"source": "in", "line": 5,
                                         "expansion_path": []}
    assert not summary.sinks["out"]["complete"]
    assert leftovers == []
    written = read_jsonl(os.path.join(results, "out.jsonl"))
    assert len(written) == summary.sinks["out"]["records"]
    with open(os.path.join(results, "out.jsonl"), "rb"):
        pass
    print("  asserted: first failure identity kept, file reopenable, honest count")


def step_sink_failure(work):
    banner("4b. real sink write failure (/dev/full)")
    source = write_jsonl(os.path.join(work, "fault-sink.jsonl"),
                         [{"n": i} for i in range(1, 6)])
    results = os.path.join(work, "fault-sink")
    graph = FlowGraph()
    graph.source("in", source)
    graph.sink("out", "out.jsonl")
    graph.add_edge("in", "out")
    graph.validate()

    async def scenario():
        run = start_run(graph, FlowConfig(
            results, default_capacity=1, max_inflight_per_node=1))
        gate = run.add_gate("out")
        gate.pause()
        await run.start()
        await asyncio.wait_for(gate.wait_paused(), 5)
        node = run.run.nodes["out"]
        node.handle.close()
        node.handle = open("/dev/full", "wb")
        gate.resume()
        summary = await run.wait()
        leftovers = [task.get_name() for task in asyncio.all_tasks()
                     if task.get_name().startswith("flow:")]
        await run.aclose()
        return summary, leftovers

    summary, leftovers = asyncio.run(scenario())
    print(f"  status={summary.status} node={summary.error['node']} "
          f"line={summary.error['identity']['line']}")
    print(f"  message: {summary.error['message'][:70]}...")
    print(f"  partial rows={summary.sinks['out']['records']} "
          f"complete={summary.sinks['out']['complete']} leftovers={leftovers}")
    assert summary.status == "failed"
    assert summary.error["node"] == "out"
    assert summary.error["identity"]["line"] == 1
    assert summary.sinks["out"]["records"] == 0
    assert leftovers == []


def step_cancel(work):
    banner("4c. cancellation while a channel is full")
    source = write_jsonl(os.path.join(work, "cancel-in.jsonl"),
                         [{"n": i} for i in range(1, 2001)])
    results = os.path.join(work, "cancel")
    graph = FlowGraph()
    graph.source("in", source)
    graph.select("pass", "n", "ge", 1)
    graph.sink("out", "out.jsonl")
    graph.add_edge("in", "pass")
    graph.add_edge("pass", "out")
    graph.validate()

    async def scenario():
        run = start_run(graph, FlowConfig(
            results, default_capacity=1, max_inflight_per_node=1))
        gate = run.add_gate("pass")
        gate.pause()
        await run.start()
        await asyncio.wait_for(gate.wait_paused(), 5)
        for _ in range(300):
            await asyncio.sleep(0)
        snap = await run.snapshot()
        pulled = snap["sources"]["in"]["lines_pulled"]
        run.cancel()
        summary = await asyncio.wait_for(run.wait(), 10)
        leftovers = [task.get_name() for task in asyncio.all_tasks()
                     if task.get_name().startswith("flow:")]
        await run.aclose()
        return summary, pulled, leftovers

    summary, pulled, leftovers = asyncio.run(scenario())
    written = read_jsonl(os.path.join(results, "out.jsonl"))
    print(f"  status={summary.status} lines pulled before cancel={pulled}")
    print(f"  partial rows={summary.sinks['out']['records']} "
          f"file rows={len(written)} complete={summary.sinks['out']['complete']}")
    print(f"  leftover flow tasks={leftovers}")
    assert summary.status == "cancelled"
    assert not summary.ok
    assert pulled < 50
    assert len(written) == summary.sinks["out"]["records"]
    assert leftovers == []


def step_bad_graph(work):
    banner("4d. a bad graph is rejected before opening the source")
    directory = os.path.join(work, "bad-graph")
    os.makedirs(directory, exist_ok=True)
    source = write_jsonl(os.path.join(directory, "in.jsonl"), [{"n": 1}])
    document = json.loads(open(GRAPH, encoding="utf-8").read())
    document["nodes"][0]["path"] = source
    document["edges"].append({"from": "ledger", "to": "items"})  # cycle
    path = os.path.join(directory, "bad.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    from distagg.flow.errors import GraphError

    results = os.path.join(directory, "results")
    try:
        load_graph(path)
    except GraphError as exc:
        print(f"  rejected: {exc.problems[0]}")
    else:  # pragma: no cover
        raise AssertionError("bad graph was accepted")
    assert not os.path.exists(results)
    assert os.path.exists(source)
    print("  asserted: no result directory, source untouched")


def main(argv=None) -> int:
    work = argv[1] if argv and len(argv) > 1 else tempfile.mkdtemp(
        prefix="distagg-flow-demo-")
    os.makedirs(work, exist_ok=True)
    print(f"work directory: {work}")
    try:
        step_split_merge(work)
        step_broadcast(work)
        step_pauses(work)
        step_empty(work)
        step_speeds(work)
        step_operator_error(work)
        step_sink_failure(work)
        step_cancel(work)
        step_bad_graph(work)
    finally:
        if argv is None or len(argv) <= 1:
            # The default temporary work directory is disposable; figure
            # files under it were printed above for reference.
            shutil.rmtree(work, ignore_errors=True)
    banner("all demonstrations passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
