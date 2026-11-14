#!/usr/bin/env python3
"""Scale demonstration: many segments, real lookup statistics, bounded scans.

Usage:  PYTHONPATH=src python3 demo/staging_scale_demo.py [DIR] [--keys N]
            [--budget BYTES] [--block-size BYTES]

Builds a store with many on-disk segments, then shows:
  * point lookups read only the candidate segment's candidate block
    (segment/block read statistics per lookup, not whole files);
  * a full scan merges all segments with a small bounded buffer
    (peak buffered bytes vs total file bytes, plus tracemalloc);
  * compaction folds everything into one segment and reclaims space.
"""

import argparse
import os
import shutil
import sys
import tempfile
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from distagg.staging import StagingStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dir", nargs="?", default=None)
    parser.add_argument("--keys", type=int, default=6000)
    parser.add_argument("--budget", type=int, default=128 * 1024)
    parser.add_argument("--block-size", type=int, default=4096)
    args = parser.parse_args()
    directory = args.dir or tempfile.mkdtemp(prefix="staging-scale-")
    if os.path.exists(directory):
        shutil.rmtree(directory)

    value = b"v" * 120
    store = StagingStore.open(
        directory, memtable_budget=args.budget, block_size=args.block_size
    )
    print("store directory:", directory)
    print("writing %d keys (sorted, %d-byte values), memtable budget %d bytes..."
          % (args.keys, len(value), args.budget))
    for i in range(args.keys):
        store.put(b"key%06d" % i, value)
    store.flush()
    stats = store.stats()
    print("-> %d segments, %d records, %d bytes on disk"
          % (stats["segment_count"], stats["total_segment_records"],
             stats["total_segment_bytes"]))

    print("\npoint lookups (sparse index -> one candidate block per segment):")
    for probe in (b"key000000", b"key%06d" % (args.keys // 2), b"key%06d" % (args.keys - 1),
                  b"key%06d" % (args.keys + 1)):
        found, lookup = store.get_with_stats(probe)
        print("  get(%s): found=%-5s blocks=%d bytes=%d segments_considered=%d "
              "skipped_by_range=%d skipped_by_commit=%d"
              % (probe.decode(), found is not None, lookup["blocks_read"],
                 lookup["bytes_read"], lookup["segments_considered"],
                 lookup["segments_skipped_key_range"], lookup["segments_skipped_commit"]))
    total = stats["total_segment_bytes"]
    print("  (whole store is %d bytes; each lookup read only a few blocks)" % total)

    print("\nfull scan with bounded buffers:")
    tracemalloc.start()
    with store.scan() as iterator:
        count = sum(1 for _ in iterator)
        peak_buffer = iterator.peak_buffered_bytes
        scanned = iterator.records_scanned
    _, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print("  %d entries, %d records merged" % (count, scanned))
    print("  peak bytes buffered by the iterator: %d (%.1f%% of the %d bytes on disk)"
          % (peak_buffer, 100.0 * peak_buffer / total, total))
    print("  tracemalloc peak during the scan:    %d bytes" % peak_py)

    print("\ncompacting...")
    result = store.compact()
    print("  segments %d -> %d, records %d -> %d, bytes %d -> %d"
          % (result["segments_before"], result["segments_after"],
             result["records_before"], result["records_after"],
             result["bytes_before"], result["bytes_after"]))
    found, lookup = store.get_with_stats(b"key%06d" % (args.keys // 2))
    print("  lookup after compact: blocks=%d bytes=%d segments_considered=%d"
          % (lookup["blocks_read"], lookup["bytes_read"], lookup["segments_considered"]))
    store.close()


if __name__ == "__main__":
    main()
