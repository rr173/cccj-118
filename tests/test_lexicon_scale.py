"""Moderate-scale test: bounded memory claims, verified structurally.

The full 105k-alias demonstration lives in ``demo/lexicon_scale_demo.py``;
this test keeps CI fast while still proving that neither the build nor
the scan mirrors the automaton in Python objects.
"""

import json
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from distagg.lexicon import (
    Automaton,
    CompileConfig,
    LexiconLibrary,
    compile_dictionary,
    scan_remarks,
    ScanConfig,
)


class ScaleTest(unittest.TestCase):
    def test_build_and_scan_stay_bounded(self):
        rng = random.Random(2026)
        work = tempfile.mkdtemp(prefix="lexicon-scale-test-")
        try:
            dict_path = os.path.join(work, "big.jsonl")
            n_aliases = 30_000
            total_chars = 0
            with open(dict_path, "w", encoding="utf-8") as fh:
                for i in range(n_aliases):
                    alias = (f"vendor{i % 2000:04d} part{rng.randint(0, 999):03d} "
                             f"variant-{rng.randint(0, 99):02d} {i:06d}")
                    total_chars += len(alias)
                    fh.write(json.dumps({
                        "term_id": f"T{i:06d}", "priority": rng.randint(1, 9),
                        "aliases": [{"text": alias, "boundary": "any"}],
                    }) + "\n")
                fh.write(json.dumps({
                    "term_id": "REAL", "priority": 1,
                    "aliases": [{"text": "pump station", "boundary": "any"}],
                }) + "\n")

            lib_path = os.path.join(work, "big.sqlite")
            stats = compile_dictionary(
                dict_path, lib_path,
                CompileConfig(transition_cache_entries=128,
                              state_cache_entries=128))
            self.assertEqual(stats.aliases, n_aliases + 1)
            self.assertGreater(stats.states, 100_000)
            # build caches never exceed their configured capacity
            for cache in stats.cache_stats.values():
                self.assertLessEqual(cache["peak_entries"], cache["capacity"])

            remarks_path = os.path.join(work, "remarks.jsonl")
            with open(remarks_path, "w", encoding="utf-8") as fh:
                for i in range(200):
                    text = "filler text without hits " * 3
                    if i == 100:
                        text = "a real pump station here"
                    fh.write(json.dumps({"id": i, "text": text}) + "\n")
            out_path = os.path.join(work, "out.jsonl")
            scan_stats = scan_remarks(
                lib_path, remarks_path, out_path, mode="resolved",
                config=ScanConfig(state_cache_entries=64,
                                  transition_cache_entries=64,
                                  output_cache_entries=32))
            self.assertEqual(scan_stats.lines_read, 200)
            self.assertEqual(scan_stats.matches, 1)
            for cache in scan_stats.automaton["caches"].values():
                self.assertLessEqual(cache["peak_entries"], cache["capacity"])
            with open(out_path, "r", encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            hits = [(row["line"], m) for row in rows for m in row["matches"]]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0][0], 101)
            self.assertEqual(hits[0][1]["term_id"], "REAL")
            self.assertEqual(hits[0][1]["text"], "pump station")
            self.assertEqual(
                (hits[0][1]["start"], hits[0][1]["end"]), (7, 19))
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
