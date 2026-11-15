"""Randomized differential tests: automaton vs. independent reference."""

import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from distagg.lexicon import Automaton, LexiconLibrary, compile_dictionary, scan_text
from reference_lexicon import reference_matches

# A small alphabet chosen to stress folding (ß, whitespace runs), ASCII
# word boundaries (letters, digits, underscore, Chinese characters) and
# overlap (short tokens over a tiny alphabet).
ALPHABET = ["a", "b", "A", "B", "s", "S", "ß", " ", "\t", "_", "1", "中",
            "p", "u", "m", "-"]


def random_text(rng, max_len=24):
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, max_len)))


def random_dictionary(rng, n_terms):
    terms = []
    used = set()
    for index in range(n_terms):
        term_id = f"T{index:03d}"
        assert term_id not in used
        used.add(term_id)
        aliases = []
        for _ in range(rng.randint(1, 3)):
            text = random_text(rng, max_len=8)
            if text == "":
                text = "a"  # empty alias text is invalid; everything else goes
            boundary = "ascii_word" if rng.random() < 0.4 else "any"
            aliases.append((text, boundary))
        terms.append((term_id, rng.randint(-3, 3), aliases))
    return terms


class DifferentialTest(unittest.TestCase):
    def test_random_dictionaries_and_texts(self):
        rng = random.Random(20261115)
        cases = 0
        for trial in range(30):
            terms = random_dictionary(rng, rng.randint(1, 8))
            work = tempfile.mkdtemp(prefix="lexicon-diff-test-")
            try:
                dict_path = os.path.join(work, "dict.jsonl")
                with open(dict_path, "w", encoding="utf-8",
                          newline="\n") as handle:
                    for term_id, priority, aliases in terms:
                        handle.write(json.dumps({
                            "term_id": term_id,
                            "priority": priority,
                            "aliases": [
                                {"text": text, "boundary": boundary}
                                for text, boundary in aliases
                            ],
                        }, ensure_ascii=False) + "\n")
                lib_path = os.path.join(work, "lib.sqlite")
                compile_dictionary(dict_path, lib_path)
                with LexiconLibrary(lib_path) as library:
                    automaton = Automaton(
                        library,
                        state_cache_entries=4,
                        transition_cache_entries=4,
                        output_cache_entries=2,
                    )
                    for _ in range(12):
                        text = random_text(rng)
                        for mode in ("all", "resolved"):
                            got = scan_text(automaton, text, mode)
                            want = reference_matches(terms, text, mode)
                            self.assertEqual(
                                got, want,
                                f"\ntrial={trial} mode={mode} "
                                f"text={text!r}\nterms={terms!r}\n"
                                f"got:  {got}\nwant: {want}")
                            cases += 1
            finally:
                import shutil

                shutil.rmtree(work, ignore_errors=True)
        self.assertGreater(cases, 500)

    def test_deterministic_regardless_of_cache_sizes(self):
        # The same library scanned with tiny and generous caches must
        # produce identical results (caches change cost, never meaning).
        rng = random.Random(7)
        terms = random_dictionary(rng, 6)
        work = tempfile.mkdtemp(prefix="lexicon-cache-test-")
        try:
            dict_path = os.path.join(work, "dict.jsonl")
            with open(dict_path, "w", encoding="utf-8", newline="\n") as handle:
                for term_id, priority, aliases in terms:
                    handle.write(json.dumps({
                        "term_id": term_id,
                        "priority": priority,
                        "aliases": [
                            {"text": text, "boundary": boundary}
                            for text, boundary in aliases
                        ],
                    }, ensure_ascii=False) + "\n")
            lib_path = os.path.join(work, "lib.sqlite")
            compile_dictionary(dict_path, lib_path)
            texts = [random_text(rng) for _ in range(30)]
            results = {}
            for capacity in (1, 2, 64):
                with LexiconLibrary(lib_path) as library:
                    automaton = Automaton(
                        library,
                        state_cache_entries=capacity,
                        transition_cache_entries=capacity,
                        output_cache_entries=capacity,
                    )
                    results[capacity] = [
                        (scan_text(automaton, text, "all"),
                         scan_text(automaton, text, "resolved"))
                        for text in texts
                    ]
            self.assertEqual(results[1], results[2])
            self.assertEqual(results[1], results[64])
        finally:
            import shutil

            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
