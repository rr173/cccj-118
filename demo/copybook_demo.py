#!/usr/bin/env python3
"""End-to-end demonstration of the distagg copybook decoder.

Steps:

1. regenerate the demo data set with the *independent* byte-construction
   script ``demo/copybook/make_order_feed.py`` (no distagg code involved);
2. compile the copybook and show the layout report (the ``check`` command),
   including the dynamic offsets behind the variable-length array;
3. decode the file record by record and compare every record against the
   hand-written expected values from ``expected.json``;
4. verify the hand-computed key byte offsets against the raw file bytes.

Run:  python3 demo/copybook_demo.py
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from distagg.copybook import compile_copybook, iter_file, load_selector_config

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copybook")
COPYBOOK = os.path.join(HERE, "order_record.cpy")
SELECTORS = os.path.join(HERE, "selectors.json")
MAKE_DATA = os.path.join(HERE, "make_order_feed.py")


def main() -> int:
    work = tempfile.mkdtemp(prefix="copybook-demo-")
    data = os.path.join(work, "orders.dat")
    expected_path = os.path.join(work, "expected.json")

    print("== 1. build demo data with the independent byte constructor ==")
    subprocess.run([sys.executable, MAKE_DATA, work], check=True)

    with open(COPYBOOK, "r", encoding="utf-8") as handle:
        source = handle.read()
    selectors = load_selector_config(SELECTORS)
    layout = compile_copybook(source, selectors=selectors)

    print("\n== 2. layout report (distagg copybook check) ==")
    report = layout.describe()
    print(f"record {report['record']}: span {report['span']}")
    for field in report["fields"]:
        occurs = field.get("occurs")
        redef = field.get("redefines")
        extras = []
        if occurs:
            extras.append(f"occurs={occurs}")
        if redef:
            extras.append(f"redefines={redef['role']}")
        if field["offset_kind"] == "dynamic":
            offset = "dynamic"
        else:
            offset = str(field["offset"])
        print(f"  {field['path']:<38} {field['kind']:<8} offset={offset:<7} "
              f"span={field['span']} {' '.join(extras)}")

    print("\n== 3. decode and compare with hand-written expectations ==")
    with open(expected_path, "r", encoding="utf-8") as handle:
        expected = json.load(handle)
    decoded = list(iter_file(layout, data))
    assert len(decoded) == len(expected["records"]), (
        len(decoded), len(expected["records"]))
    for record, want in zip(decoded, expected["records"]):
        assert record.data == want, (
            f"record {record.record_no} mismatch:\n"
            f"got  {json.dumps(record.data, sort_keys=True)}\n"
            f"want {json.dumps(want, sort_keys=True)}")
        print(f"  record {record.record_no} (file offset {record.offset}, "
              f"payload {record.length} bytes): matches hand-written values")
    print("  all records match the hand-written expected values")

    print("\n== 4. verify hand-computed key byte offsets ==")
    with open(data, "rb") as handle:
        blob = handle.read()
    offsets = expected["offsets"]
    assert len(blob) == offsets["file_length"]

    def file_offset(rec_index, payload_offset):
        return offsets["records"][rec_index]["file_offset"] + 4 + payload_offset

    checks = [
        # (description, file offset, expected raw bytes)
        ("record 1 RDW length = 62", 0, b"\x00\x3e"),
        ("record 1 RDW reserved bytes are zero", 2, b"\x00\x00"),
        ("record 1 ORDER-KIND 'S' (payload 33)", file_offset(0, 33), b"\xe2"),
        ("record 1 DETAIL-COUNT = 0 (payload 44)", file_offset(0, 44),
         b"\x00\x00"),
        ("record 1 ORDER-TOTAL packed +0.00 (payload 46)",
         file_offset(0, 46), b"\x00\x00\x00\x00\x0c"),
        ("record 2 RDW at file offset 62", 62, b"\x00\xb1\x00\x00"),
        ("record 2 ORDER-KIND 'E' (payload 33)", file_offset(1, 33), b"\xc5"),
        ("record 2 DETAILS[5].UNIT-PRICE 55.50 (payload 148)",
         file_offset(1, 148), b"\x00\x05\x55\x0c"),
        ("record 2 ORDER-TOTAL packed 6789.01 (payload 161)",
         file_offset(1, 161), b"\x00\x06\x78\x90\x1c"),
        ("record 3 RDW at file offset 239", 239, b"\x00\x6c\x00\x00"),
        ("record 3 ORDER-TOTAL packed 29.00 (payload 92)",
         file_offset(2, 92), b"\x00\x00\x02\x90\x0c"),
        ("record 3 last ROUTING-CODE 'A'+NUL (payload 102)",
         file_offset(2, 102), b"\xc1\x00"),
    ]
    for description, at, want in checks:
        got = blob[at:at + len(want)]
        assert got == want, (description, at, got.hex(), want.hex())
        print(f"  offset {at:>4}: {description:<48} -> {got.hex()} OK")

    print("\ncopybook demo OK: layout, values and byte offsets all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
