"""End-to-end tests for the asynchronous flow executor.

Every test drives real files and the real asyncio runtime; nothing is mocked.
The suite covers:

* split -> merge of one small messy file, with hand-checked identities,
  expansion paths and multiplicities;
* broadcast isolation with a custom operator mutating one branch;
* bounded memory under a paused sink (capacities 1 and 2), and resumed
  completion without dropped records;
* empty sources, empty arrays and very different source speeds;
* operator error, sink write failure and cancellation while a channel is full;
* fresh result directories, partial counts and summary statuses;
* peak RSS / peak occupancy under growing synthetic input.
"""

import asyncio
import json
import os
import resource
import tempfile
import time
import unittest

from distagg.flow import (
    FlowConfig,
    FlowGraph,
    Record,
    start_run,
)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def run_graph(graph, results_dir, **config):
    config.setdefault("default_capacity", 2)
    config.setdefault("max_inflight_per_node", 2)
    return start_run(graph, FlowConfig(results_dir, **config))


def messy_document(source_path):
    return {
        "name": "messy",
        "nodes": [
            {"name": "in", "type": "source", "path": source_path},
            {"name": "errors", "type": "select", "field": "status",
             "condition": "eq", "value": "error"},
            {"name": "goods", "type": "select", "field": "status",
             "condition": "ne", "value": "error"},
            {"name": "explode", "type": "explode", "field": "items"},
            {"name": "flat", "type": "project",
             "fields": {"order": "id", "item": "items"}},
            {"name": "merged", "type": "merge"},
            {"name": "errors_out", "type": "sink", "path": "errors.jsonl"},
            {"name": "goods_out", "type": "sink", "path": "flat.jsonl"},
            {"name": "merged_out", "type": "sink", "path": "merged.jsonl"},
        ],
        "edges": [
            {"from": "in", "to": "errors"},
            {"from": "in", "to": "goods"},
            {"from": "errors", "to": "errors_out"},
            {"from": "goods", "to": "explode"},
            {"from": "explode", "to": "flat"},
            {"from": "flat", "to": "merged"},
            {"from": "errors", "to": "merged"},
            {"from": "flat", "to": "goods_out"},
            {"from": "merged", "to": "merged_out"},
        ],
    }


MESSY_ROWS = [
    {"id": "A", "status": "ok", "items": [{"sku": "x", "qty": 1},
                                         {"sku": "x", "qty": 1}]},
    {"id": "B", "status": "error", "items": []},
    {"id": "B", "status": "error", "items": [{"sku": "y", "qty": None}]},
    {"id": "C", "status": "ok", "items": [{"sku": "z", "qty": 4}]},
]


class SplitMergeTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="flow-split-")
        self.source = write_jsonl(os.path.join(self.work, "in.jsonl"),
                                  MESSY_ROWS)

    def test_split_and_merge_with_hand_checked_identities(self):
        from distagg.flow.graph import parse_graph

        results = os.path.join(self.work, "results")
        good_path = os.path.join(results, "flat.jsonl")
        graph = parse_graph(messy_document(self.source))
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        self.assertEqual(summary.status, "success")

        goods = read_jsonl(good_path)
        # Two good source lines -> 3 exploded items.  Duplicate item values
        # stay distinct: expansion_path differs.
        self.assertEqual(
            [(row["identity"]["line"],
              row["identity"]["expansion_path"]) for row in goods],
            [(1, [0]), (1, [1]), (4, [0])],
        )
        # Project renamed the top-level fields.
        self.assertEqual([row["row"] for row in goods],
                         [{"order": "A", "item": {"sku": "x", "qty": 1}},
                          {"order": "A", "item": {"sku": "x", "qty": 1}},
                          {"order": "C", "item": {"sku": "z", "qty": 4}}])

        errors = read_jsonl(os.path.join(results, "errors.jsonl"))
        # Line 2 has an empty array (delivered whole as an error); line 3 has
        # one null-valued item.  The error branch does not explode.
        self.assertEqual([row["identity"]["line"] for row in errors], [2, 3])
        self.assertEqual([row["identity"]["expansion_path"] for row in errors],
                         [[], []])

        merged = read_jsonl(os.path.join(results, "merged.jsonl"))
        # ``in -> errors`` and ``in -> goods`` are a broadcast, but the two
        # selects are complementary, so each source line reaches the merge
        # exactly once here.  The error branch feeds merge and the error sink;
        # the good branch feeds merge after explosion.
        self.assertEqual(
            sorted((row["identity"]["line"],
                    tuple(row["identity"]["expansion_path"]))
                   for row in merged),
            [(1, (0,)), (1, (1,)), (2, ()), (3, ()), (4, (0,))],
        )

        self.assertEqual(summary.sinks["goods_out"]["records"], 3)
        self.assertEqual(summary.sinks["errors_out"]["records"], 2)
        self.assertEqual(summary.sinks["merged_out"]["records"], 5)
        for sink in summary.sinks.values():
            self.assertTrue(sink["complete"])
            self.assertTrue(sink["closed_cleanly"])
        # Per-edge sent/received plus real peak occupancy.
        edges = {(edge["from"], edge["to"]): edge for edge in summary.edges}
        self.assertEqual(edges[("in", "goods")]["sent"], 4)
        self.assertEqual(edges[("in", "goods")]["received"], 4)
        self.assertEqual(edges[("explode", "flat")]["sent"], 3)
        self.assertEqual(edges[("merged", "merged_out")]["received"], 5)
        for edge in summary.edges:
            self.assertLessEqual(edge["peak_occupancy"], edge["capacity"])
        # Source pulled exactly as many physical lines as the file has.
        self.assertEqual(summary.sources["in"]["lines_pulled"], 4)

    def test_same_record_on_two_paths_reaches_merge_twice(self):
        from distagg.flow.graph import parse_graph

        results = os.path.join(self.work, "twice")
        document = {
            "nodes": [
                {"name": "in", "type": "source", "path": self.source},
                {"name": "left", "type": "select", "field": "status",
                 "condition": "ne", "value": "never"},
                {"name": "right", "type": "select", "field": "status",
                 "condition": "ne", "value": "never"},
                {"name": "m", "type": "merge"},
                {"name": "out", "type": "sink", "path": "out.jsonl"},
            ],
            "edges": [
                {"from": "in", "to": "left"},
                {"from": "in", "to": "right"},
                {"from": "left", "to": "m"},
                {"from": "right", "to": "m"},
                {"from": "m", "to": "out"},
            ],
        }
        summary = asyncio.run(
            run_graph(parse_graph(document), results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        rows = read_jsonl(os.path.join(results, "out.jsonl"))
        self.assertEqual(len(rows), 2 * len(MESSY_ROWS))
        ids = sorted((r["identity"]["line"], r["identity"]["expansion_path"])
                     for r in rows)
        self.assertEqual(ids, [(1, []), (1, []), (2, []), (2, []),
                              (3, []), (3, []), (4, []), (4, [])])
        # Intra-path order preserved: each of the two runs is 1,2,3,4.
        self.assertEqual(summary.sinks["out"]["records"], 8)


class CustomOperatorApiTest(unittest.TestCase):
    """Caller-provided operators: emit-per-record, async iterables, cleanup."""

    def _graph(self, work, operator, count=5):
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": i} for i in range(1, count + 1)])
        graph = FlowGraph()
        graph.source("in", source)
        graph.custom("c", operator)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "c")
        graph.add_edge("c", "out")
        graph.validate()
        return graph, os.path.join(work, "r")

    def test_emit_zero_one_and_many(self):
        work = tempfile.mkdtemp(prefix="flow-custom-emit-")

        async def operator(record, emit):
            value = record.row["n"]
            if value == 2:
                return               # zero outputs
            await emit(record)       # the original record
            if value == 3:
                row = dict(record.row, twice=True)
                await emit(Record(record.source, record.line,
                                  record.expansion_path, row))

        graph, results = self._graph(work, operator)
        summary = asyncio.run(run_graph(graph, results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        rows = read_jsonl(os.path.join(results, "out.jsonl"))
        self.assertEqual([row["row"]["n"] for row in rows], [1, 3, 3, 4, 5])
        self.assertEqual(sum(1 for row in rows if row["row"].get("twice")), 1)
        self.assertEqual(summary.sinks["out"]["records"], 5)

    def test_async_iterable_is_consumed_incrementally_and_closed(self):
        work = tempfile.mkdtemp(prefix="flow-custom-iter-")
        closed = {"value": False, "yielded": 0}

        async def operator(record, emit):
            async def produce():
                try:
                    for index in range(3):
                        closed["yielded"] += 1
                        yield Record(record.source, record.line,
                                     record.expansion_path + (index,),
                                     dict(record.row, child=index))
                finally:
                    closed["value"] = True
            return produce()

        graph, results = self._graph(work, operator, count=2)
        run = run_graph(graph, results)
        summary = asyncio.run(run.run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        self.assertTrue(closed["value"])
        self.assertEqual(closed["yielded"], 6)
        rows = read_jsonl(os.path.join(results, "out.jsonl"))
        self.assertEqual(len(rows), 6)
        # The operator emits children of both source lines with paths built
        # from the (empty) parent path plus the child index; the executor
        # keeps both children of each line distinct.
        self.assertEqual(sorted((row["identity"]["line"],
                                 row["identity"]["expansion_path"][0])
                                for row in rows),
                         [(1, 0), (1, 1), (1, 2), (2, 0), (2, 1), (2, 2)])

    def test_returning_a_list_is_rejected(self):
        work = tempfile.mkdtemp(prefix="flow-custom-list-")

        async def operator(record, emit):
            return [record]          # a whole batch is not allowed

        graph, results = self._graph(work, operator)
        summary = asyncio.run(run_graph(graph, results).run_to_completion())
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.error["node"], "c")
        self.assertIn("async iterable", summary.error["message"])

    def test_emitted_record_byte_limit_is_enforced(self):
        work = tempfile.mkdtemp(prefix="flow-custom-big-")

        async def operator(record, emit):
            row = dict(record.row, pad="x" * 500)
            await emit(Record(record.source, record.line,
                              record.expansion_path, row))

        graph, results = self._graph(work, operator, count=1)
        run = run_graph(graph, results, max_record_bytes=100)
        summary = asyncio.run(run.run_to_completion())
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.error["node"], "c")
        self.assertIn("max_record_bytes", summary.error["message"])


class BroadcastIsolationTest(unittest.TestCase):
    def test_custom_operator_mutates_nested_object_on_one_branch_only(self):
        work = tempfile.mkdtemp(prefix="flow-broadcast-")
        source = write_jsonl(os.path.join(work, "in.jsonl"), [
            {"id": 1, "nested": {"mark": "original"}},
        ])
        results = os.path.join(work, "r")

        async def mutate(record, emit):
            # Mutating in place is exactly what a careless operator does; the
            # executor's broadcast must already have handed the other branch
            # its own deep copy.
            record.row["nested"]["mark"] = "changed"
            record.row["extra"] = "added"
            await emit(record)

        graph = FlowGraph()
        graph.source("in", source)
        graph.custom("mutate", mutate)
        graph.sink("mutated", "mutated.jsonl")
        graph.sink("untouched", "untouched.jsonl")
        # in -> {mutate, untouched} is a broadcast; mutate -> mutated is a
        # separate single-successor edge.  The untouched branch must never
        # observe the nested mutation performed by the custom operator.
        graph.add_edge("in", "mutate")
        graph.add_edge("in", "untouched")
        graph.add_edge("mutate", "mutated")
        graph.validate()
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        mutated = read_jsonl(os.path.join(results, "mutated.jsonl"))
        untouched = read_jsonl(os.path.join(results, "untouched.jsonl"))
        self.assertEqual(mutated[0]["row"]["nested"]["mark"], "changed")
        self.assertEqual(mutated[0]["row"]["extra"], "added")
        self.assertEqual(untouched[0]["row"]["nested"]["mark"], "original")
        self.assertNotIn("extra", untouched[0]["row"])

    def test_builtin_operators_produce_independent_rows(self):
        # project deep-copies; mutating one projected branch cannot affect the
        # other copy of the same nested object.
        work = tempfile.mkdtemp(prefix="flow-broadcast2-")
        source = write_jsonl(os.path.join(work, "in.jsonl"), [
            {"id": 1, "nested": {"mark": "original"}, "other": 5},
        ])
        results = os.path.join(work, "r")

        async def mutate_a(record, emit):
            record.row["nested"]["mark"] = "a"
            await emit(record)

        async def mutate_b(record, emit):
            # Must still see the original value.
            record.row["seen"] = record.row["nested"]["mark"]
            await emit(record)

        graph = FlowGraph()
        graph.source("in", source)
        graph.custom("a", mutate_a)
        graph.custom("b", mutate_b)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "a")
        graph.add_edge("in", "b")
        graph.add_edge("a", "out")
        graph.add_edge("b", "out")
        graph.validate()
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        rows = read_jsonl(os.path.join(results, "out.jsonl"))
        by_extra = {("seen" in row["row"]): row for row in rows}
        self.assertEqual(by_extra[True]["row"]["seen"], "original")
        self.assertEqual(by_extra[False]["row"]["nested"]["mark"], "a")


class PausedSinkTest(unittest.TestCase):
    """A paused sink must stop the source within a bounded number of records."""

    def _graph(self, work, count=60, capacity=1, inflight=2):
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": i} for i in range(1, count + 1)])
        graph = FlowGraph()
        graph.source("in", source)
        graph.select("all", "n", "ge", 1)
        graph.sink("slow", "slow.jsonl")
        graph.sink("fast", "fast.jsonl")
        graph.add_edge("in", "all")
        graph.add_edge("all", "slow")
        graph.add_edge("all", "fast")
        graph.validate()
        return graph

    def _pause_and_inspect(self, capacity, inflight):
        work = tempfile.mkdtemp(prefix=f"flow-pause-{capacity}-")
        graph = self._graph(work, capacity=capacity, inflight=inflight)
        results = os.path.join(work, "r")
        run = run_graph(graph, results, default_capacity=capacity,
                        max_inflight_per_node=inflight)
        gate = run.add_gate("slow")
        gate.pause()

        async def scenario():
            await run.start()
            await asyncio.wait_for(gate.wait_paused(), 5)
            # Let everything that can make progress without the slow sink do
            # so, then take a stable snapshot.
            for _ in range(200):
                await asyncio.sleep(0)
            snap = await run.snapshot()
            gate.resume()
            summary = await run.wait()
            await run.aclose()
            return snap, summary

        snap, summary = asyncio.run(scenario())
        source_lines = snap["sources"]["in"]["lines_pulled"]
        # The source must be stalled in a range fixed by capacity/inflight,
        # never proportional to the 60-line input.
        bound = (2 * capacity + inflight) * 2 + 4
        self.assertGreaterEqual(source_lines, 1)
        self.assertLessEqual(source_lines, bound,
                             f"source read {source_lines} while slow was paused")
        self.assertLess(source_lines, 60)
        # The other branch is not starved: its sink writes while the slow sink
        # is still paused (at least two full copies reached it).
        fast_path = os.path.join(results, "fast.jsonl")
        self.assertTrue(os.path.exists(fast_path))
        self.assertGreater(len(read_jsonl(fast_path)), 0)
        # No edge over its capacity.
        for edge in snap["edges"]:
            self.assertLessEqual(edge["peak_occupancy"], capacity)

        self.assertTrue(summary.ok, summary.error)
        self.assertEqual(summary.sinks["slow"]["records"], 60)
        self.assertEqual(summary.sinks["fast"]["records"], 60)
        self.assertEqual(summary.sources["in"]["lines_pulled"], 60)

        slow = read_jsonl(os.path.join(results, "slow.jsonl"))
        fast = read_jsonl(os.path.join(results, "fast.jsonl"))
        # Intra-edge order is retained, and nothing was dropped or duplicated.
        self.assertEqual([r["row"]["n"] for r in slow], list(range(1, 61)))
        self.assertEqual([r["row"]["n"] for r in fast], list(range(1, 61)))
        # Every edge reports honest sent/received counts.
        for edge in summary.edges:
            self.assertEqual(edge["sent"], edge["received"])
            self.assertEqual(edge["received"], 60)

    def test_capacity_one_paused_sink_bounds_the_source(self):
        self._pause_and_inspect(capacity=1, inflight=2)

    def test_capacity_two_paused_sink_bounds_the_source(self):
        self._pause_and_inspect(capacity=2, inflight=2)

    def test_gate_blocks_until_resumed_and_never_drops(self):
        work = tempfile.mkdtemp(prefix="flow-gate-")
        graph = self._graph(work, count=10)
        results = os.path.join(work, "r")
        run = run_graph(graph, results)

        async def scenario():
            gate = run.add_gate("slow")
            gate.pause()
            await run.start()
            await asyncio.wait_for(gate.wait_paused(), 5)
            snap_paused = await run.snapshot()
            self.assertTrue(gate.is_paused)
            gate.resume()
            summary = await run.wait()
            return snap_paused, summary

        snap, summary = asyncio.run(scenario())
        self.assertTrue(summary.ok, summary.error)
        self.assertEqual(summary.sinks["slow"]["records"], 10)
        self.assertGreaterEqual(snap["sinks"]["slow"]["records"], 0)


class EdgeCasesTest(unittest.TestCase):
    def test_empty_source_completes_and_every_sink_is_complete(self):
        work = tempfile.mkdtemp(prefix="flow-empty-")
        source = write_jsonl(os.path.join(work, "in.jsonl"), [])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.merge("m")
        graph.sink("out", "out.jsonl")
        # merge needs two inputs; use two empty sources.
        source2 = write_jsonl(os.path.join(work, "in2.jsonl"), [])
        graph.source("in2", source2)
        graph.add_edge("in", "m")
        graph.add_edge("in2", "m")
        graph.add_edge("m", "out")
        graph.validate()
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        self.assertEqual(summary.sinks["out"]["records"], 0)
        self.assertTrue(summary.sinks["out"]["complete"])
        self.assertEqual(summary.sources["in"]["lines_pulled"], 0)
        self.assertEqual(os.path.getsize(os.path.join(results, "out.jsonl")), 0)

    def test_empty_array_and_missing_optional_branch(self):
        work = tempfile.mkdtemp(prefix="flow-emptyarr-")
        source = write_jsonl(os.path.join(work, "in.jsonl"), [
            {"items": []},
            {"items": []},
        ])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.explode("ex", "items")
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "ex")
        graph.add_edge("ex", "out")
        graph.validate()
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        self.assertEqual(summary.sinks["out"]["records"], 0)
        self.assertTrue(summary.sinks["out"]["complete"])
        self.assertEqual(summary.sources["in"]["lines_pulled"], 2)

    def test_two_sources_with_very_different_speeds(self):
        work = tempfile.mkdtemp(prefix="flow-speeds-")
        fast_source = write_jsonl(os.path.join(work, "fast.jsonl"),
                                  [{"src": "fast", "n": i} for i in range(200)])
        slow_source = write_jsonl(os.path.join(work, "slow.jsonl"),
                                  [{"src": "slow", "n": i} for i in range(5)])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("fast", fast_source)
        graph.source("slow", slow_source)
        graph.merge("m")
        graph.sink("out", "out.jsonl")
        graph.add_edge("fast", "m")
        graph.add_edge("slow", "m")
        graph.add_edge("m", "out")
        graph.validate()
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        rows = read_jsonl(os.path.join(results, "out.jsonl"))
        self.assertEqual(len(rows), 205)
        for name in ("fast", "slow"):
            subset = [r for r in rows if r["row"]["src"] == name]
            # Intra-source order preserved exactly.
            self.assertEqual([r["row"]["n"] for r in subset],
                             list(range(len(subset))))
        # The slow source finished long before the fast one; its end marker
        # must not have ended the merge early.
        self.assertEqual(summary.sources["slow"]["lines_pulled"], 5)
        self.assertEqual(summary.sources["fast"]["lines_pulled"], 200)

    def test_merge_does_not_wait_for_an_empty_input(self):
        # One input is empty from the start; the other must flow entirely.
        work = tempfile.mkdtemp(prefix="flow-mergeempty-")
        full = write_jsonl(os.path.join(work, "full.jsonl"),
                           [{"n": i} for i in range(50)])
        empty = write_jsonl(os.path.join(work, "empty.jsonl"), [])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("full", full)
        graph.source("empty", empty)
        graph.merge("m")
        graph.sink("out", "out.jsonl")
        graph.add_edge("full", "m")
        graph.add_edge("empty", "m")
        graph.add_edge("m", "out")
        graph.validate()
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertTrue(summary.ok, summary.error)
        self.assertEqual(summary.sinks["out"]["records"], 50)


