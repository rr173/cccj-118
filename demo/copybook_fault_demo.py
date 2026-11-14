#!/usr/bin/env python3
"""Fault-injection demonstration for the copybook decoder.

Starts from the good demo data set (rebuilt by the independent constructor)
and breaks it in seven ways — a bad RDW, an illegal packed digit nibble, an
out-of-range DEPENDING ON count, an unknown REDEFINES selector value, a
truncated record, a record that is too short for the layout, and trailing
bytes — checking each time that the reported record number, file byte
offset, field path and reason point at the actual corruption.

Run:  python3 demo/copybook_fault_demo.py
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from distagg.copybook import (
    DecodeError,
    compile_copybook,
    iter_records,
    load_selector_config,
)

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copybook")
COPYBOOK = os.path.join(HERE, "order_record.cpy")
SELECTORS = os.path.join(HERE, "selectors.json")
MAKE_DATA = os.path.join(HERE, "make_order_feed.py")


def mutate(blob, offset, data):
    return blob[:offset] + data + blob[offset + len(data):]


def build_cases(blob):
    """Return [(name, mutated_bytes, expected record_no/offset/field/reason)]."""
    cases = []

    # 1. record 2's RDW reserved bytes (file offset 62..63) become 00 01.
    cases.append((
        "RDW reserved bytes not zero",
        mutate(blob, 62, b"\x00\xb1\x00\x01"),
        {"record_no": 2, "offset": 62, "field_path": None,
         "reason": "RDW reserved bytes must be zero"},
    ))

    # 2. record 2, DETAILS[1].UNIT-PRICE first packed byte -> 0xAB.
    #    payload starts at 66, DETAILS at payload 46, UNIT-PRICE at +10.
    cases.append((
        "illegal COMP-3 digit nibble",
        mutate(blob, 66 + 56, b"\xab"),
        {"record_no": 2, "offset": 62,
         "field_path": "ORDER-RECORD.DETAILS[1].UNIT-PRICE",
         "reason": "invalid packed digit 0xA"},
    ))

    # 3. record 3 DETAIL-COUNT (file offset 239+4+44) set to 9 > max 5.
    cases.append((
        "DEPENDING ON count out of range",
        mutate(blob, 239 + 4 + 44, b"\x00\x09"),
        {"record_no": 3, "offset": 239, "field_path": "ORDER-RECORD.DETAILS",
         "reason": "DEPENDING ON count 9"},
    ))

    # 4. record 1 ORDER-KIND (file offset 4+33) 'S' -> 'X': no branch mapped.
    cases.append((
        "unknown REDEFINES selection value",
        mutate(blob, 4 + 33, b"\xe7"),
        {"record_no": 1, "offset": 0, "field_path": "ORDER-RECORD.ORDER-BODY",
         "reason": "unknown selection value 'X'"},
    ))

    # 5. file cut 100 bytes into record 2: RDW promises 173 payload bytes.
    cases.append((
        "truncated record payload",
        blob[:62 + 100],
        {"record_no": 2, "offset": 62, "field_path": None,
         "reason": "truncated record"},
    ))

    # 6. record 1's RDW shrunk to 20: honest RDW, but the layout needs more.
    cases.append((
        "record too short for the layout",
        b"\x00\x14\x00\x00" + blob[4:20] + blob[62:],
        {"record_no": 1, "offset": 0,
         "field_path": "ORDER-RECORD.HEADER.CURRENCY",
         "reason": "record too short"},
    ))

    # 7. record 1 padded with 3 extra bytes (RDW says 65): trailing bytes.
    cases.append((
        "trailing bytes after the layout",
        b"\x00\x41\x00\x00" + blob[4:62] + b"\xde\xad\xbe" + blob[62:],
        {"record_no": 1, "offset": 0, "field_path": None,
         "reason": "3 trailing byte(s)"},
    ))
    return cases


def main() -> int:
    work = tempfile.mkdtemp(prefix="copybook-fault-demo-")
    data = os.path.join(work, "orders.dat")
    subprocess.run([sys.executable, MAKE_DATA, work], check=True,
                   stdout=subprocess.DEVNULL)
    with open(data, "rb") as handle:
        blob = handle.read()

    with open(COPYBOOK, "r", encoding="utf-8") as handle:
        source = handle.read()
    layout = compile_copybook(source, selectors=load_selector_config(SELECTORS))

    import io
    print("== fault cases (decoded through the Python API) ==")
    for name, mutated, want in build_cases(blob):
        decoded_before = 0
        try:
            for _ in iter_records(layout, io.BytesIO(mutated)):
                decoded_before += 1
        except DecodeError as exc:
            got = {
                "record_no": exc.record_no,
                "offset": exc.offset,
                "field_path": exc.field_path,
            }
            for key, value in want.items():
                if key == "reason":
                    assert value in exc.reason, (name, exc.reason)
                else:
                    assert got[key] == value, (name, key, got[key], value)
            print(f"\n  case: {name}")
            print(f"  decoded {decoded_before} good record(s) before the error")
            print("  " + json.dumps(exc.to_dict(), ensure_ascii=False,
                                      sort_keys=True))
        else:
            raise AssertionError(f"case {name!r} unexpectedly decoded cleanly")

    print("\n== same failure through the CLI (exit code and streams) ==")
    bad_path = os.path.join(work, "bad-nibble.dat")
    with open(bad_path, "wb") as handle:
        handle.write(build_cases(blob)[1][1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    proc = subprocess.run(
        [sys.executable, "-m", "distagg", "copybook", "decode",
         COPYBOOK, bad_path, "--select", SELECTORS],
        capture_output=True, text=True, env=env)
    print(f"  exit code: {proc.returncode}")
    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    print(f"  stdout: {len(stdout_lines)} decoded record line(s) kept")
    print(f"  stderr: {proc.stderr.strip()}")
    assert proc.returncode == 1
    assert len(stdout_lines) == 1                     # record 1 already out
    error = json.loads(proc.stderr)
    assert error["record_no"] == 2 and error["offset"] == 62
    assert error["field_path"] == "ORDER-RECORD.DETAILS[1].UNIT-PRICE"

    print("\nfault demo OK: every corruption reported at its real position")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
