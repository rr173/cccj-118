"""Hand-computed matching cases: folding, boundaries, selection rules."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from distagg.lexicon import (
    Automaton,
    LexiconLibrary,
    compile_dictionary,
    scan_text,
)


def make_library(work, records):
    dict_path = os.path.join(work, "dict.jsonl")
    with open(dict_path, "w", encoding="utf-8", newline="\n") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    lib_path = os.path.join(work, "lib.sqlite")
    compile_dictionary(dict_path, lib_path)
    return lib_path


def term(term_id, priority, *aliases):
    return {
        "term_id": term_id,
        "priority": priority,
        "aliases": [{"text": text, "boundary": boundary}
                    for text, boundary in aliases],
    }


class LexiconCase(unittest.TestCase):
    RECORDS = []

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="lexicon-match-test-")
        cls.lib_path = make_library(cls.work, cls.RECORDS)

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.work, ignore_errors=True)

    def matches(self, text, mode):
        with LexiconLibrary(self.lib_path) as library:
            automaton = Automaton(library)
            return scan_text(automaton, text, mode)

    def assertMatches(self, text, mode, expected):
        got = self.matches(text, mode)
        self.assertEqual(
            got, expected,
            f"\nmode={mode} text={text!r}\ngot:  {got}\nwant: {expected}")


def m(term_id, alias_index, start, end, text):
    return {"term_id": term_id, "alias_index": alias_index,
            "start": start, "end": end, "text": text}


class PumpDemoTest(LexiconCase):
    """The materials demo: pump / pump station / station / same-name term."""

    RECORDS = [
        term("MAT-PUMP", 10, ("pump", "ascii_word")),
        term("MAT-PUMP-STATION", 10, ("pump station", "ascii_word")),
        term("MAT-STATION", 10, ("station", "ascii_word")),
        term("MAT-PUMP-STATION-ALT", 10, ("pump station", "ascii_word")),
    ]

    def test_all_hand_computed(self):
        self.assertMatches("pump station pump", "all", [
            m("MAT-PUMP", 0, 0, 4, "pump"),
            m("MAT-PUMP-STATION", 0, 0, 12, "pump station"),
            m("MAT-PUMP-STATION-ALT", 0, 0, 12, "pump station"),
            m("MAT-STATION", 0, 5, 12, "station"),
            m("MAT-PUMP", 0, 13, 17, "pump"),
        ])

    def test_resolved_equal_priority_falls_to_term_id(self):
        # Same start, same span, same priority: lexicographically smaller
        # term_id wins; the trailing pump is adjacent-eligible.
        self.assertMatches("pump station pump", "resolved", [
            m("MAT-PUMP-STATION", 0, 0, 12, "pump station"),
            m("MAT-PUMP", 0, 13, 17, "pump"),
        ])


class PriorityTieBreakTest(unittest.TestCase):
    """Same-length same-position candidates: priority, then term_id."""

    def run_with_priorities(self, prio_a, prio_b):
        work = tempfile.mkdtemp(prefix="lexicon-prio-test-")
        try:
            lib_path = make_library(work, [
                term("TERM-A", prio_a, ("pump station", "ascii_word")),
                term("TERM-B", prio_b, ("pump station", "ascii_word")),
            ])
            with LexiconLibrary(lib_path) as library:
                automaton = Automaton(library)
                return scan_text(automaton, "pump station", "resolved")
        finally:
            import shutil

            shutil.rmtree(work, ignore_errors=True)

    def test_lower_priority_number_wins(self):
        got = self.run_with_priorities(20, 5)
        self.assertEqual([x["term_id"] for x in got], ["TERM-B"])

    def test_equal_priority_falls_to_term_id(self):
        got = self.run_with_priorities(7, 7)
        self.assertEqual([x["term_id"] for x in got], ["TERM-A"])

    def test_all_keeps_both_regardless_of_priority(self):
        work = tempfile.mkdtemp(prefix="lexicon-prio-test-")
        try:
            lib_path = make_library(work, [
                term("TERM-A", 20, ("pump station", "ascii_word")),
                term("TERM-B", 5, ("pump station", "ascii_word")),
            ])
            with LexiconLibrary(lib_path) as library:
                automaton = Automaton(library)
                got = scan_text(automaton, "pump station", "all")
            self.assertEqual([x["term_id"] for x in got],
                             ["TERM-A", "TERM-B"])
        finally:
            import shutil

            shutil.rmtree(work, ignore_errors=True)


class FoldingEdgeTest(LexiconCase):
    RECORDS = [
        term("STRASSE", 1, ("strasse", "any"), ("STRASSE", "any")),
        term("S", 1, ("s", "any")),
        term("SS", 1, ("ss", "any")),
        term("PUMP-STATION", 1, ("pump station", "any")),
        term("GAP", 1, (" ", "any")),
    ]

    def test_strasse_matches_folded_slice_from_original(self):
        # The slice is the original 6 characters, not the 7 folded ones.
        self.assertMatches("Straße", "all", [
            m("S", 0, 0, 1, "S"),
            m("STRASSE", 0, 0, 6, "Straße"),
            m("SS", 0, 4, 5, "ß"),
        ])

    def test_single_s_cannot_split_sharp_s(self):
        # Only the leading 'S' is a cut-point hit; the two folded 's'
        # characters of ß are not separately reportable.
        got = self.matches("Straße", "all")
        s_hits = [x for x in got if x["term_id"] == "S"]
        self.assertEqual(s_hits, [m("S", 0, 0, 1, "S")])

    def test_ss_matches_whole_sharp_s(self):
        got = self.matches("Straße", "all")
        ss_hits = [x for x in got if x["term_id"] == "SS"]
        self.assertEqual(ss_hits, [m("SS", 0, 4, 5, "ß")])

    def test_uppercase_body(self):
        self.assertMatches("STRASSE", "all", [
            m("S", 0, 0, 1, "S"),
            m("STRASSE", 0, 0, 7, "STRASSE"),
            m("S", 0, 4, 5, "S"),
            m("SS", 0, 4, 6, "SS"),
            m("S", 0, 5, 6, "S"),
        ])

    def test_same_term_same_interval_keeps_smallest_alias_index(self):
        # STRASSE has two aliases folding to "strasse"; only index 0 stays.
        got = self.matches("strasse", "all")
        strasse_hits = [x for x in got if x["term_id"] == "STRASSE"]
        self.assertEqual(strasse_hits, [m("STRASSE", 0, 0, 7, "strasse")])

    def test_alias_space_matches_whole_whitespace_run(self):
        text = "pump \t\n station"
        got = self.matches(text, "all")
        ps = [x for x in got if x["term_id"] == "PUMP-STATION"]
        self.assertEqual(ps, [m("PUMP-STATION", 0, 0, 15, text)])
        gap = [x for x in got if x["term_id"] == "GAP"]
        self.assertEqual(gap, [m("GAP", 0, 4, 8, " \t\n ")])

    def test_whitespace_only_remark(self):
        self.assertMatches("  \t ", "all", [m("GAP", 0, 0, 4, "  \t ")])
        self.assertMatches("  \t ", "resolved", [m("GAP", 0, 0, 4, "  \t ")])


class BoundaryTest(LexiconCase):
    RECORDS = [
        term("PUMP", 1, ("pump", "ascii_word")),
        term("PUMP-ANY", 1, ("pump", "any")),
    ]

    def pump_hits(self, text):
        return [x for x in self.matches(text, "all")
                if x["term_id"] == "PUMP"]

    def test_chinese_neighbours_are_not_ascii_word_chars(self):
        self.assertEqual(self.pump_hits("中文pump中文"),
                         [m("PUMP", 0, 2, 6, "pump")])

    def test_ascii_letter_blocks(self):
        self.assertEqual(self.pump_hits("xpump"), [])
        self.assertEqual(self.pump_hits("pumpx"), [])

    def test_digit_and_underscore_block(self):
        self.assertEqual(self.pump_hits("pump2"), [])
        self.assertEqual(self.pump_hits("2pump"), [])
        self.assertEqual(self.pump_hits("pump_1"), [])
        self.assertEqual(self.pump_hits("_pump"), [])

    def test_punctuation_and_edges_pass(self):
        self.assertEqual(self.pump_hits("pump-"), [m("PUMP", 0, 0, 4, "pump")])
        self.assertEqual(self.pump_hits("(pump)"), [m("PUMP", 0, 1, 5, "pump")])
        self.assertEqual(self.pump_hits("pump"), [m("PUMP", 0, 0, 4, "pump")])

    def test_any_boundary_matches_inside_words(self):
        got = [x for x in self.matches("xpumpx", "all")
               if x["term_id"] == "PUMP-ANY"]
        self.assertEqual(got, [m("PUMP-ANY", 0, 1, 5, "pump")])


class AdjacencyAndEmptyTest(LexiconCase):
    RECORDS = [
        term("AB", 1, ("ab", "any")),
        term("CD", 1, ("cd", "any")),
        term("ABCD", 1, ("abcd", "any")),
    ]

    def test_adjacent_hits_both_survive_resolved(self):
        self.assertMatches("ab cd", "resolved", [
            m("AB", 0, 0, 2, "ab"),
            m("CD", 0, 3, 5, "cd"),
        ])

    def test_back_to_back_adjacent(self):
        self.assertMatches("abcd", "resolved", [
            m("ABCD", 0, 0, 4, "abcd"),
        ])

    def test_empty_remark(self):
        self.assertMatches("", "all", [])
        self.assertMatches("", "resolved", [])

    def test_all_whitespace_remark_no_alias(self):
        self.assertMatches("   ", "all", [])
        self.assertMatches("   ", "resolved", [])

    def test_no_match_remark(self):
        self.assertMatches("nothing here", "all", [])


class ResolvedWalkTest(LexiconCase):
    """The greedy walk: leftmost start, longest span, then skip overlaps."""

    RECORDS = [
        term("LONG", 1, ("abcdef", "any")),
        term("MID", 1, ("bcde", "any")),
        term("SHORT", 1, ("ab", "any")),
        term("TAIL", 1, ("f", "any")),
    ]

    def test_resolved_picks_longest_at_leftmost_start(self):
        # LONG swallows MID and SHORT; TAIL starts inside LONG's span.
        self.assertMatches("abcdef", "resolved", [
            m("LONG", 0, 0, 6, "abcdef"),
        ])

    def test_all_keeps_everything(self):
        self.assertMatches("abcdef", "all", [
            m("SHORT", 0, 0, 2, "ab"),
            m("LONG", 0, 0, 6, "abcdef"),
            m("MID", 0, 1, 5, "bcde"),
            m("TAIL", 0, 5, 6, "f"),
        ])

    def test_scan_continues_after_chosen_end(self):
        self.assertMatches("abcdef abcdef", "resolved", [
            m("LONG", 0, 0, 6, "abcdef"),
            m("LONG", 0, 7, 13, "abcdef"),
        ])


if __name__ == "__main__":
    unittest.main()