class StrictSourceTest(unittest.TestCase):
    def _run_raw(self, payload: bytes, **config):
        work = tempfile.mkdtemp(prefix="flow-strict-")
        source = os.path.join(work, "in.jsonl")
        with open(source, "wb") as handle:
            handle.write(payload)
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "out")
        graph.validate()
        return asyncio.run(
            run_graph(graph, results, **config).run_to_completion())

    def test_blank_line_and_duplicate_member_and_bad_utf8(self):
        cases = [
            (b'{"a": 1}\n\n{"a": 2}\n', "blank line", 2),
            (b'{"a": 1}\n{"a": 1, "a": 2}\n', "duplicate JSON member", 2),
            (b'{"a": 1}\n\xff\xfe\n', "not valid UTF-8", 2),
            (b'[1, 2]\n', "must be an object", 1),
            (b'{"a": NaN}\n', "non-JSON constant", 1),
            (b'{"a": 1}\n', None, None),
        ]
        for payload, fragment, line in cases:
            with self.subTest(payload=payload):
                summary = self._run_raw(payload)
                if fragment is None:
                    self.assertTrue(summary.ok, summary.error)
                else:
                    self.assertEqual(summary.status, "failed")
                    error = summary.error
                    self.assertEqual(error["identity"]["source"], "in")
                    self.assertEqual(error["identity"]["line"], line)
                    self.assertIn(fragment, error["message"])

    def test_line_and_record_byte_limits_are_hard(self):
        wide = json.dumps({"a": "x" * 200})
        summary = self._run_raw((wide + "\n").encode(), max_line_bytes=100)
        self.assertEqual(summary.status, "failed")
        self.assertIn("max_line_bytes", summary.error["message"])
        self.assertEqual(summary.error["identity"]["line"], 1)
        self.assertFalse(os.path.exists(
            os.path.join(summary.results_dir, "out.jsonl")) and
            os.path.getsize(os.path.join(summary.results_dir,
                                        "out.jsonl")) > 0)

        summary = self._run_raw((json.dumps({"a": "x" * 200}) + "\n").encode(),
                                max_record_bytes=50)
        self.assertEqual(summary.status, "failed")
        self.assertIn("max_record_bytes", summary.error["message"])

    def test_graph_file_limits_apply_when_config_does_not(self):
        # Graph-declared limits are a hard cap as well.
        work = tempfile.mkdtemp(prefix="flow-limit-")
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"a": "x" * 50}])
        results = os.path.join(work, "r")
        from distagg.flow.graph import parse_graph

        graph = parse_graph({
            "max_record_bytes": 20,
            "nodes": [
                {"name": "in", "type": "source", "path": source},
                {"name": "out", "type": "sink", "path": "out.jsonl"},
            ],
            "edges": [{"from": "in", "to": "out"}],
        })
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertEqual(summary.status, "failed")
        self.assertIn("max_record_bytes", summary.error["message"])


