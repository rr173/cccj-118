"""CLI contract for ``distagg flow check`` / ``distagg flow run``."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "demo", "flow")
GRAPH = os.path.join(DEMO, "split_merge.json")


def run_cli(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + \
        env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True, env=env,
        cwd=cwd or tempfile.gettempdir())


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class FlowCliTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="flow-cli-")

    def test_check_does_not_create_a_result_directory(self):
        proc = run_cli("flow", "check", GRAPH, cwd=self.work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["edge_count"], 8)
        self.assertEqual(sorted(os.listdir(self.work)), [])

    def test_run_writes_summary_and_outputs(self):
        results = os.path.join(self.work, "run")
        proc = run_cli("flow", "run", GRAPH, "--results", results,
                       "--capacity", "2", "--max-inflight", "2",
                       cwd=self.work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["status"], "success")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["sinks"]["ledger"]["records"], 6)
        self.assertEqual(summary["sources"]["in"]["lines_pulled"], 5)
        self.assertEqual(len(read_jsonl(os.path.join(results,
                                                     "ledger.jsonl"))), 6)
        for edge in summary["edges"]:
            self.assertIn("sent", edge)
            self.assertIn("received", edge)
            self.assertIn("peak_occupancy", edge)
            self.assertEqual(edge["sent"], edge["received"])

    def test_existing_result_dir_is_refused(self):
        results = os.path.join(self.work, "run")
        os.mkdir(results)
        with open(os.path.join(results, "keep"), "w") as handle:
            handle.write("x")
        proc = run_cli("flow", "run", GRAPH, "--results", results, cwd=self.work)
        self.assertEqual(proc.returncode, 1)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["ok"])
        self.assertIn("already exists", summary["error"]["message"])
        self.assertEqual(sorted(os.listdir(results)), ["keep"])

    def test_bad_input_failure_has_nonzero_exit_and_no_success_summary(self):
        directory = tempfile.mkdtemp(prefix="flow-cli-bad-")
        source = os.path.join(directory, "in.jsonl")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write('{"n": 1}\n\n')
        graph_doc = {
            "nodes": [
                {"name": "in", "type": "source", "path": source},
                {"name": "out", "type": "sink", "path": "out.jsonl"},
            ],
            "edges": [{"from": "in", "to": "out"}],
        }
        graph_path = os.path.join(directory, "g.json")
        with open(graph_path, "w", encoding="utf-8") as handle:
            json.dump(graph_doc, handle)
        results = os.path.join(directory, "r")
        proc = run_cli("flow", "run", graph_path, "--results", results,
                       cwd=directory)
        self.assertEqual(proc.returncode, 1)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["error"]["identity"]["line"], 2)
        self.assertIn("blank line", summary["error"]["message"])
        self.assertFalse(summary["sinks"]["out"]["complete"])
        self.assertTrue(proc.stdout.strip())
        self.assertIn("error", proc.stderr.lower())

    def test_bad_graph_is_rejected_before_anything_is_opened(self):
        directory = tempfile.mkdtemp(prefix="flow-cli-badgraph-")
        source = os.path.join(directory, "in.jsonl")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write('{"n": 1}\n')
        os.chmod(source, 0o000)
        with open(GRAPH, encoding="utf-8") as handle:
            good = json.load(handle)
        good["nodes"][0]["path"] = source
        good["edges"].append({"from": "ledger", "to": "items"})
        graph_path = os.path.join(directory, "bad.json")
        with open(graph_path, "w", encoding="utf-8") as handle:
            json.dump(good, handle)
        try:
            proc = run_cli("flow", "check", graph_path, cwd=directory)
            run_proc = run_cli("flow", "run", graph_path, "--results",
                               os.path.join(directory, "r"), cwd=directory)
        finally:
            os.chmod(source, 0o644)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "graph_error")
        self.assertEqual(run_proc.returncode, 1)
        self.assertEqual(json.loads(run_proc.stdout)["error"], "graph_error")
        self.assertFalse(os.path.exists(os.path.join(directory, "r")))

    def test_existing_commands_still_available(self):
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        for name in ("flow", "staging", "copybook", "window", "submit",
                     "summary", "base", "receipt", "receipts", "check",
                     "plans", "explain", "demo"):
            self.assertIn(name, proc.stdout)
        for group in ("flow", "staging", "copybook", "window"):
            proc = run_cli(group, "--help")
            self.assertEqual(proc.returncode, 0, group + proc.stderr)


if __name__ == "__main__":
    unittest.main()
