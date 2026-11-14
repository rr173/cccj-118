"""Unit tests for partition codecs, strict JSONL reading and declarations."""

import json
import os
import tempfile
import unittest

from distagg.windowing.codec import decode_partition, encode_partition
from distagg.windowing.declaration import parse_declaration
from distagg.windowing.errors import (
    WindowDeclarationError,
    WindowInputError,
)
from distagg.windowing.jsonl_io import (
    INT64_MAX,
    INT64_MIN,
    iter_jsonl,
    validate_row,
)


def write_file(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.encode("utf-8"))
    return path


class PartitionCodecTest(unittest.TestCase):
    def test_round_trip_strings_and_nulls(self):
        cases = [
            [],
            ["a"],
            [None],
            ["a", None, "b"],
            [None, None],
            ["", "x"],
            ["unicode 日本", "mixed\x00byte"],
            ["ab", "c"],
            ["a", "bc"],
        ]
        for components in cases:
            blob = encode_partition(components)
            self.assertEqual(decode_partition(blob), tuple(components))

    def test_component_boundaries_are_distinct(self):
        self.assertNotEqual(
            encode_partition(["ab", "c"]), encode_partition(["a", "bc"])
        )

    def test_null_equality_and_ordering(self):
        self.assertEqual(encode_partition([None]), encode_partition([None]))
        # null marker 0x00 sorts before text marker 0x01.
        self.assertLess(
            encode_partition([None]), encode_partition([""])
        )

    def test_embedded_zero_byte_escaped(self):
        blob = encode_partition(["a\x00b"])
        # The raw zero byte inside text must not appear without its escape.
        self.assertNotIn(b"\x00\x02", blob)
        self.assertEqual(decode_partition(blob), ("a\x00b",))


class StrictJsonlTest(unittest.TestCase):
    def _parse_all(self, text):
        path = write_file(text)
        try:
            with open(path, "rb") as handle:
                return list(iter_jsonl(handle, path=path))
        finally:
            os.unlink(path)

    def test_blank_and_empty_lines_fail_with_line_numbers(self):
        with self.assertRaises(WindowInputError) as ctx:
            self._parse_all('{"a": 1}\n\n{"a": 2}\n')
        self.assertEqual(ctx.exception.line_number, 2)

    def test_whitespace_only_line_fails(self):
        with self.assertRaises(WindowInputError) as ctx:
            self._parse_all('{"a": 1}\n   \n')
        self.assertEqual(ctx.exception.line_number, 2)

    def test_duplicate_member_fails_at_any_depth(self):
        with self.assertRaises(WindowInputError) as ctx:
            self._parse_all('{"a": {"b": 1, "b": 2}}\n')
        self.assertEqual(ctx.exception.line_number, 1)
        self.assertIn("duplicate", str(ctx.exception))

    def test_duplicate_member_deeply_nested_reports_physical_line(self):
        text = (
            '{"g": "a", "ts": 1, "amount": 1}\n'
            '{"g": "a", "ts": 2, "amount": 2}\n'
            '{"g": "a", "ts": 3, "amount": 3, "meta": {"x": [1, {"y": 1, '
            '"y": 2}]}}\n'
        )
        with self.assertRaises(WindowInputError) as ctx:
            self._parse_all(text)
        self.assertEqual(ctx.exception.line_number, 3)
        self.assertIn("duplicate", str(ctx.exception))

    def test_duplicate_top_level_member_fails(self):
        with self.assertRaises(WindowInputError):
            self._parse_all('{"a": 1, "a": 2}\n')

    def test_non_object_top_level_fails(self):
        for text in ("[1, 2]\n", "12\n", '"str"\n', "true\n", "null\n"):
            with self.subTest(text=text):
                with self.assertRaises(WindowInputError):
                    self._parse_all(text)

    def test_truncated_json_reports_line(self):
        with self.assertRaises(WindowInputError) as ctx:
            self._parse_all('{"a": 1}\n{"a": \n')
        self.assertEqual(ctx.exception.line_number, 2)

    def test_big_integer_is_preserved(self):
        huge = 10 ** 40 + 7
        rows = self._parse_all(json.dumps({"v": huge}) + "\n")
        self.assertEqual(rows[0][2]["v"], huge)
        self.assertIsInstance(rows[0][2]["v"], int)

    def test_crlf_input_is_accepted(self):
        rows = self._parse_all('{"a": 1}\r\n{"a": 2}\r\n')
        self.assertEqual([row[0] for row in rows], [1, 2])

    def test_row_size_limit_enforced(self):
        path = write_file('{"a": "bbbb"}\n')
        try:
            with open(path, "rb") as handle:
                with self.assertRaises(WindowInputError) as ctx:
                    list(iter_jsonl(handle, path=path, max_row_bytes=5))
            self.assertEqual(ctx.exception.line_number, 1)
        finally:
            os.unlink(path)