class FailureInjectionTest(unittest.TestCase):
    def test_operator_error_reports_first_node_identity_and_stops_others(self):
        work = tempfile.mkdtemp(prefix="flow-operr-")
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": i} for i in range(1, 21)])
        results = os.path.join(work, "r")
        seen = []

        async def failing(record, emit):
            seen.append(record.line)
            if record.line == 5:
                raise RuntimeError("deliberate operator error")
            await emit(record)

        graph = FlowGraph()
        graph.source("in", source)
        graph.custom("boom", failing)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "boom")
        graph.add_edge("boom", "out")
        graph.validate()
        run = run_graph(graph, results, default_capacity=1,
                        max_inflight_per_node=1)

        async def scenario():
            summary = await run.run_to_completion()
            leftovers = [task for task in asyncio.all_tasks()
                         if task.get_name().startswith("flow:")]
            await run.aclose()
            return summary, leftovers

        summary, leftovers = asyncio.run(scenario())
        self.assertEqual(leftovers, [])
        self.assertEqual(summary.status, "failed")
        self.assertFalse(summary.ok)
        self.assertEqual(summary.error["node"], "boom")
        self.assertEqual(summary.error["identity"]["source"], "in")
        self.assertEqual(summary.error["identity"]["line"], 5)
        self.assertIn("deliberate operator error", summary.error["message"])
        self.assertIn("RuntimeError", summary.error["exception_kind"])
        self.assertIn(5, seen)
        # No further input after the failure: the source stopped being read.
        self.assertLessEqual(summary.sources["in"]["lines_pulled"], 8)
        # Partial output is kept and honestly counted, never marked complete.
        sink = summary.sinks["out"]
        self.assertFalse(sink["complete"])
        self.assertFalse(sink["closed_cleanly"])
        written = read_jsonl(os.path.join(results, "out.jsonl"))
        self.assertEqual(len(written), sink["records"])
        self.assertGreaterEqual(len(written), 1)
        # No task survives the failure.
        # The file can be reopened and read.
        with open(os.path.join(results, "out.jsonl"), encoding="utf-8") as fh:
            self.assertTrue(fh.read().endswith("\n") or fh.tell() == 0)

    def test_source_error_reports_source_line_and_stops(self):
        work = tempfile.mkdtemp(prefix="flow-srcerr-")
        source = os.path.join(work, "in.jsonl")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write('{"n": 1}\n{"n": 2}\nnot json\n{"n": 4}\n')
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "out")
        graph.validate()
        summary = asyncio.run(
            run_graph(graph, results).run_to_completion())
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.error["identity"],
                         {"source": "in", "line": 3, "expansion_path": []})
        # The sink is not complete; whatever it counted was really written.
        self.assertFalse(summary.sinks["out"]["complete"])
        self.assertLessEqual(summary.sinks["out"]["records"], 2)
        written = read_jsonl(os.path.join(results, "out.jsonl"))
        self.assertEqual(len(written), summary.sinks["out"]["records"])
        # Whatever was written is a prefix of the valid input lines.
        self.assertEqual([row["row"]["n"] for row in written],
                         list(range(1, len(written) + 1)))
        self.assertFalse(summary.sinks["out"]["complete"])

    def test_sink_write_failure_is_reported_with_identity(self):
        work = tempfile.mkdtemp(prefix="flow-sinkerr-")
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": i} for i in range(1, 6)])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "out")
        graph.validate()
        run = run_graph(graph, results)

        async def scenario():
            # Hold the sink at its input gate, so nothing has been written
            # yet, then swap its file for /dev/full.  Every write from then on
            # fails with a real ENOSPC, not a mocked exception.
            gate = run.add_gate("out")
            gate.pause()
            await run.start()
            await asyncio.wait_for(gate.wait_paused(), 5)
            node = run.run.nodes["out"]
            node.handle.close()
            node.handle = open("/dev/full", "wb")
            gate.resume()
            summary = await run.wait()
            await run.aclose()
            return summary

        summary = asyncio.run(scenario())
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.error["node"], "out")
        self.assertIn("sink write failed", summary.error["message"])
        self.assertEqual(summary.error["identity"]["source"], "in")
        self.assertIn(summary.error["identity"]["line"], (1, 2))
        self.assertFalse(summary.sinks["out"]["complete"])
        self.assertFalse(summary.sinks["out"]["closed_cleanly"])
        self.assertEqual(summary.error["identity"]["line"], 1)
        self.assertEqual(summary.sinks["out"]["records"], 0)

    def test_cancel_while_a_channel_is_full_cleans_up_promptly(self):
        work = tempfile.mkdtemp(prefix="flow-cancel-")
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": i} for i in range(1, 501)])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.select("pass", "n", "ge", 1)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "pass")
        graph.add_edge("pass", "out")
        graph.validate()
        run = run_graph(graph, results, default_capacity=1,
                        max_inflight_per_node=1)

        async def scenario():
            gate = run.add_gate("out")
            gate.pause()
            await run.start()
            await asyncio.wait_for(gate.wait_paused(), 5)
            # Let the pipeline fill every bounded buffer.
            for _ in range(200):
                await asyncio.sleep(0)
            snap = await run.snapshot()
            pulled = snap["sources"]["in"]["lines_pulled"]
            self.assertTrue(any(edge["peak_occupancy"] >= 1
                                for edge in snap["edges"]))
            started = time.monotonic()
            run.cancel()
            summary = await asyncio.wait_for(run.wait(), 10)
            elapsed = time.monotonic() - started
            leftovers = [task for task in asyncio.all_tasks()
                         if task.get_name().startswith("flow:")]
            await run.aclose()
            return summary, pulled, elapsed, leftovers

        summary, pulled, elapsed, leftovers = asyncio.run(scenario())
        self.assertEqual(leftovers, [])
        self.assertLess(elapsed, 5.0)
        self.assertEqual(summary.status, "cancelled")
        self.assertFalse(summary.ok)
        self.assertIsNone(summary.error)  # cancellation is not a failure
        self.assertLess(pulled, 20)
        sink = summary.sinks["out"]
        self.assertFalse(sink["complete"])
        # Partial output stays readable; everything claimed was really written.
        written = read_jsonl(os.path.join(results, "out.jsonl"))
        self.assertEqual(len(written), sink["records"])
        with open(os.path.join(results, "out.jsonl"), "rb"):
            pass

    def test_failure_wakes_a_task_blocked_on_a_full_channel(self):
        """A failure broadcast wakes consumers of empty and producers of full
        channels; the run returns instead of waiting for a slot that never
        frees."""
        work = tempfile.mkdtemp(prefix="flow-wake-")
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": i} for i in range(1, 20)])
        results = os.path.join(work, "r")

        async def passthrough(record, emit):
            await emit(record)

        async def boom(record, emit):
            raise ValueError("deliberate boom")

        graph = FlowGraph()
        graph.source("in", source)
        graph.custom("c", passthrough)
        graph.custom("boom", boom)
        graph.sink("out", "out.jsonl")
        graph.sink("other", "other.jsonl")
        graph.add_edge("in", "c")
        graph.add_edge("c", "out")
        graph.add_edge("in", "boom")
        graph.add_edge("boom", "other")
        graph.validate()
        run = run_graph(graph, results, default_capacity=1,
                        max_inflight_per_node=1)

        async def scenario():
            out_gate = run.add_gate("out")
            boom_gate = run.add_gate("boom")
            out_gate.pause()
            boom_gate.pause()
            await run.start()
            # Wait until the sink is actually blocked at its barrier; at that
            # point c is blocked pushing into a full/credit-limited channel.
            await asyncio.wait_for(out_gate.wait_paused(), 5)
            snapshot = await run.snapshot()
            # Now inject the failure from the other branch: its node is
            # blocked at its own gate and will raise on the first record.
            boom_gate.resume()
            summary = await asyncio.wait_for(run.wait(), 10)
            leftovers = [task for task in asyncio.all_tasks()
                         if task.get_name().startswith("flow:")]
            await run.aclose()
            return summary, leftovers, snapshot

        summary, leftovers, snapshot = asyncio.run(scenario())
        self.assertEqual(leftovers, [])
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.error["node"], "boom")
        self.assertEqual(summary.error["identity"]["line"], 1)
        self.assertIn("deliberate boom", summary.error["message"])
        # The other branch was genuinely in flight, not a mock.
        self.assertLess(snapshot["sources"]["in"]["lines_pulled"], 19)
        self.assertFalse(summary.sinks["out"]["complete"])
        # Everything the failed sink claims is on disk.
        written = read_jsonl(os.path.join(results, "out.jsonl"))
        self.assertEqual(len(written), summary.sinks["out"]["records"])

    def test_cancel_before_start_does_not_create_outputs(self):
        work = tempfile.mkdtemp(prefix="flow-cancel-pre-")
        source = write_jsonl(os.path.join(work, "in.jsonl"), [{"n": 1}])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "out")
        graph.validate()
        run = start_run(graph, FlowConfig(results))

        async def scenario():
            await run.start()
            run.cancel()
            await run.aclose()

        asyncio.run(scenario())
        # The fresh directory was created, but no sink file was written.
        self.assertTrue(os.path.isdir(results))
        self.assertFalse(os.path.exists(os.path.join(results, "out.jsonl")))
        self.assertEqual(os.listdir(results), [])


