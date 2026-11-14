"""CLI tests for the staging store: hex handling, JSON output, distagg entry."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def cli_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_cli(args, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True, env=cli_env(), **kwargs
    )


class StagingCliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="staging-cli-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store_dir = os.path.join(self.dir, "store")

    def test_put_get_scan_delete_via_cli(self):
        proc = run_cli(["staging", "put", self.store_dir, "0061", "ff00"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["commit"], 1)

        proc = run_cli(["staging", "get", self.store_dir, "0061"])
        self.assertEqual(json.loads(proc.stdout), {"found": True, "value_hex": "ff00"})

        proc = run_cli(["staging", "get", self.store_dir, "0062"])
        self.assertEqual(json.loads(proc.stdout), {"found": False, "value_hex": None})

        proc = run_cli(["staging", "delete", self.store_dir, "0061"])
        self.assertEqual(json.loads(proc.stdout)["commit"], 2)
        proc = run_cli(["staging", "get", self.store_dir, "0061"])
        self.assertEqual(json.loads(proc.stdout)["found"], False)

    def test_empty_key_via_cli(self):
        proc = run_cli(["staging", "put", self.store_dir, "", "aa"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_cli(["staging", "scan", self.store_dir])
        entries = json.loads(proc.stdout)["entries"]
        self.assertEqual(entries, [{"key_hex": "", "value_hex": "aa"}])

    def test_invalid_hex_is_rejected(self):
        proc = run_cli(["staging", "put", self.store_dir, "xyz", "aa"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "invalid_operation")
        proc = run_cli(["staging", "put", self.store_dir, "abc", "aa"])
        self.assertEqual(proc.returncode, 1)  # odd length

    def test_batch_file_and_scan_bounds(self):
        ops = [
            {"op": "put", "key": "61", "value": "41"},      # a = A
            {"op": "put", "key": "62", "value": "42"},      # b = B
            {"op": "put", "key": "63", "value": "43"},      # c = C
            {"op": "delete", "key": "62"},
        ]
        ops_file = os.path.join(self.dir, "ops.json")
        with open(ops_file, "w") as handle:
            json.dump(ops, handle)
        proc = run_cli(["staging", "batch", self.store_dir, ops_file])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_cli(["staging", "scan", self.store_dir, "--lower", "61", "--upper", "63"])
        entries = json.loads(proc.stdout)["entries"]
        self.assertEqual(entries, [{"key_hex": "61", "value_hex": "41"}])

    def test_flush_compact_stats_via_cli(self):
        for i in range(6):
            proc = run_cli(["staging", "put", self.store_dir, "6%d" % i, "56"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_cli(["staging", "flush", self.store_dir])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_cli(["staging", "compact", self.store_dir])
        result = json.loads(proc.stdout)
        self.assertEqual(result["segments_after"], 1)
        proc = run_cli(["staging", "stats", self.store_dir])
        stats = json.loads(proc.stdout)
        self.assertEqual(stats["segment_count"], 1)
        self.assertEqual(stats["commit"], 6)
        self.assertEqual(stats["total_segment_records"], 6)

    def test_get_with_lookup_stats(self):
        run_cli(["staging", "put", self.store_dir, "aa", "bb"])
        run_cli(["staging", "flush", self.store_dir])
        proc = run_cli(["staging", "get", self.store_dir, "aa", "--stats"])
        result = json.loads(proc.stdout)
        self.assertTrue(result["found"])
        self.assertEqual(result["lookup"]["blocks_read"], 1)

    def test_distagg_entrypoint_still_works(self):
        proc = run_cli(["--version"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("0.1.0", proc.stdout)
        # The original aggregation demo still runs end to end.
        db = os.path.join(self.dir, "demo.db")
        proc = run_cli(["demo", db])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_cli(["check", db])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
