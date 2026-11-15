"""Library integrity, check-report, output-link and shuffle tests."""

import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from distagg.lexicon import (
    Automaton,
    LexiconLibrary,
    LexiconLibraryError,
    compile_dictionary,
    library_report,
    scan_text,
)


def write_dictionary(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")


def term(term_id, priority, *aliases):
    return {
        "term_id": term_id,
        "priority": priority,
        "aliases": [{"text": text, "boundary": boundary}
                    for text, boundary in aliases],
    }


class LibraryIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="lexicon-lib-test-")
        self.dict_path = os.path.join(self.work, "dict.jsonl")
        self.lib_path = os.path.join(self.work, "lib.sqlite")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def build(self, records=None):
        if records is None:
            records = [term("A", 1, ("pump", "any"))]
        write_dictionary(self.dict_path, records)
        return compile_dictionary(self.dict_path, self.lib_path)

    def test_missing_library(self):
        with self.assertRaises(LexiconLibraryError):
            LexiconLibrary(os.path.join(self.work, "nope.sqlite"))

    def test_garbage_file_is_rejected(self):
        with open(self.lib_path, "wb") as handle:
            handle.write(b"this is not a sqlite database at all" * 4)
        with self.assertRaises(LexiconLibraryError):
            LexiconLibrary(self.lib_path)

    def test_foreign_sqlite_file_is_rejected(self):
        db = sqlite3.connect(self.lib_path)
        db.execute("CREATE TABLE something(x)")
        db.commit()
        db.close()
        with self.assertRaises(LexiconLibraryError) as ctx:
            LexiconLibrary(self.lib_path)
        self.assertIn("not a distagg lexicon library", str(ctx.exception))

    def test_incomplete_marker_is_rejected(self):
        self.build()
        # Flip the completeness marker back to 0, as an interrupted
        # build would leave it.
        db = sqlite3.connect(self.lib_path)
        db.execute("UPDATE meta SET value = '0' WHERE key = 'complete'")
        db.commit()
        db.close()
        with self.assertRaises(LexiconLibraryError) as ctx:
            LexiconLibrary(self.lib_path)
        self.assertIn("incomplete", str(ctx.exception))

    def test_truncated_library_is_rejected(self):
        self.build()
        with open(self.lib_path, "r+b") as handle:
            handle.truncate(2048)
        with self.assertRaises(LexiconLibraryError):
            LexiconLibrary(self.lib_path)

    def test_library_is_read_only_during_scan(self):
        self.build()
        with LexiconLibrary(self.lib_path) as library:
            with self.assertRaises(sqlite3.OperationalError):
                library._db.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")

    def test_check_report(self):
        self.build([
            term("MAT-PUMP", 10, ("pump", "ascii_word")),
            term("MAT-PUMP-STATION", 10, ("pump station", "ascii_word"),
                 ("pumpstation", "any")),
        ])
        report = library_report(self.lib_path)
        self.assertTrue(report["complete"])
        self.assertEqual(report["counts"]["terms"], 2)
        self.assertEqual(report["counts"]["aliases"], 3)
        self.assertEqual(report["counts"]["states"],
                         report["counts"]["transitions"] + 1)
        self.assertEqual(report["terms_total"], 2)
        self.assertEqual(report["terms_returned"], 2)
        terms = {t["term_id"]: t for t in report["terms"]}
        self.assertEqual(
            [a["text"] for a in terms["MAT-PUMP-STATION"]["aliases"]],
            ["pump station", "pumpstation"])
        self.assertEqual(
            terms["MAT-PUMP-STATION"]["aliases"][0]["folded"], "pump station")
        self.assertIn("resolved", report["conventions"])
        self.assertIn("all", report["conventions"])
        # limit caps the listing, never the counts
        limited = library_report(self.lib_path, limit=1)
        self.assertEqual(limited["terms_returned"], 1)
        self.assertEqual(limited["terms_total"], 2)


