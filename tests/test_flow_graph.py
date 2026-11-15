"""Graph definition, static validation and rejection-before-side-effects.

These tests never need a running event loop for the validation part; they use
``unittest`` like the rest of the repository's non-pytest modules.
"""

import json
import os
import tempfile
import unittest

from distagg.flow import FlowConfig, FlowGraph, GraphError, load_graph, parse_graph
from distagg.flow.engine import start_run


def base_document():
    return {
        "name": "t",
        "nodes": [
            {"name": "in", "type": "source", "path": "in.jsonl"},
            {"name": "ok", "type": "select", "field": "s", "condition": "eq",
             "value": "x"},
            {"name": "flat", "type": "project", "fields": {"o": "s"}},
            {"name": "exp", "type": "explode", "field": "items"},
            {"name": "m", "type": "merge"},
            {"name": "out", "type": "sink", "path": "out.jsonl"},
        ],
        "edges": [
            {"from": "in", "to": "ok"},
            {"from": "in", "to": "exp"},
            {"from": "ok", "to": "flat"},
            {"from": "flat", "to": "m"},
            {"from": "exp", "to": "m"},
            {"from": "m", "to": "out"},
        ],
    }


class GraphValidationTest(unittest.TestCase):
    def _problems(self, document):
        with self.assertRaises(GraphError) as ctx:
            parse_graph(document)
        return " | ".join(ctx.exception.problems)

    def test_valid_document_parses(self):
        graph = parse_graph(base_document())
        self.assertEqual(len(graph.nodes), 6)
        self.assertEqual(len(graph.edges), 6)
        self.assertEqual(set(graph.order), {n.name for n in graph.nodes})
        # Topological order respects every edge.
        position = {name: i for i, name in enumerate(graph.order)}
        for edge in graph.edges:
            self.assertLess(position[edge.source], position[edge.target])

    def test_cyclic_graph_rejected_with_cycle(self):
        document = base_document()
        document["edges"].append({"from": "out", "to": "ok"})
        # A sink may not have outputs either; both problems are reported.
        problems = self._problems(document)
        self.assertIn("cycle", problems)

    def test_cycle_between_processing_nodes_reported(self):
        document = {
            "nodes": [
                {"name": "s", "type": "source", "path": "x"},
                {"name": "a", "type": "explode", "field": "f"},
                {"name": "b", "type": "explode", "field": "f"},
                {"name": "out", "type": "sink", "path": "o"},
            ],
            "edges": [
                {"from": "s", "to": "a"},
                {"from": "a", "to": "b"},
                {"from": "b", "to": "a"},
                {"from": "b", "to": "out"},
            ],
        }
        problems = self._problems(document)
        self.assertIn("cycle", problems)
        self.assertIn("a -> b -> a", problems)

    def test_node_not_leading_to_a_sink_rejected(self):
        document = base_document()
        document["nodes"].append({"name": "orphan", "type": "explode",
                                  "field": "items"})
        document["edges"].append({"from": "ok", "to": "orphan"})
        problems = self._problems(document)
        self.assertIn("do not lead to any sink", problems)
        self.assertIn("orphan", problems)

    def test_duplicate_node_name_rejected(self):
        document = base_document()
        document["nodes"].append({"name": "ok", "type": "explode",
                                  "field": "items"})
        self.assertIn("duplicate node name", self._problems(document))

    def test_duplicate_edge_rejected(self):
        document = base_document()
        document["edges"].append({"from": "in", "to": "ok"})
        self.assertIn("duplicate edge", self._problems(document))

    def test_dangling_edges_rejected(self):
        document = base_document()
        document["edges"].append({"from": "in", "to": "nope"})
        self.assertIn("unknown node", self._problems(document))
        document = base_document()
        document["edges"].append({"from": "ghost", "to": "out"})
        self.assertIn("unknown node", self._problems(document))

    def test_input_count_rules(self):
        # Two inputs to a plain node.
        document = base_document()
        document["edges"].append({"from": "exp", "to": "ok"})
        self.assertIn("exactly one input", self._problems(document))
        # A merge with one input.
        document = base_document()
        document["edges"] = [
            edge for edge in document["edges"] if edge["to"] != "m"]
        document["edges"].append({"from": "flat", "to": "m"})
        self.assertIn("at least two inputs", self._problems(document))
        # A source with an input.
        document = base_document()
        document["nodes"].append({"name": "s2", "type": "source", "path": "y"})
        document["edges"].append({"from": "ok", "to": "s2"})
        self.assertIn("must have no input", self._problems(document))
        # A sink with an output.
        document = base_document()
        document["edges"].append({"from": "out", "to": "flat"})
        problems = self._problems(document)
        self.assertIn("must have no output", problems)

    def test_sink_without_input_rejected(self):
        document = base_document()
        document["nodes"].append({"name": "extra", "type": "sink",
                                  "path": "x.jsonl"})
        self.assertIn("has no input", self._problems(document))

    def test_unknown_members_rejected(self):
        document = base_document()
        document["nodes"][1]["valuee"] = 1
        self.assertIn("unknown setting", self._problems(document))
        document = base_document()
        document["extra"] = True
        self.assertIn("unknown top-level", self._problems(document))

    def test_select_condition_static_checks(self):
        for op, value, fragment in [
            ("like", 1, "must be one of"),
            ("lt", 1.5, "integer, string, boolean or null"),
            ("eq", [1], "integer, string, boolean or null"),
        ]:
            with self.subTest(op=op, value=value):
                document = base_document()
                document["nodes"][1].update(condition=op, value=value)
                self.assertIn(fragment, self._problems(document))
        document = base_document()
        document["nodes"][1] = {"name": "ok", "type": "select",
                                "field": "s", "condition": "is_null",
                                "value": None}
        self.assertIn("must be omitted", self._problems(document))

    def test_project_and_explode_static_checks(self):
        document = base_document()
        document["nodes"][2]["fields"] = {}
        self.assertIn("non-empty", self._problems(document))
        document = base_document()
        document["nodes"][2]["fields"] = {"out": 3}
        self.assertIn("non-empty string", self._problems(document))
        document = base_document()
        del document["nodes"][3]["field"]
        self.assertIn("missing", self._problems(document).lower())

    def test_ordering_comparison_type_error_is_static(self):
        # A string compared with lt requires a string expected value; the pair
        # is only knowable at runtime, but a float expected value is static.
        document = base_document()
        document["nodes"][1].update(condition="lt", value=1.5)
        self.assertIn("integer, string, boolean or null",
                      self._problems(document))

    def test_edge_capacity_must_be_positive(self):
        document = base_document()
        document["edges"][0]["capacity"] = 0
        self.assertIn("positive integer", self._problems(document))
        document = base_document()
        document["edges"][0]["capacity"] = True
        self.assertIn("positive integer", self._problems(document))

    def test_sink_paths_must_be_relative_and_unique(self):
        document = base_document()
        document["nodes"][5]["path"] = "../escape.jsonl"
        self.assertIn("inside the result directory", self._problems(document))
        document = base_document()
        document["nodes"][5]["path"] = "/abs.jsonl"
        self.assertIn("relative to the result directory",
                      self._problems(document))
        document = base_document()
        document["nodes"].append({"name": "out2", "type": "sink",
                                  "path": "./out.jsonl"})
        document["edges"].append({"from": "ok", "to": "out2"})
        self.assertIn("both write", self._problems(document))

    def test_custom_nodes_are_api_only(self):
        document = base_document()
        document["nodes"].append({"name": "c", "type": "custom"})
        document["edges"].append({"from": "ok", "to": "c"})
        document["edges"].append({"from": "c", "to": "m"})
        self.assertIn("Python-API only", self._problems(document))

    def test_duplicate_json_members_in_graph_document_rejected(self):
        path = os.path.join(tempfile.mkdtemp(prefix="flowgraph-"), "g.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"nodes": [], "nodes": []}')
        with self.assertRaises(GraphError) as ctx:
            load_graph(path)
        self.assertIn("duplicate JSON member", str(ctx.exception))

    def test_graph_file_paths_are_relative_to_the_file(self):
        directory = tempfile.mkdtemp(prefix="flowgraph-")
        document = base_document()
        os.makedirs(os.path.join(directory, "data"), exist_ok=True)
        open(os.path.join(directory, "data", "in.jsonl"), "w").close()
        document["nodes"][0]["path"] = "data/in.jsonl"
        path = os.path.join(directory, "g.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        graph = load_graph(path)
        self.assertEqual(
            graph.node("in").options["path"],
            os.path.join(directory, "data", "in.jsonl"),
        )


class RejectedBeforeSideEffectsTest(unittest.TestCase):
    """A bad graph must fail before sources are read or results are created."""

    def test_bad_graph_never_opens_source_or_creates_results(self):
        directory = tempfile.mkdtemp(prefix="flowbad-")
        source = os.path.join(directory, "in.jsonl")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write('{"s": "x", "items": [1]}\n')
        results = os.path.join(directory, "results")
        document = base_document()
        document["nodes"][0]["path"] = source
        document["edges"].append({"from": "out", "to": "ok"})  # cycle + sink out
        with self.assertRaises(GraphError):
            parse_graph(document)
        # The helper ``start_run`` is the only place that could touch the disk;
        # validation happens on the graph itself, so nothing exists yet.
        self.assertFalse(os.path.exists(results))
        self.assertTrue(os.path.exists(source))

    def test_start_run_rejects_bad_graph_synchronously(self):
        graph = FlowGraph()
        graph.source("in", "/nonexistent/in.jsonl")
        graph.sink("out", "out.jsonl")
        # No edge -> both node-level degree errors.
        with self.assertRaises(GraphError):
            start_run(graph, FlowConfig("/tmp/never-created-flow-dir"))
        self.assertFalse(os.path.exists("/tmp/never-created-flow-dir"))

    def test_oversized_graph_rejected(self):
        graph = FlowGraph()
        graph.source("in", "/nonexistent/in.jsonl")
        graph.explode("e", "items")
        graph.sink("out", "out.jsonl")
        graph.add_edge("in", "e")
        graph.add_edge("e", "out")
        graph.validate()
        with self.assertRaises(GraphError):
            start_run(graph, FlowConfig("/tmp/never-created-flow-dir",
                                        max_nodes=2))
        self.assertFalse(os.path.exists("/tmp/never-created-flow-dir"))

    def test_cli_check_rejects_without_creating_anything(self):
        import subprocess
        import sys

        directory = tempfile.mkdtemp(prefix="flowcli-")
        graph_path = os.path.join(directory, "bad.json")
        document = base_document()
        document["edges"].append({"from": "out", "to": "ok"})
        with open(graph_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = os.path.join(root, "src") + os.pathsep + \
            env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "distagg", "flow", "check", graph_path],
            capture_output=True, text=True, env=env, cwd=directory)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "graph_error")
        self.assertTrue(payload["problems"])
        self.assertEqual(sorted(os.listdir(directory)), ["bad.json"])


if __name__ == "__main__":
    unittest.main()
