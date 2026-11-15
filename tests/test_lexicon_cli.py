"""CLI tests: separate compile/scan processes, failure protocols."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import run_cli


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            if isinstance(line, str):
                handle.write(line + "\n")
            else:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")


DICT = [
    {"term_id": "MAT-PUMP", "priority": 10,
     "aliases": [{"text": "pump", "boundary": "ascii_word"}]},
    {"term_id": "MAT-PUMP-STATION", "priority": 10,
     "aliases": [{"text": "pump station", "boundary": "ascii_word"}]},
    {"term_id": "MAT-STATION", "priority": 10,
     "aliases": [{"text": "station", "boundary": "ascii_word"}]},
    {"term_id": "MAT-PUMP-STATION-ALT", "priority": 10,
     "aliases": [{"text": "pump station", "boundary": "ascii_word"}]},
]


class CliTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="lexicon-cli-test-")
        self.dict_path = os.path.join(self.work, "dict.jsonl")
        self.lib_path = os.path.join(self.work, "lib.sqlite")
        self.input_path = os.path.join(self.work, "remarks.jsonl")
        self.output_path = os.path.join(self.work, "out.jsonl")
        write_lines(self.dict_path, DICT)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def run_lexicon(self, *args):
        return run_cli(["lexicon", *args])

    def compile(self):
        result = self.run_lexicon("compile", self.dict_path, self.lib_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def scan(self, *extra):
        result = self.run_lexicon(
            "scan", self.lib_path, self.input_path, self.output_path, *extra)
        return result

    def read_output(self):
        with open(self.output_path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_compile_check_scan_in_separate_processes(self):
        # Compile and scan run as independent OS processes: the scan can
        # only work off the on-disk library, never off in-memory residue.
        stats = self.compile()
        self.assertEqual(stats["terms"], 4)
        self.assertEqual(stats["aliases"], 4)

        check = self.run_lexicon("check", self.lib_path)
        self.assertEqual(check.returncode, 0, check.stderr)
        report = json.loads(check.stdout)
        self.assertTrue(report["complete"])
        self.assertEqual(report["counts"]["terms"], 4)
        self.assertEqual(report["counts"]["states"],
                         report["counts"]["transitions"] + 1)
        self.assertIn("resolved", report["conventions"])

        write_lines(self.input_path, [
            {"id": "R1", "text": "pump station pump"},
            {"id": "R2", "text": ""},
            {"id": "R3", "text": "no hits here"},
        ])
        result = self.scan("--mode", "resolved")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["lines_read"], 3)
        self.assertEqual(summary["lines_written"], 3)
        self.assertEqual(summary["matches"], 2)
        rows = self.read_output()
        self.assertEqual([row["line"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[0]["record"], {"id": "R1", "text": "pump station pump"})
        self.assertEqual(
            [(x["term_id"], x["start"], x["end"], x["text"])
             for x in rows[0]["matches"]],
            [("MAT-PUMP-STATION", 0, 12, "pump station"),
             ("MAT-PUMP", 13, 17, "pump")])
        self.assertEqual(rows[1]["matches"], [])
        self.assertEqual(rows[2]["matches"], [])

    def test_scan_all_mode(self):
        self.compile()
        write_lines(self.input_path, [{"id": "R1", "text": "pump station pump"}])
        result = self.scan("--mode", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.read_output()
        self.assertEqual(
            [(x["term_id"], x["start"], x["end"]) for x in rows[0]["matches"]],
            [("MAT-PUMP", 0, 4),
             ("MAT-PUMP-STATION", 0, 12),
             ("MAT-PUMP-STATION-ALT", 0, 12),
             ("MAT-STATION", 5, 12),
             ("MAT-PUMP", 13, 17)])

    def test_bad_last_line_keeps_previous_output(self):
        self.compile()
        write_lines(self.input_path, [{"id": "R1", "text": "pump"}])
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.output_path, "rb") as handle:
            previous = handle.read()

        # Now corrupt the input: a valid first line and a broken last line.
        write_lines(self.input_path, [
            {"id": "R1", "text": "pump"},
            '{"id": "R2", "text": "pump station"',  # truncated JSON
        ])
        result = self.scan()
        self.assertNotEqual(result.returncode, 0)
        error = json.loads(result.stdout)
        self.assertEqual(error["error"], "lexicon_input_error")
        self.assertEqual(error["line_number"], 2)
        # The previous result is untouched; no new file pretends success.
        with open(self.output_path, "rb") as handle:
            self.assertEqual(handle.read(), previous)

    def test_input_errors_report_physical_line(self):
        self.compile()
        cases = [
            (b"\xff\xfe", "not valid UTF-8"),
            (b'{"id": 1, "text": "a", "text": "b"}', "duplicate JSON member"),
            (b'[1, 2]', "must be an object"),
            (b'{"id": 1}', "missing required member 'text'"),
            (b'{"id": 1, "text": 42}', "'text' must be a string"),
        ]
        for raw, fragment in cases:
            with open(self.input_path, "wb") as handle:
                handle.write(b'{"id": 1, "text": "pump"}\n')
                handle.write(raw + b"\n")
            result = self.scan()
            self.assertNotEqual(result.returncode, 0, raw)
            error = json.loads(result.stdout)
            self.assertEqual(error["line_number"], 2, raw)
            self.assertIn(fragment, error["message"], raw)
            self.assertFalse(os.path.exists(self.output_path), raw)

    def test_output_write_failure_leaves_no_target(self):
        self.compile()
        write_lines(self.input_path, [{"id": "R1", "text": "pump"}])
        readonly_dir = os.path.join(self.work, "readonly")
        os.mkdir(readonly_dir)
        os.chmod(readonly_dir, 0o500)
        self.output_path = os.path.join(readonly_dir, "out.jsonl")
        try:
            result = self.scan()
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(os.path.exists(self.output_path))
        finally:
            os.chmod(readonly_dir, 0o700)

    def test_compile_refuses_to_overwrite(self):
        self.compile()
        result = self.run_lexicon("compile", self.dict_path, self.lib_path)
        self.assertNotEqual(result.returncode, 0)
        error = json.loads(result.stdout)
        self.assertEqual(error["error"], "lexicon_library_error")
        self.assertIn("overwrite", error["message"])

    def test_compile_bad_dictionary_reports_line_and_alias(self):
        write_lines(self.dict_path, DICT + [
            {"term_id": "BAD", "priority": 1,
             "aliases": [{"text": "ok", "boundary": "any"},
                         {"text": "x", "boundary": "word"}]},
        ])
        result = self.run_lexicon("compile", self.dict_path, self.lib_path)
        self.assertNotEqual(result.returncode, 0)
        error = json.loads(result.stdout)
        self.assertEqual(error["error"], "lexicon_dictionary_error")
        self.assertEqual(error["line_number"], 5)
        self.assertEqual(error["alias_index"], 1)
        self.assertFalse(os.path.exists(self.lib_path))

    def test_scan_corrupt_library_fails(self):
        self.compile()
        with open(self.lib_path, "r+b") as handle:
            handle.seek(1500)
            handle.write(b"\x00" * 4000)
        write_lines(self.input_path, [{"id": "R1", "text": "pump"}])
        result = self.scan()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self.output_path))

    def test_scan_missing_text_limit(self):
        self.compile()
        big = "x" * 200
        write_lines(self.input_path, [{"id": "R1", "text": big}])
        result = self.scan("--max-remark-bytes", "100")
        self.assertNotEqual(result.returncode, 0)
        error = json.loads(result.stdout)
        self.assertEqual(error["line_number"], 1)
        self.assertIn("limit", error["message"])
        self.assertFalse(os.path.exists(self.output_path))

    def test_scan_stats_are_reported(self):
        self.compile()
        write_lines(self.input_path, [
            {"id": "R1", "text": "pump station pump"},
            {"id": "R2", "text": "station"},
        ])
        result = self.scan("--mode", "all",
                           "--state-cache-entries", "8",
                           "--transition-cache-entries", "8",
                           "--output-cache-entries", "4")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        automaton = summary["automaton"]
        self.assertGreater(automaton["transition_lookups"], 0)
        self.assertGreaterEqual(automaton["fail_follows"], 0)
        self.assertEqual(automaton["candidates_raw"], 6)
        for cache in automaton["caches"].values():
            self.assertLessEqual(cache["peak_entries"], cache["capacity"])
        self.assertGreater(summary["peak_rss_kib"], 0)

    def test_scan_empty_input_publishes_empty_output(self):
        self.compile()
        write_lines(self.input_path, [])
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["lines_read"], 0)
        self.assertEqual(self.read_output(), [])

    def test_candidate_limit_fails_loudly(self):
        write_lines(self.dict_path, [
            {"term_id": "A1", "priority": 1,
             "aliases": [{"text": "a", "boundary": "any"}]},
            {"term_id": "A2", "priority": 1,
             "aliases": [{"text": "aa", "boundary": "any"}]},
            {"term_id": "A3", "priority": 1,
             "aliases": [{"text": "aaa", "boundary": "any"}]},
        ])
        self.compile()
        write_lines(self.input_path, [{"id": "R1", "text": "a" * 200}])
        result = self.scan("--mode", "all", "--max-candidates-per-record", "16")
        self.assertNotEqual(result.returncode, 0)
        error = json.loads(result.stdout)
        self.assertEqual(error["error"], "lexicon_input_error")
        self.assertEqual(error["line_number"], 1)
        self.assertIn("candidates", error["message"])
        self.assertFalse(os.path.exists(self.output_path))
        # without the limit the same remark scans fine
        result = self.scan("--mode", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.read_output()
        # "a" x 200, "aa" x 199, "aaa" x 198
        self.assertEqual(len(rows[0]["matches"]), 200 + 199 + 198)


if __name__ == "__main__":
    unittest.main()
