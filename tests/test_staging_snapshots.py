"""Snapshot and iterator isolation: pinned views across writes, flush, compact."""

import os
import shutil
import tempfile
import unittest

from distagg.staging import ClosedError, StagingStore


class StagingSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="staging-snap-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def open(self, **kwargs):
        kwargs.setdefault("memtable_budget", 4096)
        kwargs.setdefault("block_size", 256)
        return StagingStore.open(self.dir, **kwargs)

    def test_snapshot_isolation_across_writes(self):
        store = self.open()
        store.put(b"k", b"v1")
        snap = store.snapshot()
        self.assertEqual(snap.commit_number, 1)
        store.put(b"k", b"v2")
        store.delete(b"other")
        store.put(b"new", b"appears-later")
        self.assertEqual(snap.get(b"k"), b"v1")
        self.assertIsNone(snap.get(b"new"))
        self.assertEqual(store.get(b"k"), b"v2")
        self.assertEqual(store.get(b"new"), b"appears-later")
        self.assertEqual(list(snap.scan()), [(b"k", b"v1")])
        snap.close()
        store.close()

    def test_snapshot_survives_flush_and_compact(self):
        store = self.open()
        store.put(b"k", b"v1")
        snap = store.snapshot()
        # Rewrite the same key many times, crossing several flushes.
        for i in range(2, 9):
            store.put(b"k", b"v%d" % i)
            store.flush()
        store.compact()
        self.assertEqual(snap.get(b"k"), b"v1")
        self.assertEqual(store.get(b"k"), b"v8")
        self.assertEqual(list(snap.scan()), [(b"k", b"v1")])
        snap.close()
        store.close()

    def test_snapshot_delete_visibility(self):
        store = self.open()
        store.put(b"k", b"kept-for-snap")
        snap = store.snapshot()
        store.delete(b"k")
        store.flush()
        store.compact()
        self.assertEqual(snap.get(b"k"), b"kept-for-snap")
        self.assertIsNone(store.get(b"k"))
        snap.close()
        store.close()

    def test_iterator_pins_view_without_snapshot(self):
        store = self.open()
        for i in range(10):
            store.put(b"k%02d" % i, b"v%d" % i)
        store.flush()
        iterator = store.scan()
        first = next(iterator)
        self.assertEqual(first, (b"k00", b"v0"))
        # Writes + compact while the iterator is paused mid-scan.
        for i in range(10):
            store.put(b"k%02d" % i, b"CHANGED%d" % i)
        store.flush()
        store.compact()
        rest = list(iterator)
        self.assertEqual(
            [first] + rest,
            [(b"k%02d" % i, b"v%d" % i) for i in range(10)],
        )
        self.assertTrue(iterator.closed)  # exhausted iterators release their pins
        store.close()

    def test_iterator_early_close_releases_segment_files(self):
        store = self.open()
        for i in range(10):
            store.put(b"k%02d" % i, b"v%d" % i)
        store.flush()
        segment_files = [
            name for name in os.listdir(self.dir) if name.endswith(".sst")
        ]
        self.assertEqual(len(segment_files), 1)
        iterator = store.scan()
        next(iterator)
        store.compact()  # old segment is obsolete but pinned by the iterator
        still_there = [n for n in os.listdir(self.dir) if n.endswith(".sst")]
        self.assertEqual(len(still_there), 2)  # old (pinned) + new
        iterator.close()
        remaining = [n for n in os.listdir(self.dir) if n.endswith(".sst")]
        self.assertEqual(len(remaining), 1)  # pin released -> old file deleted
        store.close()

    def test_snapshot_pins_segment_files_until_closed(self):
        store = self.open()
        store.put(b"k", b"v1")
        store.flush()
        snap = store.snapshot()
        store.put(b"k", b"v2")
        store.flush()
        store.compact()
        files_while_pinned = [n for n in os.listdir(self.dir) if n.endswith(".sst")]
        self.assertEqual(len(files_while_pinned), 2)
        self.assertEqual(snap.get(b"k"), b"v1")
        snap.close()
        files_after = [n for n in os.listdir(self.dir) if n.endswith(".sst")]
        self.assertEqual(len(files_after), 1)
        store.close()

    def test_compaction_keeps_versions_for_live_snapshots(self):
        store = self.open()
        store.put(b"k", b"v1")
        store.flush()
        snap = store.snapshot()
        store.put(b"k", b"v2")
        store.flush()
        store.delete(b"k")
        store.flush()
        result = store.compact()
        # The compacted segment keeps v1 (visible to the snapshot at commit 1)
        # and the tombstone (visible to current readers, and still hiding v1).
        # v2 is visible to no protected reader, so it is reclaimed already.
        self.assertEqual(result["records_after"], 2)
        self.assertEqual(snap.get(b"k"), b"v1")
        snap.close()
        result = store.compact()
        # With no snapshot left, the key is simply gone: overwritten values
        # and the useless tombstone are reclaimed.
        self.assertEqual(result["records_after"], 0)
        self.assertIsNone(store.get(b"k"))
        store.close()

    def test_tombstone_kept_while_snapshot_needs_older_value(self):
        store = self.open()
        store.put(b"victim", b"old-value")
        store.flush()
        snap = store.snapshot()
        store.delete(b"victim")
        store.flush()
        result = store.compact()
        # old value (for the snapshot) + tombstone (for current readers).
        self.assertEqual(result["records_after"], 2)
        self.assertEqual(snap.get(b"victim"), b"old-value")
        self.assertIsNone(store.get(b"victim"))
        snap.close()
        result = store.compact()
        self.assertEqual(result["records_after"], 0)
        # And the value did not resurrect anywhere.
        self.assertIsNone(store.get(b"victim"))
        self.assertEqual(list(store.scan()), [])
        store.close()

    def test_snapshot_scan_iterator_independent_of_snapshot_close(self):
        store = self.open()
        for i in range(5):
            store.put(b"k%d" % i, b"v%d" % i)
        snap = store.snapshot()
        iterator = snap.scan()
        self.assertEqual(next(iterator), (b"k0", b"v0"))
        snap.close()  # the iterator pinned its own view
        store.put(b"k0", b"rewritten")
        store.flush()
        store.compact()
        self.assertEqual(
            list(iterator), [(b"k%d" % i, b"v%d" % i) for i in range(1, 5)]
        )
        store.close()

    def test_snapshot_taken_mid_flush_sees_consistent_state(self):
        """A snapshot created while a flush is between segment write and
        segment-list insertion must still see every committed batch."""
        store = self.open()
        for i in range(6):
            store.put(b"k%d" % i, b"v%d" % i)
        observations = {}

        def pause(point, info):
            if point != "flush_segment_written":
                return
            # The frozen memtable is being written out; the new segment is
            # not yet visible.  Views captured now must still be complete.
            snap = store.snapshot()
            observations["snap"] = (snap.get(b"k0"), snap.get(b"k5"), len(list(snap.scan())))
            observations["get"] = store.get(b"k3")
            observations["scan_len"] = len(list(store.scan()))
            snap.close()

        store._pause = pause
        store.flush()
        self.assertEqual(observations["snap"], (b"v0", b"v5", 6))
        self.assertEqual(observations["get"], b"v3")
        self.assertEqual(observations["scan_len"], 6)
        self.assertEqual(len(list(store.scan())), 6)
        store.close()

    def test_failed_flush_rolls_back_without_losing_batches(self):
        """If the segment write fails, the store stays fully consistent."""
        from unittest import mock

        store = self.open()
        for i in range(5):
            store.put(b"k%d" % i, b"v%d" % i)
        from distagg.staging import store as store_module

        with mock.patch.object(
            store_module.SegmentWriter, "finish", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                store.flush()
        # Everything is still readable from the (restored) memtable + WAL.
        for i in range(5):
            self.assertEqual(store.get(b"k%d" % i), b"v%d" % i)
        self.assertEqual(store.commit_number, 5)
        # And a later flush + reopen works normally.
        store.flush()
        store.close()
        reopened = self.open()
        for i in range(5):
            self.assertEqual(reopened.get(b"k%d" % i), b"v%d" % i)
        reopened.close()

    def test_store_close_then_snapshot_use_raises(self):
        store = self.open()
        store.put(b"k", b"v")
        snap = store.snapshot()
        store.close()
        with self.assertRaises(ClosedError):
            snap.get(b"k")
        snap.close()  # closing afterwards is still fine

    def test_one_writer_many_reader_threads(self):
        """A single writer thread plus concurrent readers/snapshot users."""
        import threading

        store = self.open()
        for i in range(50):
            store.put(b"k%02d" % i, b"base")
        store.flush()
        errors = []
        stop = threading.Event()

        def writer():
            try:
                for generation in range(60):
                    store.write_batch(
                        [("put", b"k%02d" % (generation % 50), b"gen%d" % generation)]
                    )
                    if generation % 10 == 9:
                        store.flush()
                    if generation == 30:
                        store.compact()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            finally:
                stop.set()

        def reader():
            try:
                while not stop.is_set():
                    snapshot = store.snapshot()
                    value = snapshot.get(b"k10")
                    self.assertIn(value, (b"base",) + tuple(b"gen%d" % g for g in range(60)))
                    entries = list(snapshot.scan())
                    self.assertEqual(len(entries), 50)
                    self.assertEqual(entries, sorted(entries))
                    snapshot.close()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(list(store.scan())), 50)
        store.close()


if __name__ == "__main__":
    unittest.main()
