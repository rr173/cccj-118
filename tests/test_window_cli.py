"""CLI, atomic replacement and temporary-workspace cleanup tests."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

DECLARATION = {
    "partition_by": ["g"],
    "order_by": "ts",
    "value": "amount",
    "lag_lead_k": 1,
    "windows": {
        "w": {"mode": "ROWS", "start": -1, "end": 1, "exclude": "none"}
    },
}


def run_cli(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    # Run from a neutral cwd so a planted json.py in the data directory can
    # never shadow the standard library.
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True, env=env, cwd=tempfile.gettempdir())


class WindowCliTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="window-cli-")
        self.in_path = os.path.join(self.work, "in.jsonl")
        self.out_path = os.path.join(self.work, "out.jsonl")
        self.decl_path = os.path.join(self.work, "decl.json")
        with open(self.in_path, "w", encoding="utf-8") as handle:
            for i in range(1, 6):
                handle.write(json.dumps(
                    {"g": "a", "ts": i, "amount": i}) + "\n")
        with open(self.decl_path, "w", encoding="utf-8") as handle:
            json.dump(DECLARATION, handle)

    def _temp_entries(self):
        return [name for name in os.listdir(self.work)
                if name.startswith("distagg-window-")]

    def test_success_creates_output_and_cleans_workspace(self):
        proc = run_cli("window", "run", self.in_path, self.out_path,
                       "--declaration", self.decl_path,
                       "--batch-size", "2", "--spill-threshold", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["rows_read"], 5)
        self.assertTrue(os.path.exists(self.out_path))
        # The private temp workspace is removed; only the declared files and
        # the new output remain.
        self.assertEqual(self._temp_entries(), [])
        self.assertEqual(
            sorted(os.listdir(self.work)),
            sorted(["in.jsonl", "out.jsonl", "decl.json"]),
        )

    def test_input_and_output_same_file_rejected(self):
        proc = run_cli("window", "run", self.in_path, self.in_path,
                       "--declaration", self.decl_path)
        self.assertEqual(proc.returncode, 1)
        error = json.loads(proc.stdout)
        self.assertEqual(error["error"], "window_error")
        # Input untouched.
        with open(self.in_path, encoding="utf-8") as handle:
            self.assertEqual(sum(1 for line in handle if line.strip()), 5)

    def test_invalid_declaration_fails_before_touching_output(self):
        bad_decl = os.path.join(self.work, "bad.json")
        with open(bad_decl, "w", encoding="utf-8") as handle:
            json.dump({"partition_by": ["g"], "order_by": "ts",
                       "value": "amount", "lag_lead_k": 1,
                       "windows": {"w": {"mode": "ROWS", "start": 5,
                                         "end": 1, "exclude": "none"}}},
                      handle)
        proc = run_cli("window", "run", self.in_path, self.out_path,
                       "--declaration", bad_decl)
        self.assertEqual(proc.returncode, 1)
        error = json.loads(proc.stdout)
        self.assertEqual(error["error"], "window_declaration_error")
        self.assertFalse(os.path.exists(self.out_path))
        self.assertEqual(self._temp_entries(), [])

    def test_old_result_preserved_on_bad_last_line(self):
        sentinel = "PREVIOUS RESULT - MUST SURVIVE\n"
        with open(self.out_path, "w", encoding="utf-8") as handle:
            handle.write(sentinel)
        bad_in = os.path.join(self.work, "bad.jsonl")
        with open(bad_in, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"g": "a", "ts": 1, "amount": 1}) + "\n")
            handle.write("not-json\n")
        proc = run_cli("window", "run", bad_in, self.out_path,
                       "--declaration", self.decl_path)
        self.assertEqual(proc.returncode, 1)
        error = json.loads(proc.stdout)
        self.assertEqual(error["line_number"], 2)
        with open(self.out_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), sentinel)
        self.assertEqual(self._temp_entries(), [])

    def test_write_failure_keeps_old_result_and_cleans_temp(self):
        # A read-only directory makes the final os.replace fail; the scratch
        # workspace lives next to the target and must still be removed.
        readonly_dir = os.path.join(self.work, "readonly")
        os.mkdir(readonly_dir)
        os.chmod(readonly_dir, 0o500)
        try:
            impossible = os.path.join(readonly_dir, "out.jsonl")
            proc = run_cli("window", "run", self.in_path, impossible,
                           "--declaration", self.decl_path)
        finally:
            os.chmod(readonly_dir, 0o700)
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory write permissions")
        self.assertEqual(proc.returncode, 1)
        error = json.loads(proc.stdout)
        self.assertEqual(error["error"], "window_io_error")
        self.assertFalse(os.path.exists(impossible))
        self.assertEqual(self._temp_entries(), [])

    def test_bad_cli_arguments_are_nonzero(self):
        proc = run_cli("window", "run", self.in_path, self.out_path,
                       "--declaration", self.decl_path,
                       "--batch-size", "0")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "window_error")
        proc = run_cli("window", "run")
        self.assertEqual(proc.returncode, 2)

    def test_existing_commands_still_available(self):
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("window", proc.stdout)
        proc = run_cli("staging", "--help")
        self.assertEqual(proc.returncode, 0)
        proc = run_cli("copybook", "--help")
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