class OutputLinkTest(unittest.TestCase):
    """Shared suffixes are found through output links, not copied lists."""

    def test_suffix_chain_finds_every_ending_alias(self):
        work = tempfile.mkdtemp(prefix="lexicon-outlink-test-")
        try:
            records = [
                term("A", 1, ("a", "any")),
                term("BA", 1, ("ba", "any")),
                term("CBA", 1, ("cba", "any")),
                term("DCBA", 1, ("dcba", "any")),
            ]
            dict_path = os.path.join(work, "dict.jsonl")
            lib_path = os.path.join(work, "lib.sqlite")
            write_dictionary(dict_path, records)
            compile_dictionary(dict_path, lib_path)
            with LexiconLibrary(lib_path) as library:
                # No state stores more than its own single output row:
                # the inherited matches arrive via out_link at scan time.
                db = sqlite3.connect(f"file:{lib_path}?mode=ro", uri=True)
                try:
                    rows = db.execute(
                        "SELECT state_id, COUNT(*) FROM outputs "
                        "GROUP BY state_id").fetchall()
                    self.assertTrue(all(count == 1 for _sid, count in rows))
                finally:
                    db.close()
                automaton = Automaton(library)
                got = scan_text(automaton, "dcba", "all")
            self.assertEqual(
                [(x["term_id"], x["start"], x["end"]) for x in got],
                [("DCBA", 0, 4), ("CBA", 1, 4), ("BA", 2, 4), ("A", 3, 4)])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_long_shared_suffix_at_scale(self):
        work = tempfile.mkdtemp(prefix="lexicon-suffix-test-")
        try:
            records = [term("BASE", 1, ("pump station", "any"))]
            records += [
                term(f"V{i:04d}", 1, (f"vendor{i} pump station", "any"))
                for i in range(200)
            ]
            dict_path = os.path.join(work, "dict.jsonl")
            lib_path = os.path.join(work, "lib.sqlite")
            write_dictionary(dict_path, records)
            compile_dictionary(dict_path, lib_path)
            with LexiconLibrary(lib_path) as library:
                automaton = Automaton(library)
                got = scan_text(automaton, "vendor42 pump station", "all")
            ids = sorted(x["term_id"] for x in got)
            self.assertEqual(ids, ["BASE", "V0042"])
            base = [x for x in got if x["term_id"] == "BASE"][0]
            self.assertEqual(base["text"], "pump station")
            self.assertEqual((base["start"], base["end"]), (9, 21))
        finally:
            shutil.rmtree(work, ignore_errors=True)


class ShuffleTest(unittest.TestCase):
    """Dictionary line order must not change business decisions."""

    def test_shuffled_lines_same_results(self):
        rng = random.Random(99)
        records = [
            term("MAT-PUMP", 10, ("pump", "ascii_word")),
            term("MAT-PUMP-STATION", 5, ("pump station", "ascii_word"),
                 ("pumpstation", "any")),
            term("MAT-STATION", 7, ("station", "ascii_word")),
            term("MAT-PUMP-STATION-ALT", 5, ("pump station", "ascii_word")),
            term("MAT-SEAL", 1, ("seal", "any"), ("seal kit", "any")),
        ]
        texts = [
            "pump station pump",
            "no pumpstation here",
            "seal kit and seal",
            "station to station",
            "",
        ]
        work = tempfile.mkdtemp(prefix="lexicon-shuffle-test-")
        try:
            outputs = {}
            for label in ("original", "shuffled"):
                order = list(records)
                if label == "shuffled":
                    rng.shuffle(order)
                dict_path = os.path.join(work, f"dict-{label}.jsonl")
                lib_path = os.path.join(work, f"lib-{label}.sqlite")
                write_dictionary(dict_path, order)
                compile_dictionary(dict_path, lib_path)
                with LexiconLibrary(lib_path) as library:
                    automaton = Automaton(library)
                    outputs[label] = [
                        (scan_text(automaton, text, "all"),
                         scan_text(automaton, text, "resolved"))
                        for text in texts
                    ]
            self.assertEqual(outputs["original"], outputs["shuffled"])
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
