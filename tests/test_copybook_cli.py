"""CLI tests for ``distagg copybook check`` and ``distagg copybook decode``."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DEMO = os.path.join(ROOT, "demo", "copybook")
COPYBOOK = os.path.join(DEMO, "order_record.cpy")
SELECTORS = os.path.join(DEMO, "selectors.json")
MAKE_DATA = os.path.join(DEMO, "make_order_feed.py")


def run_cli(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True, env=env)


class CopybookCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="copybook-cli-test-")
        cls.data = os.path.join(cls.work, "orders.dat")
        cls.expected = os.path.join(cls.work, "expected.json")
        subprocess.run([sys.executable, MAKE_DATA, cls.work], check=True,
                       stdout=subprocess.DEVNULL)

    def test_check_prints_layout_report(self):
        proc = run_cli("copybook", "check", COPYBOOK, "--select", SELECTORS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["record"], "ORDER-RECORD")
        self.assertEqual(report["span"],
                         {"kind": "variable", "min_bytes": 58, "max_bytes": 173})
        fields = {f["path"]: f for f in report["fields"]}
        # Static offsets where the layout is constant ...
        self.assertEqual(fields["ORDER-RECORD.ORDER-KIND"]["offset"], 33)
        self.assertEqual(fields["ORDER-RECORD.DETAIL-COUNT"]["offset"], 44)
        self.assertEqual(fields["ORDER-RECORD.DETAILS"]["offset"], 46)
        # ... and honest dynamic markers behind the variable array.
        trailer = fields["ORDER-RECORD.TRAILER.ORDER-TOTAL"]
        self.assertIsNone(trailer["offset"])
        self.assertEqual(trailer["offset_kind"], "dynamic")
        # OCCURS and REDEFINES relations are reported.
        self.assertEqual(fields["ORDER-RECORD.DETAILS"]["occurs"],
                         {"kind": "depending", "min": 0, "max": 5,
                          "on": "ORDER-RECORD.DETAIL-COUNT"})
        target = fields["ORDER-RECORD.ORDER-BODY"]["redefines"]
        self.assertEqual(target["role"], "target")
        self.assertEqual(target["group_span"], 10)
        self.assertEqual(target["selected_by"]["using"],
                         "ORDER-RECORD.ORDER-KIND")
        branch = fields["ORDER-RECORD.EXTENDED-BODY"]["redefines"]
        self.assertEqual(branch, {"role": "branch",
                                  "target": "ORDER-RECORD.ORDER-BODY"})

    def test_check_compile_error_is_json_with_line(self):
        bad = os.path.join(self.work, "bad.cpy")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("01 REC.\n  05 A PIC X(1) VALUE 'X'.\n")
        proc = run_cli("copybook", "check", bad)
        self.assertEqual(proc.returncode, 1)
        error = json.loads(proc.stdout)
        self.assertEqual(error["error"], "copybook_unsupported")
        self.assertEqual(error["line"], 2)
        self.assertEqual(error["field"], "A")

    def test_check_missing_selector_config(self):
        proc = run_cli("copybook", "check", COPYBOOK)
        self.assertEqual(proc.returncode, 1)
        error = json.loads(proc.stdout)
        self.assertEqual(error["error"], "selector_config")
        self.assertIn("ORDER-RECORD.ORDER-BODY", error["message"])

    def test_decode_streams_json_lines(self):
        proc = run_cli("copybook", "decode", COPYBOOK, self.data,
                       "--select", SELECTORS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(lines), 3)
        with open(self.expected, "r", encoding="utf-8") as handle:
            expected = json.load(handle)
        for line, want in zip(lines, expected["records"]):
            self.assertEqual(line["data"], want)
        self.assertEqual([line["record"] for line in lines], [1, 2, 3])
        self.assertEqual([line["offset"] for line in lines], [0, 62, 239])

    def test_decode_limit(self):
        proc = run_cli("copybook", "decode", COPYBOOK, self.data,
                       "--select", SELECTORS, "--limit", "2")
        self.assertEqual(proc.returncode, 0)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_decode_bad_record_exit_and_streams(self):
        with open(self.data, "rb") as handle:
            blob = bytearray(handle.read())
        blob[66 + 56] = 0xAB          # DETAILS[1].UNIT-PRICE of record 2
        bad = os.path.join(self.work, "bad.dat")
        with open(bad, "wb") as handle:
            handle.write(bytes(blob))
        proc = run_cli("copybook", "decode", COPYBOOK, bad,
                       "--select", SELECTORS)
        self.assertEqual(proc.returncode, 1)
        # The good record before the failure stays on stdout ...
        stdout_lines = [line for line in proc.stdout.splitlines() if line]
        self.assertEqual(len(stdout_lines), 1)
        self.assertEqual(json.loads(stdout_lines[0])["record"], 1)
        # ... and the error object on stderr names the exact position.
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"], "decode_error")
        self.assertEqual(error["record_no"], 2)
        self.assertEqual(error["offset"], 62)
        self.assertEqual(error["field_path"],
                         "ORDER-RECORD.DETAILS[1].UNIT-PRICE")
        self.assertIn("invalid packed digit", error["reason"])

    def test_existing_commands_still_available(self):
        proc = run_cli("--version")
        self.assertEqual(proc.returncode, 0)
        proc = run_cli("copybook", "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("check", proc.stdout)
        self.assertIn("decode", proc.stdout)


if __name__ == "__main__":
    unittest.main()
