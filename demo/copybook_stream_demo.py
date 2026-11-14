#!/usr/bin/env python3
"""Streaming demonstration: decode many records without loading the file.

A generator writes ``N`` ORDER-RECORDs to a file one after another; the
decoder then consumes them as a stream.  The demo verifies the record count
and the first/last record contents (recomputed independently from the
generator parameters), and shows that

* a counting file wrapper proves the decoder reads only a handful of bytes
  ahead of the current record — never the whole file, and
* peak memory during the full decode (tracemalloc) stays flat while the
  file size grows with ``N``.

Run:  python3 demo/copybook_stream_demo.py [N]   (default N = 20000)
"""

import os
import sys
import tempfile
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "copybook"))

import make_order_feed as feed  # the independent byte constructor

from distagg.copybook import compile_copybook, iter_records, load_selector_config

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copybook")
COPYBOOK = os.path.join(HERE, "order_record.cpy")
SELECTORS = os.path.join(HERE, "selectors.json")


def generate(path, count):
    """Write `count` records; record i has i%6 details, alternating kinds."""
    with open(path, "wb") as handle:
        for i in range(count):
            details = [
                feed.detail(f"{k + 1:02d}", f"SKU{k + 1:03d}", k + 1,
                            1000 * (k + 1) + i % 7, -50 * k,
                            ["T1", "T2", "T3"])
                for k in range(i % 6)
            ]
            kind = "E" if i % 2 else "S"
            body = (feed.ebc("Y") + feed.ebc(f"LOY{i % 1000:06d}")) if kind == "E" \
                else (feed.ebc(f"STD-{i % 100:02d}") + b"\x40" * 4)
            payload = feed.record(
                f"ORD{i % 100000:05d}", "20240201", "USD", b"\x40\x40",
                f"ORD{i % 100000:05d}", "OP01",
                kind, body, len(details), details,
                100 * len(details), "N",
                [feed.ebc("US"), feed.ebc("01"), feed.ebc("ZZ")],
            )
            handle.write(feed.rdw(payload) + payload)


def money(scaled):
    """Decimal string for a scaled-by-100 integer, no floats involved."""
    sign = "-" if scaled < 0 else ""
    scaled = abs(scaled)
    return f"{sign}{scaled // 100}.{scaled % 100:02d}"


def expected_record(i):
    """Independently recomputed expected content of record i (1-based)."""
    n = (i - 1) % 6
    kind = "E" if (i - 1) % 2 else "S"
    data = {
        "HEADER": {"ORDER-ID": f"ORD{(i - 1) % 100000:05d}",
                   "ORDER-DATE": "20240201", "CURRENCY": "USD"},
        "AUDIT": {"ORDER-ID": f"ORD{(i - 1) % 100000:05d}", "OPERATOR": "OP01"},
        "ORDER-KIND": kind,
        "DETAIL-COUNT": str(n),
        "DETAILS": [
            {"LINE-NO": str(k + 1), "SKU": f"SKU{k + 1:03d}", "QTY": str(k + 1),
             "UNIT-PRICE": money(1000 * (k + 1) + (i - 1) % 7),
             "DISCOUNT": money(-50 * k), "TAGS": ["T1", "T2", "T3"]}
            for k in range(n)
        ],
        "TRAILER": {"ORDER-TOTAL": money(100 * n), "STATUS-FLAG": "N",
                    "ROUTING-CODE": ["US", "01", "ZZ"]},
    }
    if kind == "E":
        data["EXTENDED-BODY"] = {"GIFT-FLAG": "Y",
                                 "LOYALTY-ID": f"LOY{(i - 1) % 1000:06d}"}
    else:
        data["ORDER-BODY"] = f"STD-{(i - 1) % 100:02d}"
    return data


class CountingReader:
    """File wrapper that reports how many bytes were actually read."""

    def __init__(self, handle):
        self._handle = handle
        self.bytes_read = 0

    def read(self, n=-1):
        data = self._handle.read(n)
        self.bytes_read += len(data)
        return data


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    work = tempfile.mkdtemp(prefix="copybook-stream-demo-")
    data = os.path.join(work, "orders.dat")

    print(f"== generate {count} records ==")
    generate(data, count)
    file_size = os.path.getsize(data)
    print(f"  file size: {file_size:,} bytes")

    with open(COPYBOOK, "r", encoding="utf-8") as handle:
        source = handle.read()
    layout = compile_copybook(source, selectors=load_selector_config(SELECTORS))

    print("== laziness: one record pulled, only its bytes read ==")
    with open(data, "rb") as handle:
        reader = CountingReader(handle)
        stream = iter_records(layout, reader)
        first = next(stream)
        print(f"  after record 1: {reader.bytes_read} bytes read of "
              f"{file_size:,} ({reader.bytes_read / file_size:.4%})")
        assert first.data == expected_record(1)
        assert reader.bytes_read < file_size // 100

    print("== full streaming decode (tracemalloc running) ==")
    tracemalloc.start()
    seen = 0
    first_data = last_data = None
    with open(data, "rb") as handle:
        for record in iter_records(layout, handle):
            seen += 1
            if first_data is None:
                first_data = record.data
            last_data = record.data
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"  records decoded: {seen:,}")
    print(f"  peak python memory during decode: {peak / 1024:.0f} KiB "
          f"for a {file_size / 1024:.0f} KiB file")
    assert seen == count
    assert first_data == expected_record(1), "first record mismatch"
    assert last_data == expected_record(count), "last record mismatch"
    print("  first and last record contents match the generator parameters")

    # Same decode against a file 5x larger must not need more memory.
    bigger = os.path.join(work, "orders5.dat")
    generate(bigger, count * 5)
    tracemalloc.start()
    with open(bigger, "rb") as handle:
        for _ in iter_records(layout, handle):
            pass
    _, peak5 = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"  peak python memory for 5x records ({count * 5:,}): "
          f"{peak5 / 1024:.0f} KiB (file {os.path.getsize(bigger) / 1024:.0f} KiB)")
    assert peak5 < peak * 4 + (1 << 20), (peak, peak5)

    print("\nstream demo OK: constant-memory streaming decode verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
