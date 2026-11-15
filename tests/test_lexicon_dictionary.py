"""Dictionary validation and compile-time failure tests."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from distagg.lexicon import (
    CompileConfig,
    LexiconDictionaryError,
    LexiconLibrary,
    LexiconLibraryError,
    compile_dictionary,
)


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            if isinstance(line, str):
                handle.write(line + "\n")
            else:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def record(term_id, priority=1, aliases=None):
    if aliases is None:
        aliases = [{"text": "x", "boundary": "any"}]
    return {"term_id": term_id, "priority": priority, "aliases": aliases}


class CompileFailureTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.work = tempfile.mkdtemp(prefix="lexicon-dict-test-")
        self.dict_path = os.path.join(self.work, "dict.jsonl")
        self.lib_path = os.path.join(self.work, "lib.sqlite")

    def compile(self):
        return compile_dictionary(self.dict_path, self.lib_path)

    def assert_compile_fails(self, line_number, alias_index=None):
        with self.assertRaises(LexiconDictionaryError) as ctx:
            self.compile()
        exc = ctx.exception
        self.assertEqual(exc.line_number, line_number, str(exc))
        if alias_index is not None:
            self.assertEqual(exc.alias_index, alias_index, str(exc))
        # A failed compile must not leave an openable library behind.
        self.assertFalse(os.path.exists(self.lib_path))
        return exc

    def test_valid_dictionary_compiles(self):
        write_lines(self.dict_path, [
            record("A", 5, [{"text": "pump", "boundary": "ascii_word"}]),
            record("B", -3, [{"text": "a", "boundary": "any"},
                             {"text": "b", "boundary": "any"}]),
        ])
        stats = self.compile()
        self.assertEqual(stats.terms, 2)
        self.assertEqual(stats.aliases, 3)
        self.assertTrue(os.path.exists(self.lib_path))
        with LexiconLibrary(self.lib_path) as library:
            self.assertEqual(library.counts()["terms"], 2)

    def test_empty_dictionary_compiles_to_empty_automaton(self):
        write_lines(self.dict_path, [])
        stats = self.compile()
        self.assertEqual(stats.terms, 0)
        self.assertEqual(stats.states, 1)  # just the root
        with LexiconLibrary(self.lib_path) as library:
            self.assertEqual(library.counts()["states"], 1)

    def test_bad_utf8(self):
        with open(self.dict_path, "wb") as handle:
            handle.write(b'{"term_id": "A", "priority": 1, "aliases": ')
            handle.write('[{"text": "'.encode())
            handle.write(b"\xff")
            handle.write('", "boundary": "any"}]}\n'.encode())
        self.assert_compile_fails(1)

    def test_blank_line(self):
        write_lines(self.dict_path, [record("A"), "   ", record("B")])
        self.assert_compile_fails(2)

    def test_duplicate_json_member(self):
        write_lines(self.dict_path, [
            '{"term_id": "A", "term_id": "B", "priority": 1, '
            '"aliases": [{"text": "x", "boundary": "any"}]}',
        ])
        self.assert_compile_fails(1)

    def test_non_object_line(self):
        write_lines(self.dict_path, ['["term_id", "A"]'])
        self.assert_compile_fails(1)

    def test_missing_members(self):
        write_lines(self.dict_path, [{"priority": 1, "aliases": [
            {"text": "x", "boundary": "any"}]}])
        self.assert_compile_fails(1)

    def test_unknown_member(self):
        write_lines(self.dict_path, [
            {"term_id": "A", "priority": 1, "label": "x",
             "aliases": [{"text": "x", "boundary": "any"}]},
        ])
        self.assert_compile_fails(1)

    def test_term_id_empty_or_wrong_type(self):
        write_lines(self.dict_path, [record("")])
        self.assert_compile_fails(1)
        write_lines(self.dict_path, [record(7)])
        self.assert_compile_fails(1)

    def test_priority_bool_is_not_an_integer(self):
        write_lines(self.dict_path, [record("A", True)])
        self.assert_compile_fails(1)

    def test_priority_float_and_string(self):
        write_lines(self.dict_path, [record("A", 1.5)])
        self.assert_compile_fails(1)
        write_lines(self.dict_path, [record("A", "1")])
        self.assert_compile_fails(1)

    def test_priority_beyond_int64(self):
        write_lines(self.dict_path, [record("A", 2 ** 63)])
        self.assert_compile_fails(1)
        write_lines(self.dict_path, [record("A", -(2 ** 63))])
        stats = self.compile()  # INT64_MIN is fine
        self.assertEqual(stats.terms, 1)

    def test_aliases_empty_or_wrong_type(self):
        write_lines(self.dict_path, [record("A", 1, [])])
        self.assert_compile_fails(1)
        write_lines(self.dict_path, [record("A", 1, "pump")])
        self.assert_compile_fails(1)

    def test_alias_not_an_object(self):
        write_lines(self.dict_path, [record("A", 1, ["pump"])])
        self.assert_compile_fails(1, alias_index=0)

    def test_alias_unknown_member(self):
        write_lines(self.dict_path, [record("A", 1, [
            {"text": "pump", "boundary": "any", "note": "n"}])])
        self.assert_compile_fails(1, alias_index=0)

    def test_alias_text_empty(self):
        write_lines(self.dict_path, [record("A", 1, [
            {"text": "", "boundary": "any"}])])
        self.assert_compile_fails(1, alias_index=0)

    def test_unknown_boundary_reports_line_and_alias_index(self):
        write_lines(self.dict_path, [
            record("A", 1, [{"text": "ok", "boundary": "any"}]),
            record("B", 1, [{"text": "fine", "boundary": "any"},
                            {"text": "bad", "boundary": "word"}]),
        ])
        self.assert_compile_fails(2, alias_index=1)

    def test_whitespace_only_alias_is_not_empty_after_folding(self):
        # A whitespace-only alias folds to one U+0020 space — valid, and
        # it matches any whitespace run in the body.
        write_lines(self.dict_path, [record("GAP", 1, [
            {"text": " \t ", "boundary": "any"}])])
        stats = self.compile()
        self.assertEqual(stats.aliases, 1)

    def test_duplicate_term_id_reports_both_lines(self):
        write_lines(self.dict_path, [
            record("A", 1), record("B", 2), record("A", 3),
        ])
        exc = self.assert_compile_fails(3)
        self.assertIn("line 1", str(exc))

    def test_same_alias_text_everywhere_is_kept(self):
        write_lines(self.dict_path, [
            record("A", 1, [{"text": "pump", "boundary": "any"}]),
            record("B", 1, [{"text": "pump", "boundary": "any"}]),
            record("C", 1, [{"text": "Pump", "boundary": "any"},   # folds equal
                            {"text": "PUMP", "boundary": "any"}]),  # folds equal
        ])
        stats = self.compile()
        self.assertEqual(stats.aliases, 4)
        self.assertEqual(stats.outputs, 4)  # nothing dropped for name-sharing

    def test_existing_target_is_refused_and_untouched(self):
        write_lines(self.dict_path, [record("A")])
        self.compile()
        with open(self.lib_path, "rb") as handle:
            before = handle.read()
        with self.assertRaises(LexiconLibraryError):
            self.compile()
        with open(self.lib_path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_record_size_limit(self):
        write_lines(self.dict_path, [record("A", 1, [
            {"text": "x" * 100, "boundary": "any"}])])
        with self.assertRaises(LexiconDictionaryError) as ctx:
            compile_dictionary(self.dict_path, self.lib_path,
                               CompileConfig(max_record_bytes=64))
        self.assertEqual(ctx.exception.line_number, 1)
        self.assertFalse(os.path.exists(self.lib_path))

    def test_dictionary_and_library_must_differ(self):
        write_lines(self.dict_path, [record("A")])
        with self.assertRaises(LexiconLibraryError):
            compile_dictionary(self.dict_path, self.dict_path)


if __name__ == "__main__":
    unittest.main()
