#!/usr/bin/env python3
"""Build the copybook demo data set **without** using distagg.

This script is the independent byte constructor for the ORDER-FEED demo: it
lays out EBCDIC text, zoned DISPLAY numerics, big-endian COMP binary and
packed COMP-3 decimals by hand, frames each record with an RDW, and writes

* ``orders.dat``    — the RDW-framed export file, and
* ``expected.json`` — the hand-written expected decode output plus key byte
                      offsets, which ``demo/copybook_demo.py`` checks the
                      real decoder against.

Nothing here imports distagg, so the demo is a genuine contract test of the
decoder, not a round trip through the project's own code.

Usage:  python3 demo/copybook/make_order_feed.py [OUTPUT_DIR]
(default: ``demo/copybook/data``)
"""

import json
import os
import sys

# ---------------------------------------------------------------------------
# Hand-rolled byte builders (independent of the decoder under test).
# ---------------------------------------------------------------------------

def ebc(text):
    """EBCDIC cp037 bytes of a text field, exactly as written (no padding)."""
    return text.encode("cp037")


def zoned(digits):
    """Unsigned DISPLAY numeric: one EBCDIC digit byte per character."""
    assert all("0" <= ch <= "9" for ch in digits)
    return digits.encode("cp037")


def comp(value, nbytes):
    """Unsigned big-endian COMP binary."""
    return value.to_bytes(nbytes, "big")


def packed(value, total_digits, sign=None):
    """COMP-3 packed decimal for a scaled integer (value = amount x 100)."""
    if sign is None:
        sign = 0xD if value < 0 else 0xC
    nibbles = [int(ch) for ch in str(abs(value)).rjust(total_digits, "0")]
    assert len(nibbles) == total_digits
    nibbles.append(sign)
    if len(nibbles) % 2:
        nibbles.insert(0, 0)          # leading padding nibble
    out = bytearray()
    for i in range(0, len(nibbles), 2):
        out.append((nibbles[i] << 4) | nibbles[i + 1])
    return bytes(out)


def rdw(payload):
    """Record descriptor word: length (including itself) + two zero bytes."""
    return (len(payload) + 4).to_bytes(2, "big") + b"\x00\x00"


def detail(line_no, sku, qty, unit_price, discount, tags, price_sign=None,
           discount_sign=None):
    """One DETAILS occurrence: 2+6+2+4+3+3*2 = 23 bytes."""
    assert len(tags) == 3 and all(len(t) == 2 for t in tags)
    return b"".join([
        zoned(line_no),
        ebc(sku),
        comp(qty, 2),
        packed(unit_price, 7, price_sign),
        packed(discount, 5, discount_sign),
        b"".join(ebc(t) for t in tags),
    ])


def record(header_id, date, currency, filler, audit_id, operator, kind, body,
           count, details, total, status, routing):
    """Assemble one ORDER-RECORD payload (body = raw 10-byte alternation)."""
    assert len(body) == 10, "the REDEFINES group always occupies 10 bytes"
    payload = b"".join([
        ebc(header_id), zoned(date), ebc(currency), filler,
        ebc(audit_id), ebc(operator),
        ebc(kind),
        body,
        comp(count, 2),
        b"".join(details),
        packed(total, 9),
        ebc(status),
        b"".join(routing),
    ])
    assert len(payload) == 46 + 23 * len(details) + 12
    return payload


# ---------------------------------------------------------------------------
# The three demo records.
# ---------------------------------------------------------------------------

def record_1():
    """Standard order, zero details (TRAILER follows DETAIL-COUNT directly)."""
    return record(
        "ORD00001", "20240115", "USD", b"\x40\x40",
        "ORD00001", "OP01",
        "S",
        ebc("STD-01") + b"\x40\x40\x40\x40",      # 6 used + 4 slack spaces
        0, [],
        0, "N",
        [ebc("US"), ebc("01"), ebc("ZZ")],
    )


def record_2():
    """Extended order, the maximum five details, negative packed values."""
    details = [
        detail("01", "SKU001", 2, 123456, -1250, ["A1", "B2", "C3"]),
        detail("02", "SKU002", 1, -5, 0, ["X ", " Y", "ZZ"]),
        detail("03", "SKU003", 100, 9999999, -99999, ["T1", "T2", "T3"]),
        detail("04", "SKU004", 3, 1, 10, ["N ", "E ", "W "],
               price_sign=0xF),                    # F = non-negative, like C
        detail("05", "SKU005", 7, 5550, -25, ["L ", "A ", "S "]),
    ]
    return record(
        "ORD00002", "20240116", "EUR", b"\x00\x00",
        "ORD00002", "OP02",
        "E",
        ebc("Y") + ebc("LOY123456"),               # GIFT-FLAG + LOYALTY-ID
        5, details,
        678901, "R",
        [ebc("EU"), ebc("02"), ebc("EX")],
    )


def record_3():
    """Standard order, two details, F sign nibbles and a NUL in text."""
    details = [
        detail("01", "SKU010", 5, 1000, 0, ["P ", "Q ", "R "],
               price_sign=0xF, discount_sign=0xF),
        detail("02", "SKU011", 9, 2025, -125, ["S ", "T ", "U "]),
    ]
    return record(
        "ORD00003", "20240117", "GBP", b"\x40\x40",
        "ORD00003", "OP03",
        "S",
        ebc("STD-03") + b"\x00\x00\x00\x00",       # 6 used + 4 slack low-values
        2, details,
        2900, "N",
        [ebc("GB"), ebc("03"), b"\xc1\x00"],       # "A" + NUL byte, preserved
    )


