"""Record-level tests: RDW framing, OCCURS, REDEFINES selection, streaming."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from distagg.copybook import (
    DecodeError,
    compile_copybook,
    decode_record,
    iter_records,
    load_selector_config,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "demo", "copybook")
COPYBOOK = os.path.join(DEMO, "order_record.cpy")
SELECTORS = os.path.join(DEMO, "selectors.json")
MAKE_DATA = os.path.join(DEMO, "make_order_feed.py")


def ebc(text):
    return text.encode("cp037")


def rdw(payload):
    return (len(payload) + 4).to_bytes(2, "big") + b"\x00\x00"


def load_demo_layout():
    with open(COPYBOOK, "r", encoding="utf-8") as handle:
        source = handle.read()
    return compile_copybook(source, selectors=load_selector_config(SELECTORS))


class RdwTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layout = load_demo_layout()

    def test_empty_file_yields_nothing(self):
        self.assertEqual(list(iter_records(self.layout, io.BytesIO(b""))), [])

    def test_truncated_rdw(self):
        with self.assertRaises(DecodeError) as ctx:
            list(iter_records(self.layout, io.BytesIO(b"\x00\x3e")))
        exc = ctx.exception
        self.assertEqual(exc.record_no, 1)
        self.assertEqual(exc.offset, 0)
        self.assertIn("truncated RDW", exc.reason)

    def test_rdw_reserved_bytes_must_be_zero(self):
        blob = b"\x00\x06\x00\x01" + b"\xc1\xc2"
        with self.assertRaises(DecodeError) as ctx:
            list(iter_records(self.layout, io.BytesIO(blob)))
        self.assertIn("reserved bytes", ctx.exception.reason)

    def test_rdw_length_smaller_than_rdw(self):
        with self.assertRaises(DecodeError) as ctx:
            list(iter_records(self.layout, io.BytesIO(b"\x00\x03\x00\x00")))
        self.assertIn("smaller than the 4-byte RDW", ctx.exception.reason)

    def test_truncated_payload(self):
        blob = b"\x00\x3e\x00\x00" + b"\xc1" * 10
        with self.assertRaises(DecodeError) as ctx:
            list(iter_records(self.layout, io.BytesIO(blob)))
        exc = ctx.exception
        self.assertEqual(exc.record_no, 1)
        self.assertIn("truncated record", exc.reason)

    def test_record_context_added_to_field_errors(self):
        # One good record (all spaces text-only layout would be easier, but
        # use the demo layout with a garbage DISPLAY date field).
        payload = bytearray(58)
        payload[0:8] = ebc("ORD00001")
        payload[8:16] = b"\xff" * 8          # ORDER-DATE: not EBCDIC digits
        payload[16:19] = ebc("USD")
        payload[21:29] = ebc("ORD00001")
        payload[29:33] = ebc("OP01")
        payload[33] = 0xE2                    # 'S'
        payload[34:40] = ebc("STD-01")
        payload[44:46] = b"\x00\x00"          # count 0
        payload[46:51] = b"\x00\x00\x00\x00\x0c"
        payload[51] = 0xD5                    # 'N'
        payload[52:58] = ebc("US01ZZ")
        blob = rdw(bytes(payload)) + bytes(payload) + rdw(bytes(payload)) \
            + bytes(payload)
        records = []
        with self.assertRaises(DecodeError) as ctx:
            for record in iter_records(self.layout, io.BytesIO(blob)):
                records.append(record)
        exc = ctx.exception
        self.assertEqual(exc.record_no, 1)
        self.assertEqual(exc.offset, 0)
        self.assertEqual(exc.field_path, "ORDER-RECORD.HEADER.ORDER-DATE")
        self.assertEqual(records, [])


class OccursTest(unittest.TestCase):
    def test_count_below_minimum_rejected(self):
        layout = compile_copybook(
            "01 REC.\n"
            "  05 N PIC 9(1) COMP.\n"
            "  05 A OCCURS 1 TO 3 DEPENDING ON REC.N PIC X(2).\n"
        )
        payload = b"\x00\x00"                 # count 0 < min 1
        with self.assertRaises(DecodeError) as ctx:
            decode_record(layout, payload)
        self.assertIn("outside the declared range 1 TO 3", ctx.exception.reason)
        self.assertEqual(ctx.exception.field_path, "REC.A")

    def test_count_above_maximum_rejected(self):
        layout = compile_copybook(
            "01 REC.\n"
            "  05 N PIC 9(1) COMP.\n"
            "  05 A OCCURS 0 TO 2 DEPENDING ON REC.N PIC X(2).\n"
        )
        payload = b"\x00\x03" + b"\xc1\xc1" * 3
        with self.assertRaises(DecodeError) as ctx:
            decode_record(layout, payload)
        self.assertIn("outside the declared range 0 TO 2", ctx.exception.reason)

    def test_trailing_field_after_variable_array(self):
        layout = compile_copybook(
            "01 REC.\n"
            "  05 N PIC 9(1) COMP.\n"
            "  05 A OCCURS 0 TO 3 DEPENDING ON REC.N PIC X(2).\n"
            "  05 TAIL PIC X(1).\n"
        )
        for count in (0, 1, 3):
            payload = bytes([0, count]) + ebc("ab" * count) + ebc("Z")
            out = decode_record(layout, payload)
            self.assertEqual(out["A"], ["ab"] * count)
            self.assertEqual(out["TAIL"], "Z")
            self.assertEqual(out["N"], str(count))

    def test_error_path_carries_occurrence_index(self):
        layout = compile_copybook(
            "01 REC.\n"
            "  05 N PIC 9(1) COMP.\n"
            "  05 A OCCURS 0 TO 3 DEPENDING ON REC.N.\n"
            "    10 D PIC 9(2).\n"
        )
        payload = b"\x00\x03" + b"\xf1\xf1" + b"\xf2\xf2" + b"\xff\xff"
        with self.assertRaises(DecodeError) as ctx:
            decode_record(layout, payload)
        self.assertEqual(ctx.exception.field_path, "REC.A[3].D")

    def test_fixed_occurs_output_is_array(self):
        layout = compile_copybook("01 REC.\n  05 A PIC X(2) OCCURS 3.\n")
        out = decode_record(layout, ebc("a1b2c3"))
        self.assertEqual(out, {"A": ["a1", "b2", "c3"]})


class RedefinesDecodeTest(unittest.TestCase):
    SOURCE = (
        "01 REC.\n"
        "  05 K PIC X(1).\n"
        "  05 SHORT PIC X(2).\n"
        "  05 LONG REDEFINES SHORT.\n"
        "    10 AMOUNT PIC S9(3)V99 COMP-3.\n"
        "    10 FLAG PIC X(1).\n"
        "  05 TAIL PIC X(1).\n"
    )
    SELECTORS = {"REC.SHORT": {"using": "REC.K",
                               "map": {"S": "REC.SHORT", "L": "REC.LONG"}}}

    def layout(self):
        return compile_copybook(self.SOURCE, selectors=self.SELECTORS)

    def test_short_branch_and_slack_skipped(self):
        # Alternation span = max(2, 4) = 4; SHORT uses 2, slack 2 skipped.
        payload = ebc("S") + ebc("Hi") + b"\xff\xff" + ebc("!")
        out = decode_record(self.layout(), payload)
        self.assertEqual(out, {"K": "S", "SHORT": "Hi", "TAIL": "!"})

    def test_long_branch(self):
        payload = ebc("L") + b"\x12\x34\x5c" + ebc("Y") + ebc("!")
        out = decode_record(self.layout(), payload)
        self.assertEqual(out, {"K": "L",
                               "LONG": {"AMOUNT": "123.45", "FLAG": "Y"},
                               "TAIL": "!"})

    def test_unselected_branch_not_validated(self):
        # Garbage packed bytes where LONG.AMOUNT would live: fine while 'S'
        # is selected, an error only when 'L' is selected.
        payload = ebc("S") + ebc("Hi") + b"\xab\xcd" + ebc("!")
        out = decode_record(self.layout(), payload)
        self.assertEqual(out["SHORT"], "Hi")
        payload = ebc("L") + b"\xab\xcd\xef" + ebc("Y") + ebc("!")
        with self.assertRaises(DecodeError) as ctx:
            decode_record(self.layout(), payload)
        self.assertEqual(ctx.exception.field_path, "REC.LONG.AMOUNT")

    def test_unknown_selection_value_rejected(self):
        payload = ebc("?") + ebc("Hi") + b"\x00\x00" + ebc("!")
        with self.assertRaises(DecodeError) as ctx:
            decode_record(self.layout(), payload)
        self.assertIn("unknown selection value '?'", ctx.exception.reason)
        self.assertEqual(ctx.exception.field_path, "REC.SHORT")

    def test_integer_selector(self):
        source = (
            "01 REC.\n"
            "  05 K PIC 9(1).\n"
            "  05 A PIC X(1).\n"
            "  05 B REDEFINES A PIC 9(2).\n"
        )
        layout = compile_copybook(
            source, {"REC.A": {"using": "REC.K",
                               "map": {"1": "REC.A", "2": "REC.B"}}})
        out = decode_record(layout, b"\xf2" + b"\xf5\xf7")
        self.assertEqual(out, {"K": "2", "B": "57"})

    def test_fixed_occurs_on_branch_item(self):
        source = (
            "01 REC.\n"
            "  05 K PIC X(1).\n"
            "  05 A PIC X(2) OCCURS 2.\n"
            "  05 B REDEFINES A PIC X(4).\n"
            "  05 TAIL PIC X(1).\n"
        )
        layout = compile_copybook(
            source, {"REC.A": {"using": "REC.K",
                               "map": {"x": "REC.A", "y": "REC.B"}}})
        out = decode_record(layout, ebc("x") + ebc("a1b2") + ebc("!"))
        self.assertEqual(out, {"K": "x", "A": ["a1", "b2"], "TAIL": "!"})
        out = decode_record(layout, ebc("y") + ebc("wxyz") + ebc("!"))
        self.assertEqual(out, {"K": "y", "B": "wxyz", "TAIL": "!"})


class StreamingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layout = load_demo_layout()
        cls.work = tempfile.mkdtemp(prefix="copybook-test-")
        cls.data = os.path.join(cls.work, "orders.dat")
        cls.expected = os.path.join(cls.work, "expected.json")
        subprocess.run([sys.executable, MAKE_DATA, cls.work], check=True,
                       stdout=subprocess.DEVNULL)

    def test_demo_data_matches_hand_written_expectations(self):
        with open(self.expected, "r", encoding="utf-8") as handle:
            expected = json.load(handle)
        with open(self.data, "rb") as handle:
            records = list(iter_records(self.layout, handle))
        self.assertEqual(len(records), 3)
        for record, want in zip(records, expected["records"]):
            self.assertEqual(record.data, want)
        metas = expected["offsets"]["records"]
        for record, meta in zip(records, metas):
            self.assertEqual(record.offset, meta["file_offset"])
            self.assertEqual(record.length, meta["payload_length"])

    def test_no_read_ahead(self):
        class Counting(io.RawIOBase):
            def __init__(self, raw):
                self.raw = raw
                self.bytes_read = 0

            def read(self, n=-1):
                data = self.raw.read(n)
                self.bytes_read += len(data or b"")
                return data

        size = os.path.getsize(self.data)
        with open(self.data, "rb") as raw:
            counter = Counting(raw)
            stream = iter_records(self.layout, counter)
            first = next(stream)
            self.assertEqual(first.record_no, 1)
            self.assertLess(counter.bytes_read, size)
            self.assertEqual(counter.bytes_read, 62)   # exactly record 1

    def test_many_records_stream(self):
        # 5000 copies of a valid record: count and last content check.
        with open(self.data, "rb") as handle:
            blob = handle.read()
        record1 = blob[:62]
        stream = io.BytesIO(record1 * 5000)
        seen = 0
        last = None
        for record in iter_records(self.layout, stream):
            seen += 1
            last = record
        self.assertEqual(seen, 5000)
        self.assertEqual(last.record_no, 5000)
        self.assertEqual(last.offset, 62 * 4999)
        self.assertEqual(last.data["ORDER-KIND"], "S")
        self.assertEqual(last.data["DETAILS"], [])

    def test_records_before_failure_stay_available(self):
        with open(self.data, "rb") as handle:
            blob = handle.read()
        broken = blob[:62] + b"\x00\xb1\x00\x01"  # bad RDW on record 2
        seen = []
        with self.assertRaises(DecodeError) as ctx:
            for record in iter_records(self.layout, io.BytesIO(broken)):
                seen.append(record)
        self.assertEqual(len(seen), 1)
        self.assertEqual(ctx.exception.record_no, 2)
        self.assertEqual(ctx.exception.offset, 62)


if __name__ == "__main__":
    unittest.main()
