#!/usr/bin/env python3
"""Short-key demonstration of the staging store semantics.

Usage:  PYTHONPATH=src python3 demo/staging_demo.py [DIR]

Covers, with real output at every step:
  1. how the empty key and keys containing zero bytes sort;
  2. what a same-batch put/delete/put sequence leaves behind;
  3. an old snapshot vs a fresh scan after the same key is rewritten
     across several flushes;
  4. a scan paused in the middle while a compaction runs — the iterator
     keeps its original view;
  5. version reclamation: after the snapshot closes, another compact
     really removes old versions (record counts and file bytes).
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from distagg.staging import StagingStore


def show(title):
    print("\n=== %s ===" % title)


def hx(key):
    return "0x" + key.hex() if key else "(empty key)"


def main():
    directory = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="staging-demo-")
    if os.path.exists(directory):
        shutil.rmtree(directory)
    store = StagingStore.open(directory, memtable_budget=1024, block_size=512)
    print("store directory:", directory)

    show("1. ordering of the empty key and keys with zero bytes")
    keys = [b"a", b"", b"\x00", b"a\x00", b"\x00a", b"\xff", b"a ", b"\x00\x00"]
    for index, key in enumerate(keys):
        store.put(key, b"v%d" % index)
    print("inserted (shuffled):", [hx(k) for k in keys])
    print("scan order:         ", [hx(k) for k, _ in store.scan()])

    show("2. one batch: put k=A, delete k, put k=B")
    commit = store.write_batch(
        [("put", b"k", b"A"), ("delete", b"k"), ("put", b"k", b"B")]
    )
    print("commit:", commit, "-> get(k):", store.get(b"k"))
    store.write_batch([("put", b"m", b"X"), ("delete", b"m")])
    print("put m=X, delete m in one batch -> get(m):", store.get(b"m"))

    show("3. snapshot pinned before rewrites that cross several flushes")
    store.put(b"hot", b"gen-1")
    store.flush()
    snap = store.snapshot()
    print("snapshot taken at commit", snap.commit_number)
    for generation in range(2, 6):
        store.put(b"hot", b"gen-%d" % generation)
        store.flush()
    print("rewrote hot=gen-2..gen-5, flushing after each write")
    print("segments now:", store.stats()["segment_count"])
    print("snapshot.get(hot):", snap.get(b"hot"), "   store.get(hot):", store.get(b"hot"))
    fresh = list(store.scan(lower=b"hot", upper=b"hot\x00"))
    old = list(snap.scan(lower=b"hot", upper=b"hot\x00"))
    print("fresh scan:", fresh, "  snapshot scan:", old)

    show("4. scan paused mid-way, then compact: the iterator keeps its view")
    before_compact = list(store.scan())
    iterator = store.scan()
    head = [next(iterator), next(iterator), next(iterator)]
    print("read 3 entries, then compacting while the iterator is parked...")
    result = store.compact()
    print("compact:", {k: result[k] for k in ("segments_before", "segments_after",
                                            "records_before", "records_after")})
    rest = list(iterator)
    print("iterator result identical to pre-compact scan:",
          head + rest == before_compact)
    print("obsolete segment files still pinned during iteration were:",
          result["pending_delete_files"] or "(deleted once unpinned)")

    show("5. close the snapshot, compact again: old versions really go away")
    stats = store.stats()
    print("before: segments=%d records=%d bytes=%d protected=%s" % (
        stats["segment_count"], stats["total_segment_records"],
        stats["total_segment_bytes"], stats["protected_commits"]))
    snap.close()
    result = store.compact()
    stats = store.stats()
    print("after:  segments=%d records=%d bytes=%d" % (
        stats["segment_count"], stats["total_segment_records"],
        stats["total_segment_bytes"]))
    print("compact reclaimed: %d -> %d records, deleted files: %s" % (
        result["records_before"], result["records_after"], result["deleted_files"]))
    print("hot is still:", store.get(b"hot"), "(newest version survives)")

    store.close()
    print("\nreopening the store to prove durability...")
    store = StagingStore.open(directory)
    print("commit:", store.commit_number, " hot:", store.get(b"hot"),
        " entries:", sum(1 for _ in store.scan()))
    store.close()


if __name__ == "__main__":
    main()
