"""Unit tests for the comparison-string folding and its provenance map."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from distagg.lexicon.folding import (
    ascii_word_boundary_ok,
    fold_with_mapping,
    is_cut_point,
    original_interval,
)


class FoldTest(unittest.TestCase):
    def test_plain_ascii(self):
        comparison, mapping = fold_with_mapping("AbC")
        self.assertEqual(comparison, "abc")
        self.assertEqual(mapping, [(0, 1), (1, 2), (2, 3)])

    def test_empty_string(self):
        self.assertEqual(fold_with_mapping(""), ("", []))

    def test_casefold_expansion_keeps_provenance(self):
        # ß folds to two comparison characters, both owned by original char 4.
        comparison, mapping = fold_with_mapping("Straße")
        self.assertEqual(comparison, "strasse")
        self.assertEqual(mapping[4], (4, 5))
        self.assertEqual(mapping[5], (4, 5))
        self.assertEqual(mapping[6], (5, 6))

    def test_whitespace_run_folds_to_one_space(self):
        comparison, mapping = fold_with_mapping("a \t\n b")
        self.assertEqual(comparison, "a b")
        self.assertEqual(mapping, [(0, 1), (1, 5), (5, 6)])

    def test_all_whitespace_string(self):
        comparison, mapping = fold_with_mapping("  \t ")
        self.assertEqual(comparison, " ")
        self.assertEqual(mapping, [(0, 4)])

    def test_no_trimming(self):
        comparison, _ = fold_with_mapping(" a ")
        self.assertEqual(comparison, " a ")

    def test_no_punctuation_stripping(self):
        comparison, _ = fold_with_mapping("a,b!")
        self.assertEqual(comparison, "a,b!")

    def test_fullwidth_space_is_whitespace(self):
        comparison, mapping = fold_with_mapping("a　b")
        self.assertEqual(comparison, "a b")
        self.assertEqual(mapping[1], (1, 2))

    def test_mapping_is_monotone(self):
        _comparison, mapping = fold_with_mapping("Aß \t Cİz")
        starts = [m[0] for m in mapping]
        ends = [m[1] for m in mapping]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(ends, sorted(ends))


class CutPointTest(unittest.TestCase):
    def test_inside_expansion_is_not_a_cut(self):
        _comparison, mapping = fold_with_mapping("Straße")
        # position 5 sits between the two 's' characters folded out of ß.
        self.assertFalse(is_cut_point(mapping, 7, 5))
        self.assertTrue(is_cut_point(mapping, 7, 4))
        self.assertTrue(is_cut_point(mapping, 7, 6))

    def test_edges_are_cut_points(self):
        _comparison, mapping = fold_with_mapping("ab")
        self.assertTrue(is_cut_point(mapping, 2, 0))
        self.assertTrue(is_cut_point(mapping, 2, 2))

    def test_whitespace_run_edges_are_cut_points(self):
        _comparison, mapping = fold_with_mapping("a \t\n b")
        self.assertTrue(is_cut_point(mapping, 3, 1))
        self.assertTrue(is_cut_point(mapping, 3, 2))

    def test_original_interval(self):
        _comparison, mapping = fold_with_mapping("Straße")
        self.assertEqual(original_interval(mapping, 4, 6), (4, 5))  # ß
        self.assertEqual(original_interval(mapping, 0, 7), (0, 6))


class AsciiWordBoundaryTest(unittest.TestCase):
    def test_ascii_letters_digits_underscore_block(self):
        for ch in "aZ09_":
            self.assertFalse(ascii_word_boundary_ok(ch + "pump", 1, 5))
            self.assertFalse(ascii_word_boundary_ok("pump" + ch, 0, 4))

    def test_chinese_and_punctuation_do_not_block(self):
        self.assertTrue(ascii_word_boundary_ok("中文pump中文", 2, 6))
        self.assertTrue(ascii_word_boundary_ok("(pump)", 1, 5))
        self.assertTrue(ascii_word_boundary_ok("pump-", 0, 4))

    def test_string_edges_pass(self):
        self.assertTrue(ascii_word_boundary_ok("pump", 0, 4))


if __name__ == "__main__":
    unittest.main()
