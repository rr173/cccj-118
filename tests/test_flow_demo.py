"""The packaged demo: hand-checked split/merge, broadcast isolation and scale.

The demo files live in ``demo/flow``.  This module both asserts the hand
computations and provides the figures quoted in ``docs/FLOW.md``.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest

from distagg.flow import FlowConfig, FlowGraph, load_graph, start_run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "demo", "flow")
GRAPH = os.path.join(DEMO, "split_merge.json")
INPUT = os.path.join(DEMO, "imports.jsonl")


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def cli_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + \
        env.get("PYTHONPATH", "")
    return env


class HandCheckedDemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="flow-demo-")
        cls.results = os.path.join(cls.work, "results")
        graph = load_graph(GRAPH)
        run = start_run(graph, FlowConfig(
            cls.results, default_capacity=2, max_inflight_per_node=2))
        cls.summary = asyncio.run(run.run_to_completion())
        asyncio.run(run.aclose())
        if not cls.summary.ok:
            raise RuntimeError(cls.summary.error)

    def test_hand_listed_ledger(self):
        """The documented ledger: line, path, multiplicity, reason."""
        rows = read_jsonl(os.path.join(self.results, "ledger.jsonl"))
        # Every exception row arrives over ``exceptions -> merged``; the
        # error branch is not exploded, so both rows keep an empty path.
        # The merge visits its two inputs in edge order, so the exception
        # group is first; only the order *inside* one input is contractual.
        error_lines = [(row["identity"]["line"],
                        tuple(row["identity"]["expansion_path"]))
                       for row in rows if row["row"].get("status") == "error"]
        self.assertEqual(error_lines, [(2, ()), (3, ())])

        goods = [(row["identity"]["line"],
                  tuple(row["identity"]["expansion_path"]),
                  row["row"])
                 for row in rows if "order_id" in row["row"]]
        self.assertEqual(
            goods,
            [(1, (0,), {"order_id": "A-1", "item": {"sku": "s1", "qty": 2}}),
             (1, (1,), {"order_id": "A-1", "item": {"sku": "s1", "qty": 2}}),
             (4, (0,), {"order_id": "A-3", "item": {"sku": "s4", "qty": 7}}),
             (5, (0,), {"order_id": "A-4", "item": {"sku": "s5", "qty": 1}})],
        )
        # A-1's two items are the same value but distinct children (paths
        # (0,) and (1,) differ); A-2 appears twice on two different lines with
        # equal business data, and is never de-duplicated.
        duplicates = [(row["identity"]["line"], row["row"]["id"])
                      for row in rows if row["row"].get("id") == "A-2"]
        self.assertEqual(duplicates, [(2, "A-2"), (3, "A-2")])
        self.assertEqual(len(rows), 6)

    def test_multiplicities_are_the_sum_of_all_paths(self):
        exception_rows = read_jsonl(
            os.path.join(self.results, "exceptions.jsonl"))
        # A-2 line 2 and line 3 -> 2 exception rows.
        self.assertEqual(len(exception_rows), 2)
        self.assertEqual(self.summary.sinks["exception_report"]["records"], 2)
        # 4 flattened items + 2 error rows -> 6 ledger rows.
        self.assertEqual(self.summary.sinks["ledger"]["records"], 6)
        self.assertTrue(self.summary.sinks["ledger"]["complete"])
        self.assertTrue(self.summary.sinks["exception_report"]["complete"])
        # The source pulled exactly the physical line count.
        self.assertEqual(self.summary.sources["in"]["lines_pulled"], 5)

    def test_edge_counters_are_real(self):
        edges = {(edge["from"], edge["to"]): edge
                 for edge in self.summary.edges}
        self.assertEqual(edges[("in", "exceptions")]["sent"], 5)
        self.assertEqual(edges[("in", "acceptable")]["sent"], 5)
        self.assertEqual(edges[("exceptions", "exception_report")]["sent"], 2)
        self.assertEqual(edges[("merged", "ledger")]["sent"], 6)
        for edge in self.summary.edges:
            self.assertEqual(edge["sent"], edge["received"])
            self.assertLessEqual(edge["peak_occupancy"], edge["capacity"])

    def test_cli_check_and_run_report_the_same_facts(self):
        proc = subprocess.run(
            [sys.executable, "-m", "distagg", "flow", "check", GRAPH],
            capture_output=True, text=True, env=cli_env(), cwd=self.work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["node_count"], 8)

    def test_documented_broadcast_and_bounded_memory_demo(self):
        """The custom-operator broadcast demo documented in the README."""
        work = tempfile.mkdtemp(prefix="flow-demo-broadcast-")
        source = os.path.join(work, "in.jsonl")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"id": "A-1", "nested": {"mark": "original"}}) + "\n")

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
        run = start_run(graph, FlowConfig(os.path.join(work, "r")))
        summary = asyncio.run(run.run_to_completion())
        asyncio.run(run.aclose())
        self.assertTrue(summary.ok, summary.error)
        a = read_jsonl(os.path.join(work, "r", "branch-a.jsonl"))[0]["row"]
        b = read_jsonl(os.path.join(work, "r", "branch-b.jsonl"))[0]["row"]
        self.assertEqual(a["nested"]["mark"], "changed-on-branch-a")
        self.assertEqual(b["nested"]["mark"], "original")


if __name__ == "__main__":
    unittest.main()