class ResultDirectoryTest(unittest.TestCase):
    def test_existing_result_directory_is_never_overwritten(self):
        work = tempfile.mkdtemp(prefix="flow-results-")
        source = write_jsonl(os.path.join(work, "in.jsonl"), [{"n": 1}])
        results = os.path.join(work, "r")
        os.mkdir(results)
        with open(os.path.join(results, "sentinel"), "w") as handle:
            handle.write("keep")
        graph = FlowGraph()
        graph.source("in", source)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "out")
        graph.validate()
        run = start_run(graph, FlowConfig(results))
        summary = asyncio.run(run.run_to_completion())
        self.assertEqual(summary.status, "failed")
        self.assertIn("already exists", summary.error["message"])
        self.assertEqual(sorted(os.listdir(results)), ["sentinel"])

    def test_two_runs_get_distinct_directories(self):
        work = tempfile.mkdtemp(prefix="flow-fresh-")
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": 1}, {"n": 2}])
        graph = FlowGraph()
        graph.source("in", source)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "out")
        graph.validate()

        seen = set()

        async def once():
            run = start_run(graph, FlowConfig(None, default_capacity=2),
                            base_dir=work)
            summary = await run.run_to_completion()
            seen.add(summary.results_dir)
            self.assertTrue(summary.ok, summary.error)
            await run.aclose()

        asyncio.run(once())
        asyncio.run(once())
        self.assertEqual(len(seen), 2)
        for directory in seen:
            self.assertTrue(os.path.isdir(directory))
            self.assertEqual(len(read_jsonl(os.path.join(directory,
                                                         "out.jsonl"))), 2)

    def test_result_stream_callback_rows_match_files(self):
        work = tempfile.mkdtemp(prefix="flow-iter-")
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": i} for i in range(1, 7)])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.select("pass", "n", "ge", 1)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "pass")
        graph.add_edge("pass", "out")
        graph.validate()

        async def scenario():
            run = start_run(graph, FlowConfig(
                results, default_capacity=2, max_inflight_per_node=2,
                result_stream=True, result_capacity=2))
            await run.start()
            items = []
            async for sink_name, identity, row in run.iter_results():
                items.append((sink_name, identity, row))
            summary = await run.wait()
            await run.aclose()
            return items, summary

        items, summary = asyncio.run(scenario())
        self.assertTrue(summary.ok, summary.error)
        self.assertEqual(len(items), 6)
        self.assertEqual([item[0] for item in items], ["out"] * 6)
        self.assertEqual([item[2]["n"] for item in items], [1, 2, 3, 4, 5, 6])
        self.assertEqual([item[1]["line"] for item in items], [1, 2, 3, 4, 5, 6])
        self.assertEqual(summary.sinks["out"]["records"], 6)

    def test_closing_result_iterator_early_cancels_the_run(self):
        work = tempfile.mkdtemp(prefix="flow-iter-close-")
        source = write_jsonl(os.path.join(work, "in.jsonl"),
                             [{"n": i} for i in range(1, 2001)])
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "out")
        graph.validate()

        async def scenario():
            run = start_run(graph, FlowConfig(
                results, default_capacity=1, max_inflight_per_node=1,
                result_stream=True, result_capacity=1))
            await run.start()
            iterator = run.iter_results()
            first = await iterator.__anext__()
            await iterator.aclose()  # close early
            summary = await asyncio.wait_for(run.wait(), 10)
            await run.aclose()
            return first, summary

        first, summary = asyncio.run(scenario())
        self.assertEqual(first[0], "out")
        self.assertEqual(summary.status, "cancelled")
        self.assertLess(summary.sinks["out"]["records"], 2000)
        self.assertLess(summary.sources["in"]["lines_pulled"], 2000)


