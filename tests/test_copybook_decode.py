"""Field-level decoding tests: DISPLAY, COMP, COMP-3, text, decimal output."""

import unittest

from distagg.copybook import (
    DecodeError,
    compile_copybook,
    decode_record,
    format_decimal,
)


def layout_for(body, selectors=None):
    return compile_copybook(f"01 REC.\n{body}\n", selectors=selectors)


def decode_one(pic_usage, payload):
    layout = layout_for(f"  05 F {pic_usage}.\n")
    return decode_record(layout, payload)["F"]


def decode_bad(pic_usage, payload):
    try:
        decode_one(pic_usage, payload)
    except DecodeError as exc:
        return exc
    raise AssertionError("record unexpectedly decoded")


class FormatDecimalTest(unittest.TestCase):
    def test_scale_preserved(self):
        self.assertEqual(format_decimal(12345, 2), "123.45")
        self.assertEqual(format_decimal(5, 2), "0.05")
        self.assertEqual(format_decimal(-5, 2), "-0.05")
        self.assertEqual(format_decimal(0, 2), "0.00")
        self.assertEqual(format_decimal(-1250, 2), "-12.50")
        self.assertEqual(format_decimal(42, 0), "42")
        self.assertEqual(format_decimal(-7, 0), "-7")
        self.assertEqual(format_decimal(999999999999999999, 0),
                         "999999999999999999")


class TextTest(unittest.TestCase):
    def test_spaces_and_nuls_preserved_exact(self):
        value = decode_one("PIC X(4)", b"\xc1\x40\x00\xe9")
        self.assertEqual(value, "A \x00Z")
        self.assertEqual(len(value), 4)

    def test_no_trimming(self):
        self.assertEqual(decode_one("PIC X(3)", b"\x40\x40\x40"), "   ")


class DisplayTest(unittest.TestCase):
    def test_digits(self):
        self.assertEqual(decode_one("PIC 9(5)", b"\xf0\xf0\xf0\xf4\xf2"), "42")
        self.assertEqual(decode_one("PIC 9(1)", b"\xf0"), "0")

    def test_non_digit_byte_rejected(self):
        exc = decode_bad("PIC 9(3)", b"\xf1\xf2\xc1")
        self.assertIn("not an EBCDIC digit", exc.reason)
        self.assertEqual(exc.field_path, "REC.F")

    def test_space_not_a_digit(self):
        exc = decode_bad("PIC 9(2)", b"\x40\xf1")
        self.assertIsInstance(exc, DecodeError)

    def test_display_with_decimals(self):
        # 9(3)V99 DISPLAY: 5 digit bytes, implied decimal point.
        self.assertEqual(decode_one("PIC 9(3)V99", b"\xf1\xf2\xf3\xf4\xf5"),
                         "123.45")


class CompTest(unittest.TestCase):
    def test_sizes_by_digits(self):
        self.assertEqual(decode_one("PIC 9(4) COMP", b"\x00\x2a"), "42")
        self.assertEqual(decode_one("PIC 9(5) COMP", b"\x00\x00\x00\x2a"), "42")
        self.assertEqual(
            decode_one("PIC 9(10) COMP", b"\x00\x00\x00\x00\x00\x00\x00\x2a"),
            "42")

    def test_signed(self):
        self.assertEqual(decode_one("PIC S9(4) COMP", b"\xff\xff"), "-1")
        self.assertEqual(decode_one("PIC S9(2) COMP", b"\xff\x9d"), "-99")

    def test_unsigned_range_checked(self):
        exc = decode_bad("PIC 9(2) COMP", b"\x00\x64")   # 100 > 99
        self.assertIn("does not fit", exc.reason)

    def test_signed_range_checked(self):
        exc = decode_bad("PIC S9(2) COMP", b"\x80\x00")  # -32768 < -99
        self.assertIn("does not fit", exc.reason)
        exc = decode_bad("PIC S9(2) COMP", b"\x00\x64")  # 100 > 99
        self.assertIn("does not fit", exc.reason)

    def test_comp_with_implied_decimals(self):
        # S9(3)V99 COMP: 5 digits -> 4 bytes, value scaled by 100.
        self.assertEqual(
            decode_one("PIC S9(3)V99 COMP", b"\xff\xff\xf3\x0c"), "-33.16")
        self.assertEqual(
            decode_one("PIC S9(3)V99 COMP", b"\x00\x00\x30\x39"), "123.45")


