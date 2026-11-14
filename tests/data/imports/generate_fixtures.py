#!/usr/bin/env python3
"""Deterministically (re)generate the TSV importer fixtures.

Run from the repository root::

    python3 -I tests/data/imports/generate_fixtures.py

The script writes only files inside its own directory and uses no randomness,
so two runs on any machine produce byte-identical outputs.  A test asserts
that regeneration does not change the committed fixtures.

Fixture line map of ``corrections_mixed.tsv`` (header is physical line 1)::

    2  C001  6162/6364                 importable
    3  C002  0x / 0x                   importable (empty key AND empty value)
    4  C003  6b/76                     importable
    5  C001  identical repeat          duplicate of line 2
    6  C004  empty key field           quarantined: empty_field
    7  C005  abc (odd length)          quarantined: hex_odd_length
    8  C006  zz (bad character)        quarantined: hex_bad_character
    9  C007  supplier 'rival'          quarantined: supplier_mismatch
    10 C008  value '6' (odd length)    quarantined: hex_odd_length
    11 (blank physical line)           quarantined: blank_line
    12 C009  wrong field count         quarantined: field_count_mismatch
    13 C002  identical repeat          duplicate of line 3
    14 C010  64/65                     conflict (with line 15)
    15 C010  64/66                     conflict (with line 14)
    16 C011  66/67                     importable
    17 C012  68/69                     importable
    18 C013  6a/6b                     importable

Totals: 6 importable, 2 duplicate, 2 conflict, 7 quarantined.  Two rows
carry the hex_odd_length reason (line 7 key, line 10 value).
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG = {
    "format": 1,
    "supplier_column": "supplier",
    "correction_id_column": "correction_id",
    "key_column": "key_hex",
    "value_column": "value_hex",
    "required_columns": ["supplier", "correction_id", "key_hex", "value_hex"],
    "allowed_extra_columns": ["note"],
    "supplier": {"expected": "acme"},
    "batch_rows": 3,
}

HEADER = "supplier\tcorrection_id\tkey_hex\tvalue_hex\tnote\n"


def write_text(name: str, content: str) -> None:
    with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def write_bytes(name: str, content: bytes) -> None:
    with open(os.path.join(HERE, name), "wb") as handle:
        handle.write(content)


def row(supplier: str, cid: str, key: str, value: str, note: str = "") -> str:
    return f"{supplier}\t{cid}\t{key}\t{value}\t{note}\n"


def main() -> None:
    with open(os.path.join(HERE, "supplier_config.json"), "w", encoding="utf-8") as handle:
        json.dump(CONFIG, handle, indent=2, sort_keys=True)
        handle.write("\n")

    # -- all-importable file: 8 rows, batch_rows=3 -> batches of 3, 3, 2 ----
    good = HEADER
    for i in range(1, 9):
        good += row("acme", "G%03d" % i, "6b%02d" % i, "76%02d" % i, "good-%d" % i)
    write_text("corrections_good.tsv", good)

    # -- mixed file: every classification and every quarantine reason -------
    mixed = HEADER
    mixed += row("acme", "C001", "6162", "6364", "note-1")          # 2 importable
    mixed += row("acme", "C002", "0x", "0x", "empty-bytes")         # 3 importable
    mixed += row("acme", "C003", "6b", "76", "")                    # 4 importable
    mixed += row("acme", "C001", "6162", "6364", "note-1")          # 5 duplicate
    mixed += row("acme", "C004", "", "01", "empty-key")             # 6 empty field
    mixed += row("acme", "C005", "abc", "01", "odd-key")            # 7 odd hex
    mixed += row("acme", "C006", "zz", "01", "bad-hex")             # 8 bad char
    mixed += row("rival", "C007", "61", "62", "wrong-supplier")     # 9 mismatch
    mixed += row("acme", "C008", "6162", "6", "odd-value")          # 10 odd hex
    mixed += "\n"                                                    # 11 blank
    mixed += "acme\tC009\t61\t62\n"                                  # 12 4 fields
    mixed += row("acme", "C002", "0x", "0x", "empty-bytes")         # 13 duplicate
    mixed += row("acme", "C010", "64", "65", "conflict-a")          # 14 conflict
    mixed += row("acme", "C010", "64", "66", "conflict-b")          # 15 conflict
    mixed += row("acme", "C011", "66", "67", "")                    # 16 importable
    mixed += row("acme", "C012", "68", "69", "")                    # 17 importable
    mixed += row("acme", "C013", "6a", "6b", "")                    # 18 importable
    write_text("corrections_mixed.tsv", mixed)

    # -- truncated UTF-8 in a field: 0xFF can never start a UTF-8 sequence --
    broken = HEADER.encode("utf-8")
    broken += row("acme", "U001", "61", "62", "ok").encode("utf-8")
    broken += b"acme\tU002\t62\t63\tbad-\xff-note\n"
    broken += row("acme", "U003", "63", "64", "recovered").encode("utf-8")
    write_bytes("bad_utf8.tsv", broken)

    # -- header-level defects: whole file must be rejected, not partially read
    write_text(
        "bad_header_missing.tsv",
        "supplier\tcorrection_id\tkey_hex\tnote\n" + row("acme", "H001", "61", "62"),
    )
    write_text(
        "bad_header_duplicate.tsv",
        "supplier\tcorrection_id\tkey_hex\tkey_hex\tnote\n"
        + row("acme", "H002", "61", "62"),
    )
    write_text(
        "bad_header_unknown.tsv",
        "supplier\tcorrection_id\tkey_hex\tvalue_hex\tnote\tunexpected\n"
        + "acme\tH003\t61\t62\tx\ty\n",
    )

    # -- no trailing newline: the final physical line must still be parsed --
    write_text("no_trailing_newline.tsv", HEADER + row("acme", "N001", "61", "62").rstrip("\n"))


if __name__ == "__main__":
    main()