class MemoryBoundTest(unittest.TestCase):
    """Peak occupancy is capped and the run's declared bound is honest.

    The real RSS doubling measurement lives in ``tools/flow_stress.py`` (two
    sizes, one process, line-by-line consumption); this test asserts the
    mechanism that keeps memory flat: every edge stays at or below its
    capacity, the source is throttled by the bounded pipeline, and the record
    bound the summary reports is a function of the configuration only.
    """

    def _run(self, count):
        work = tempfile.mkdtemp(prefix=f"flow-mem-{count}-")
        source = os.path.join(work, "in.jsonl")
        with open(source, "w", encoding="utf-8") as handle:
            for i in range(count):
                handle.write(json.dumps({"n": i, "pad": "x" * 64}) + "\n")
        results = os.path.join(work, "r")
        graph = FlowGraph()
        graph.source("in", source)
        graph.select("keep", "n", "ge", 0)
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "keep")
        graph.add_edge("keep", "out")
        graph.validate()

        async def scenario():
            run = start_run(graph, FlowConfig(
                results, default_capacity=2, max_inflight_per_node=2))
            await run.start()
            summary = await run.wait()
            await run.aclose()
            return summary

        return asyncio.run(scenario())

    def test_occupancy_is_capped_and_the_declared_bound_is_configuration_only(self):
        small = self._run(500)
        large = self._run(5000)
        self.assertTrue(small.ok, small.error)
        self.assertTrue(large.ok, large.error)
        for summary in (small, large):
            for edge in summary.edges:
                self.assertLessEqual(edge["peak_occupancy"], edge["capacity"])
            bound = summary["buffers"]["theoretical_record_bound"]
            self.assertEqual(bound, summary["buffers"]["edge_capacity_records"]
                             + summary["buffers"]["inflight_credit_total"])
            self.assertLessEqual(bound, 64)
        self.assertEqual(small["buffers"]["theoretical_record_bound"],
                         large["buffers"]["theoretical_record_bound"])
        self.assertEqual(small.sinks["out"]["records"], 500)
        self.assertEqual(large.sinks["out"]["records"], 5000)


if __name__ == "__main__":
    unittest.main()
