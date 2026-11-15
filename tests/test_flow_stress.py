import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest

from distagg.flow import FlowConfig, FlowGraph, start_run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StressToolTest(unittest.TestCase):
    """The reproducible scale driver doubles the input and must stay flat."""

    def test_flow_stress_reports_flat_memory_and_bounded_edges(self):
        work = tempfile.mkdtemp(prefix="flow-stress-test-")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + \
            env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "flow_stress.py"),
             "--rows", "300", "--capacity", "2", "--inflight", "2",
             "--results", work, "--json"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        small, large = payload["measurements"]
        self.assertEqual(small["source_lines"], 300)
        self.assertEqual(large["source_lines"], 600)
        self.assertEqual(large["consumed"], 4 * 600)
        self.assertEqual(small["peak_edge_occupancy"], 2)
        self.assertEqual(large["peak_edge_occupancy"], 2)
        self.assertEqual(small["record_bound"], large["record_bound"])
        self.assertLessEqual(large["peak_edge_occupancy"], 2)
        self.assertLess(large["rss_delta_kib"], 50_000)
        with open(os.path.join(work, "run-600", "out.jsonl"),
                  encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(rows), 2400)


if __name__ == "__main__":
    unittest.main()