class Comp3Test(unittest.TestCase):
    def test_positive_signs(self):
        self.assertEqual(decode_one("PIC S9(3) COMP-3", b"\x12\x3c"), "123")
        self.assertEqual(decode_one("PIC S9(3) COMP-3", b"\x12\x3f"), "123")

    def test_negative(self):
        self.assertEqual(decode_one("PIC S9(3) COMP-3", b"\x12\x3d"), "-123")

    def test_unsigned_rejects_negative(self):
        exc = decode_bad("PIC 9(3) COMP-3", b"\x12\x3d")
        self.assertIn("unsigned", exc.reason)

    def test_unsigned_accepts_c_and_f(self):
        self.assertEqual(decode_one("PIC 9(3) COMP-3", b"\x12\x3c"), "123")
        self.assertEqual(decode_one("PIC 9(3) COMP-3", b"\x12\x3f"), "123")

    def test_bad_sign_nibble(self):
        for sign in (0xA, 0xB, 0xE, 0x0, 0x9):
            exc = decode_bad("PIC S9(3) COMP-3", bytes([0x12, 0x30 | sign]))
            self.assertIn("sign nibble", exc.reason, hex(sign))

    def test_bad_digit_nibble(self):
        exc = decode_bad("PIC S9(3) COMP-3", b"\x1a\x3c")
        self.assertIn("invalid packed digit 0xA", exc.reason)
        self.assertEqual(exc.field_path, "REC.F")

    def test_padding_nibble_must_be_zero(self):
        # 9(4) COMP-3 -> 3 bytes, high nibble of first byte is padding.
        self.assertEqual(decode_one("PIC 9(4) COMP-3", b"\x01\x23\x4f"), "1234")
        exc = decode_bad("PIC 9(4) COMP-3", b"\x11\x23\x4f")
        self.assertIn("padding nibble", exc.reason)

    def test_decimals(self):
        self.assertEqual(decode_one("PIC S9(5)V99 COMP-3", b"\x00\x12\x34\x5d"),
                         "-123.45")
        self.assertEqual(decode_one("PIC S9(3)V99 COMP-3", b"\x00\x00\x5d"),
                         "-0.05")

    def test_negative_zero_normalised(self):
        self.assertEqual(decode_one("PIC S9(3)V99 COMP-3", b"\x00\x00\x0d"),
                         "0.00")

    def test_illegal_digits_never_repaired(self):
        exc = decode_bad("PIC S9(5) COMP-3", b"\xff\xff\xfc")
        self.assertIsInstance(exc, DecodeError)


class RecordFramingTest(unittest.TestCase):
    def test_record_too_short_names_field(self):
        layout = layout_for("  05 A PIC X(4).\n  05 B PIC 9(3).\n")
        try:
            decode_record(layout, b"\xc1\xc2\xc3\xc4\xf1")
        except DecodeError as exc:
            self.assertEqual(exc.field_path, "REC.B")
            self.assertIn("record too short", exc.reason)
        else:
            raise AssertionError("short record decoded")

    def test_trailing_bytes_rejected(self):
        layout = layout_for("  05 A PIC X(2).\n")
        try:
            decode_record(layout, b"\xc1\xc2\xc3")
        except DecodeError as exc:
            self.assertIn("1 trailing byte(s)", exc.reason)
            self.assertIsNone(exc.field_path)
        else:
            raise AssertionError("trailing bytes accepted")

    def test_filler_skipped_not_output_not_validated(self):
        layout = layout_for(
            "  05 A PIC X(1).\n  05 FILLER PIC 9(2).\n  05 B PIC X(1).\n")
        # FILLER bytes 0xff 0xff would be illegal digits - not validated.
        out = decode_record(layout, b"\xc1\xff\xff\xc2")
        self.assertEqual(out, {"A": "A", "B": "B"})


if __name__ == "__main__":
    unittest.main()
