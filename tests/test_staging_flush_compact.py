"""Memtable budget, explicit flush/compact, and on-disk reclamation evidence."""

import os
import shutil
import tempfile
import unittest

from distagg.staging import StagingStore


def segment_files(path):
    return [n for n in os.listdir(path) if n.endswith(".sst")]


class StagingFlushCompactTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="staging-flush-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def open(self, **kwargs):
        kwargs.setdefault("memtable_budget", 1024)
        kwargs.setdefault("block_size", 256)
        return StagingStore.open(self.dir, **kwargs)

    def test_budget_triggers_automatic_flush(self):
        store = self.open()
        for i in range(40):
            store.put(b"key%03d" % i, b"v" * 40)
        stats = store.stats()
        self.assertGreaterEqual(stats["segment_count"], 2)
        # After each automatic flush the buffer is back under the budget.
        self.assertLess(stats["memtable"]["bytes"], stats["memtable"]["budget"])
        for i in range(40):
            self.assertEqual(store.get(b"key%03d" % i), b"v" * 40)
        store.close()

    def test_oversized_batch_is_flushed_right_after_completion(self):
        store = self.open()
        big = [("put", b"big%03d" % i, b"x" * 100) for i in range(50)]
        commit = store.write_batch(big)  # one batch far beyond the 1 KiB budget
        self.assertEqual(store.commit_number, commit)
        stats = store.stats()
        # The batch was applied atomically, then the buffer was drained.
        self.assertLess(stats["memtable"]["bytes"], stats["memtable"]["budget"])
        self.assertGreaterEqual(stats["segment_count"], 1)
        self.assertEqual(store.get(b"big049"), b"x" * 100)
        store.close()

    def test_explicit_flush_and_wal_rotation(self):
        store = self.open()
        store.put(b"a", b"1")
        result = store.flush()
        self.assertIsNotNone(result)
        self.assertEqual(len(segment_files(self.dir)), 1)
        wals = [n for n in os.listdir(self.dir) if n.endswith(".log")]
        self.assertEqual(len(wals), 1)  # the retired WAL is gone
        self.assertIsNone(store.flush())  # empty memtable: no-op
        self.assertEqual(len(segment_files(self.dir)), 1)
        store.put(b"a", b"2")
        store.flush()
        self.assertEqual(len(segment_files(self.dir)), 2)
        self.assertEqual(store.get(b"a"), b"2")
        store.close()

    def test_compact_merges_and_reclaims_with_real_numbers(self):
        store = self.open()
        for generation in range(4):
            for i in range(20):
                store.put(b"key%02d" % i, b"gen%d" % generation)
            store.flush()
        before = store.stats()
        self.assertEqual(before["segment_count"], 4)
        self.assertEqual(before["total_segment_records"], 80)
        result = store.compact()
        self.assertEqual(result["segments_before"], 4)
        self.assertEqual(result["segments_after"], 1)
        self.assertEqual(result["records_before"], 80)
        self.assertEqual(result["records_after"], 20)  # overwritten versions gone
        self.assertLess(result["bytes_after"], result["bytes_before"])
        after = store.stats()
        self.assertEqual(after["segment_count"], 1)
        self.assertEqual(after["total_segment_records"], 20)
        self.assertEqual(len(segment_files(self.dir)), 1)
        for i in range(20):
            self.assertEqual(store.get(b"key%02d" % i), b"gen3")
        store.close()

    def test_compact_with_no_segments_is_a_noop(self):
        store = self.open()
        store.put(b"a", b"1")
        result = store.compact()
        self.assertEqual(result["segments_before"], 0)
        self.assertEqual(result["segments_after"], 0)
        self.assertEqual(store.get(b"a"), b"1")
        store.close()

    def test_compact_removes_all_tombstones_when_unobserved(self):
        store = self.open()
        for i in range(10):
            store.put(b"k%d" % i, b"v")
        store.flush()
        for i in range(10):
            store.delete(b"k%d" % i)
        store.flush()
        result = store.compact()
        self.assertEqual(result["records_after"], 0)
        self.assertEqual(result["segments_after"], 0)
        self.assertEqual(segment_files(self.dir), [])
        self.assertEqual(list(store.scan()), [])
        store.close()

    def test_point_lookup_reads_only_candidate_blocks(self):
        # Sorted inserts give disjoint segment key ranges, so the sparse
        # index can rule segments out without reading any block from them.
        store = self.open(memtable_budget=2048, block_size=512)
        for i in range(200):
            store.put(b"key%04d" % i, b"v" * 30)
        store.flush()
        stats = store.stats()
        self.assertGreaterEqual(stats["segment_count"], 3)
        value, lookup = store.get_with_stats(b"key0100")
        self.assertEqual(value, b"v" * 30)
        self.assertEqual(lookup["blocks_read"], 1)  # one block, not whole files
        self.assertGreaterEqual(lookup["segments_skipped_key_range"], 1)
        total_bytes = stats["total_segment_bytes"]
        self.assertLess(lookup["bytes_read"], total_bytes // 2)
        # A missing key reads at most one candidate block as well.
        value, lookup = store.get_with_stats(b"key0100x")
        self.assertIsNone(value)
        self.assertLessEqual(lookup["blocks_read"], 1)
        store.close()

    def test_scan_uses_bounded_buffer(self):
        store = self.open(memtable_budget=4096, block_size=512)
        for i in range(300):
            store.put(b"key%04d" % i, b"v" * 50)
        store.flush()
        total = store.stats()["total_segment_bytes"]
        with store.scan() as iterator:
            count = sum(1 for _ in iterator)
            peak = iterator.peak_buffered_bytes
        self.assertEqual(count, 300)
        # One block per segment plus the memtable copy — far below the
        # whole data set, which never gets loaded into memory.
        self.assertLess(peak, total // 2)
        store.close()

    def test_reopen_does_not_reapply_records(self):
        store = self.open()
        for i in range(30):
            store.put(b"k%02d" % i, b"v%d" % i)
        store.flush()
        store.put(b"extra", b"1")  # stays in the WAL
        records = store.stats()["total_segment_records"]
        commit = store.commit_number
        store.close()
        for _ in range(3):  # open and close repeatedly
            reopened = self.open()
            self.assertEqual(reopened.commit_number, commit)
            self.assertEqual(reopened.stats()["total_segment_records"], records)
            self.assertEqual(reopened.get(b"extra"), b"1")
            self.assertEqual(reopened.get(b"k05"), b"v5")
            reopened.close()


if __name__ == "__main__":
    unittest.main()
