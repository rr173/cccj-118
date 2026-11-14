"""End-to-end CLI tests for ``distagg staging import ...``.

These tests drive the real command line in child processes, so they cover
argument wiring, exit codes, the hard SIGKILL batch-boundary hook (an
actual killed process, not an in-process exception), symlink/path boundary
errors, and byte-identical regeneration of the shipped fixtures.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DATA = os.path.join(ROOT, "tests", "data", "imports")
CONFIG = os.path.join(DATA, "supplier_config.json")
MIXED = os.path.join(DATA, "corrections_mixed.tsv")
GOOD = os.path.join(DATA, "corrections_good.tsv")
GENERATOR = os.path.join(DATA, "generate_fixtures.py")
FIXTURE_TSV = [
    "bad_header_duplicate.tsv",
    "bad_header_missing.tsv",
    "bad_header_unknown.tsv",
    "bad_utf8.tsv",
    "corrections_good.tsv",
    "corrections_mixed.tsv",
    "no_trailing_newline.tsv",
]


def cli_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_cli(args, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True, env=cli_env(), **kwargs
    )


def load_stdout(proc):
    return json.loads(proc.stdout)


class ImportCliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="import-cli-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = os.path.join(self.dir, "store")
        self.state = os.path.join(self.dir, "state")

    def test_precheck_cli_json_and_exit_zero(self):
        proc = run_cli(["staging", "import", "precheck", CONFIG, MIXED, self.state,
                        "--root", DATA])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = load_stdout(proc)
        self.assertEqual(report["mode"], "precheck")
        self.assertEqual(report["counts"]["total_rows"], 17)
        self.assertEqual(report["samples"]["quarantined"][0]["status"], "quarantined")

    def test_run_cli_lands_batches_and_status_reports_boundary(self):
        proc = run_cli(["staging", "import", "run", CONFIG, MIXED, self.store, self.state,
                        "--root", DATA])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = load_stdout(proc)
        self.assertEqual(report["staging"]["confirmed_batches"], 2)

        proc = run_cli(["staging", "import", "status", self.state])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = load_stdout(proc)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["confirmed_rows"], 6)
        # The store remains independently readable through the normal CLI.
        proc = run_cli(["staging", "stats", self.store])
        self.assertEqual(load_stdout(proc)["commit"], 2)

    def test_rerun_is_idempotent_at_the_process_level(self):
        run_cli(["staging", "import", "run", CONFIG, GOOD, self.store, self.state,
                 "--root", DATA])
        first = load_stdout(run_cli(["staging", "stats", self.store]))["commit"]
        proc = run_cli(["staging", "import", "run", CONFIG, GOOD, self.store, self.state,
                        "--root", DATA])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        second = load_stdout(run_cli(["staging", "stats", self.store]))["commit"]
        self.assertEqual(first, second)
        ledger = os.path.join(self.state, "batches.jsonl")
        with open(ledger, encoding="utf-8") as handle:
            self.assertEqual(sum(1 for _ in handle), 3)

    def test_real_sigkill_at_batch_boundary_then_resume(self):
        # The good fixture has 8 importable rows at batch_rows=3 -> 3/3/2.
        proc = run_cli(["staging", "import", "run", CONFIG, GOOD, self.store, self.state,
                        "--root", DATA, "--kill-after-batch", "1"])
        self.assertEqual(proc.returncode, -9)  # terminated by SIGKILL(9)
        status = load_stdout(run_cli(["staging", "import", "status", self.state]))
        self.assertEqual(status["status"], "interrupted_batches_confirmed")
        self.assertEqual(status["confirmed_batches"], 1)
        self.assertEqual(status["last_physical_line"], 4)
        # Only the first batch's three keys are visible.
        stats = load_stdout(run_cli(["staging", "stats", self.store]))
        self.assertEqual(stats["commit"], 1)

        # Resume finishes the remaining two batches; no batch is rewritten.
        proc = run_cli(["staging", "import", "run", CONFIG, GOOD, self.store, self.state,
                        "--root", DATA])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = load_stdout(proc)
        self.assertEqual(report["staging"]["confirmed_batches"], 3)
        self.assertEqual(report["staging"]["confirmed_rows"], 8)
        self.assertEqual(load_stdout(run_cli(["staging", "stats", self.store]))["commit"], 3)

    def test_resume_matches_a_fresh_execution(self):
        # Interrupted + resumed run.
        run_cli(["staging", "import", "run", CONFIG, GOOD, self.store, self.state,
                 "--root", DATA, "--kill-after-batch", "2"])
        run_cli(["staging", "import", "run", CONFIG, GOOD, self.store, self.state,
                 "--root", DATA])
        resumed = load_stdout(run_cli(["staging", "scan", self.store]))["entries"]

        # Fresh one-shot execution.
        fresh_store = os.path.join(self.dir, "fresh-store")
        fresh_state = os.path.join(self.dir, "fresh-state")
        run_cli(["staging", "import", "run", CONFIG, GOOD, fresh_store, fresh_state,
                 "--root", DATA])
        fresh = load_stdout(run_cli(["staging", "scan", fresh_store]))["entries"]
        self.assertEqual(resumed, fresh)

    def test_changed_file_resume_is_refused_with_nonzero_exit(self):
        target = os.path.join(self.dir, "changing.tsv")
        shutil.copyfile(GOOD, target)
        run_cli(["staging", "import", "run", CONFIG, target, self.store, self.state,
                 "--root", self.dir])
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("acme\tZ999\t7a\t7b\tadded\n")
        proc = run_cli(["staging", "import", "run", CONFIG, target, self.store, self.state,
                        "--root", self.dir])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(load_stdout(proc)["error"], "import_fingerprint_mismatch")
        # A new import identity on the same content is allowed.
        proc = run_cli(["staging", "import", "run", CONFIG, target,
                        os.path.join(self.dir, "s2"), os.path.join(self.dir, "st2"),
                        "--root", self.dir, "--import-id", "imp-fresh-0002"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_symlink_and_path_escape_exit_nonzero(self):
        link = os.path.join(self.dir, "link.tsv")
        os.symlink(GOOD, link)
        proc = run_cli(["staging", "import", "precheck", CONFIG, link,
                        os.path.join(self.dir, "st"), "--root", self.dir])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(load_stdout(proc)["error"], "import_path_error")
        # Explicit opt-in follows the link.
        proc = run_cli(["staging", "import", "precheck", CONFIG, link,
                        os.path.join(self.dir, "st2"), "--root", self.dir,
                        "--allow-symlinks"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

        outside = os.path.join(self.dir, "outside.tsv")
        with open(outside, "wb") as handle:
            handle.write(b"x")
        proc = run_cli(["staging", "import", "precheck", CONFIG, outside,
                        os.path.join(self.dir, "st3"),
                        "--root", os.path.join(self.dir, "nested")])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(load_stdout(proc)["error"], "import_path_error")

    def test_header_defects_and_bad_encoding_are_reported(self):
        for name, expected in (
            ("bad_header_missing.tsv", "import_header_error"),
            ("bad_header_duplicate.tsv", "import_header_error"),
            ("bad_header_unknown.tsv", "import_header_error"),
        ):
            proc = run_cli(["staging", "import", "precheck", CONFIG,
                            os.path.join(DATA, name), os.path.join(self.dir, "s-" + name),
                            "--root", DATA])
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(load_stdout(proc)["error"], expected, name)
        proc = run_cli(["staging", "import", "precheck", CONFIG,
                        os.path.join(DATA, "bad_utf8.tsv"),
                        os.path.join(self.dir, "s-utf8"), "--root", DATA])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(load_stdout(proc)["quarantine_reasons"], {"invalid_utf8": 1})

    def test_report_write_failure_exit_code_and_recoverable_state(self):
        proc = run_cli(["staging", "import", "run", CONFIG, GOOD, self.store, self.state,
                        "--root", DATA, "--fail-report-write"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(load_stdout(proc)["error"], "import_report_write_failed")
        status = load_stdout(run_cli(["staging", "import", "status", self.state]))
        self.assertEqual(status["status"], "data_committed_report_missing")
        self.assertFalse(status["reports"]["run.json"]["exists"])
        # The committed batches are still queryable independently.
        self.assertEqual(load_stdout(run_cli(["staging", "stats", self.store]))["commit"], 3)
        # Re-running completes the report without extra commits.
        run_cli(["staging", "import", "run", CONFIG, GOOD, self.store, self.state,
                 "--root", DATA])
        self.assertEqual(
            load_stdout(run_cli(["staging", "import", "status", self.state]))["status"],
            "complete",
        )

    def test_missing_config_is_a_config_error(self):
        proc = run_cli(["staging", "import", "precheck",
                        os.path.join(self.dir, "nope.json"), MIXED, self.state,
                        "--root", DATA])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(load_stdout(proc)["error"], "import_config_error")

    def test_reports_never_contain_input_file_copy_or_credentials(self):
        proc = run_cli(["staging", "import", "run", CONFIG, MIXED, self.store, self.state,
                        "--root", DATA])
        report = load_stdout(proc)
        report_bytes = json.dumps(report).encode("utf-8")
        # The report carries the relative path, never the absolute input.
        self.assertEqual(report["input"]["relative_path"], "corrections_mixed.tsv")
        self.assertNotIn(os.path.abspath(MIXED).encode(), report_bytes)
        # The whole input file is not embedded anywhere in the report.
        with open(MIXED, "rb") as handle:
            full_input = handle.read()
        self.assertNotIn(base64.b64encode(full_input), report_bytes)
        self.assertNotIn(full_input, report_bytes)
        # Raw lines appear only inside the bounded sample buckets, each capped.
        sample_count = sum(len(bucket) for bucket in report["samples"].values())
        self.assertLessEqual(sample_count, report["sample_limit"] * 4)
        for row in report["rows"]:  # the full row list never embeds raw bytes
            self.assertNotIn("raw_line_b64", row)


class FixtureReproducibilityTest(unittest.TestCase):
    def test_generator_is_byte_deterministic(self):
        def digests():
            result = {}
            for name in FIXTURE_TSV:
                with open(os.path.join(DATA, name), "rb") as handle:
                    result[name] = hashlib.sha256(handle.read()).hexdigest()
            return result

        before = digests()
        proc = subprocess.run(
            [sys.executable, "-I", GENERATOR], capture_output=True, text=True,
            cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(before, digests())


if __name__ == "__main__":
    unittest.main()