class RowValidationTest(unittest.TestCase):
    def _validate(self, obj):
        return validate_row(
            obj,
            line_number=1,
            partition_fields=["g1", "g2"],
            order_field="ts",
            value_field="amount",
        )

    def test_missing_field_fails(self):
        with self.assertRaises(WindowInputError):
            self._validate({"g1": "a", "g2": None, "ts": 1})

    def test_partition_wrong_type_fails(self):
        for bad in (1, 1.5, True, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(WindowInputError):
                    self._validate(
                        {"g1": bad, "g2": None, "ts": 1, "amount": 2}
                    )

    def test_order_rejects_bool_float_null_and_out_of_range(self):
        base = {"g1": "a", "g2": None, "amount": 2}
        for bad in (True, False, 1.0, None, "1"):
            with self.subTest(bad=bad):
                with self.assertRaises(WindowInputError):
                    self._validate({**base, "ts": bad})
        with self.assertRaises(WindowInputError):
            self._validate({**base, "ts": INT64_MAX + 1})
        with self.assertRaises(WindowInputError):
            self._validate({**base, "ts": INT64_MIN - 1})

    def test_order_accepts_limits(self):
        base = {"g1": "a", "g2": None, "amount": 2}
        self.assertEqual(self._validate({**base, "ts": INT64_MIN})[1], INT64_MIN)
        self.assertEqual(self._validate({**base, "ts": INT64_MAX})[1], INT64_MAX)

    def test_value_rejects_bool_and_float_but_keeps_big_ints(self):
        base = {"g1": "a", "g2": None, "ts": 1}
        for bad in (True, 1.5, "1"):
            with self.subTest(bad=bad):
                with self.assertRaises(WindowInputError):
                    self._validate({**base, "amount": bad})
        huge = -(10 ** 50)
        _parts, _ts, value = self._validate({**base, "amount": huge})
        self.assertEqual(value, huge)

    def test_null_is_valid_partition_and_value(self):
        parts, ts, value = self._validate(
            {"g1": None, "g2": None, "ts": 0, "amount": None}
        )
        self.assertEqual(parts, [None, None])
        self.assertIsNone(value)


class DeclarationTest(unittest.TestCase):
    def _parse(self, data):
        return parse_declaration(data)

    def test_valid_declaration(self):
        decl = self._parse({
            "partition_by": ["a", "b"],
            "order_by": "ts",
            "value": "amount",
            "lag_lead_k": 3,
            "windows": {
                "w": {"mode": "range".upper(), "start": -10 ** 30,
                      "end": 10 ** 30, "exclude": "group"}
            },
        })
        self.assertEqual(decl.windows["w"].start, -10 ** 30)

    def test_unknown_top_level_member_rejected(self):
        with self.assertRaises(WindowDeclarationError):
            self._parse({
                "partition_by": ["a"], "order_by": "ts", "value": "v",
                "lag_lead_k": 1, "windows": {}, "extra": 1,
            })

    def test_unknown_window_member_rejected(self):
        with self.assertRaises(WindowDeclarationError):
            self._parse({
                "partition_by": ["a"], "order_by": "ts", "value": "v",
                "lag_lead_k": 1,
                "windows": {"w": {"mode": "ROWS", "start": 0, "end": 1,
                                  "exclude": "none", "bogus": True}},
            })

    def test_bad_mode_exclude_and_offsets(self):
        base = {
            "partition_by": ["a"], "order_by": "ts", "value": "v",
            "lag_lead_k": 1,
        }
        with self.assertRaises(WindowDeclarationError):
            self._parse({**base, "windows": {"w": {
                "mode": "GROUPS", "start": 0, "end": 1, "exclude": "none"}}})
        with self.assertRaises(WindowDeclarationError):
            self._parse({**base, "windows": {"w": {
                "mode": "ROWS", "start": 2, "end": 1, "exclude": "none"}}})
        with self.assertRaises(WindowDeclarationError):
            self._parse({**base, "windows": {"w": {
                "mode": "ROWS", "start": 0, "end": 1, "exclude": "all"}}})

    def test_k_must_be_positive_integer(self):
        base = {
            "partition_by": ["a"], "order_by": "ts", "value": "v",
            "windows": {"w": {"mode": "ROWS", "start": 0, "end": 0,
                              "exclude": "none"}},
        }
        for bad in (0, -1, 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(WindowDeclarationError):
                    self._parse({**base, "lag_lead_k": bad})

    def test_empty_partition_by_and_windows_rejected(self):
        with self.assertRaises(WindowDeclarationError):
            self._parse({
                "partition_by": [], "order_by": "ts", "value": "v",
                "lag_lead_k": 1, "windows": {"w": {
                    "mode": "ROWS", "start": 0, "end": 0, "exclude": "none"}}})
        with self.assertRaises(WindowDeclarationError):
            self._parse({
                "partition_by": ["a"], "order_by": "ts", "value": "v",
                "lag_lead_k": 1, "windows": {}})


if __name__ == "__main__":
    unittest.main()