RECORDS = [record_1(), record_2(), record_3()]

# ---------------------------------------------------------------------------
# Hand-written expectations: what the decoder must produce, and where the
# interesting bytes sit.  Written out literally on purpose.
# ---------------------------------------------------------------------------

EXPECTED_RECORDS = [
    {
        "HEADER": {"ORDER-ID": "ORD00001", "ORDER-DATE": "20240115",
                   "CURRENCY": "USD"},
        "AUDIT": {"ORDER-ID": "ORD00001", "OPERATOR": "OP01"},
        "ORDER-KIND": "S",
        "ORDER-BODY": "STD-01",
        "DETAIL-COUNT": "0",
        "DETAILS": [],
        "TRAILER": {"ORDER-TOTAL": "0.00", "STATUS-FLAG": "N",
                    "ROUTING-CODE": ["US", "01", "ZZ"]},
    },
    {
        "HEADER": {"ORDER-ID": "ORD00002", "ORDER-DATE": "20240116",
                   "CURRENCY": "EUR"},
        "AUDIT": {"ORDER-ID": "ORD00002", "OPERATOR": "OP02"},
        "ORDER-KIND": "E",
        "EXTENDED-BODY": {"GIFT-FLAG": "Y", "LOYALTY-ID": "LOY123456"},
        "DETAIL-COUNT": "5",
        "DETAILS": [
            {"LINE-NO": "1", "SKU": "SKU001", "QTY": "2",
             "UNIT-PRICE": "1234.56", "DISCOUNT": "-12.50",
             "TAGS": ["A1", "B2", "C3"]},
            {"LINE-NO": "2", "SKU": "SKU002", "QTY": "1",
             "UNIT-PRICE": "-0.05", "DISCOUNT": "0.00",
             "TAGS": ["X ", " Y", "ZZ"]},
            {"LINE-NO": "3", "SKU": "SKU003", "QTY": "100",
             "UNIT-PRICE": "99999.99", "DISCOUNT": "-999.99",
             "TAGS": ["T1", "T2", "T3"]},
            {"LINE-NO": "4", "SKU": "SKU004", "QTY": "3",
             "UNIT-PRICE": "0.01", "DISCOUNT": "0.10",
             "TAGS": ["N ", "E ", "W "]},
            {"LINE-NO": "5", "SKU": "SKU005", "QTY": "7",
             "UNIT-PRICE": "55.50", "DISCOUNT": "-0.25",
             "TAGS": ["L ", "A ", "S "]},
        ],
        "TRAILER": {"ORDER-TOTAL": "6789.01", "STATUS-FLAG": "R",
                    "ROUTING-CODE": ["EU", "02", "EX"]},
    },
    {
        "HEADER": {"ORDER-ID": "ORD00003", "ORDER-DATE": "20240117",
                   "CURRENCY": "GBP"},
        "AUDIT": {"ORDER-ID": "ORD00003", "OPERATOR": "OP03"},
        "ORDER-KIND": "S",
        "ORDER-BODY": "STD-03",
        "DETAIL-COUNT": "2",
        "DETAILS": [
            {"LINE-NO": "1", "SKU": "SKU010", "QTY": "5",
             "UNIT-PRICE": "10.00", "DISCOUNT": "0.00",
             "TAGS": ["P ", "Q ", "R "]},
            {"LINE-NO": "2", "SKU": "SKU011", "QTY": "9",
             "UNIT-PRICE": "20.25", "DISCOUNT": "-1.25",
             "TAGS": ["S ", "T ", "U "]},
        ],
        "TRAILER": {"ORDER-TOTAL": "29.00", "STATUS-FLAG": "N",
                    "ROUTING-CODE": ["GB", "03", "A\x00"]},
    },
]

# Key byte offsets, computed by hand from the copybook (payload offsets are
# 0-based inside the record payload, i.e. after the 4-byte RDW).
OFFSETS = {
    "payload": {
        "ORDER-KIND": 33,
        "ORDER-BODY/EXTENDED-BODY": 34,
        "DETAIL-COUNT": 44,
        "DETAILS": 46,
        "DETAIL-STRIDE": 23,
    },
    "records": [
        {"file_offset": 0, "rdw_length": 62, "payload_length": 58,
         "TRAILER.ORDER-TOTAL_payload_offset": 46},
        {"file_offset": 62, "rdw_length": 177, "payload_length": 173,
         "TRAILER.ORDER-TOTAL_payload_offset": 161},
        {"file_offset": 239, "rdw_length": 108, "payload_length": 104,
         "TRAILER.ORDER-TOTAL_payload_offset": 92},
    ],
    "file_length": 347,
}


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out_dir, exist_ok=True)

    blob = b"".join(rdw(payload) + payload for payload in RECORDS)
    assert len(blob) == OFFSETS["file_length"], (len(blob), OFFSETS["file_length"])
    for payload, meta in zip(RECORDS, OFFSETS["records"]):
        assert len(payload) == meta["payload_length"]
        assert len(payload) + 4 == meta["rdw_length"]

    data_path = os.path.join(out_dir, "orders.dat")
    with open(data_path, "wb") as handle:
        handle.write(blob)

    expected_path = os.path.join(out_dir, "expected.json")
    with open(expected_path, "w", encoding="utf-8") as handle:
        json.dump({"records": EXPECTED_RECORDS, "offsets": OFFSETS},
                  handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    print(f"wrote {data_path} ({len(blob)} bytes, {len(RECORDS)} records)")
    print(f"wrote {expected_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
