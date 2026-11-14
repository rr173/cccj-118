"""Basic semantics of the staging store: keys, batches, scans, persistence."""

import os
import shutil
import tempfile
import unittest

from distagg.staging import (
    Batch,
    ClosedError,
    InvalidOperation,
    StagingStore,
)


class StagingBasicTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="staging-basic-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def open(self, **kwargs):
        kwargs.setdefault("memtable_budget", 4096)
        kwargs.setdefault("block_size", 256)
        return StagingStore.open(self.dir, **kwargs)

    def test_put_get_delete_roundtrip(self):
        store = self.open()
        self.assertEqual(store.put(b"alpha", b"1"), 1)
        self.assertEqual(store.put(b"beta", b"2"), 2)
        self.assertEqual(store.get(b"alpha"), b"1")
        self.assertEqual(store.get(b"beta"), b"2")
        self.assertIsNone(store.get(b"gamma"))
        self.assertEqual(store.delete(b"alpha"), 3)
        self.assertIsNone(store.get(b"alpha"))
        self.assertEqual(store.get(b"beta"), b"2")
        store.close()

    def test_empty_key_and_empty_value(self):
        store = self.open()
        store.put(b"", b"empty-key")
        store.put(b"empty-value", b"")
        self.assertEqual(store.get(b""), b"empty-key")
        # An empty value is stored, and is distinct from "not found".
        self.assertEqual(store.get(b"empty-value"), b"")
        self.assertIsNone(store.get(b"absent"))
        store.delete(b"")
        self.assertIsNone(store.get(b""))
        store.close()

    def test_key_ordering_unsigned_bytes(self):
        store = self.open()
        keys = [b"a", b"", b"\x00", b"a\x00", b"\x00a", b"\xff", b"\xff\x00", b"a "]
        for index, key in enumerate(keys):  # deliberately unsorted
            store.put(key, b"v%d" % index)
        ordered = [key for key, _ in store.scan()]
        self.assertEqual(ordered, sorted(keys))
        # and the exact expected unsigned-byte lexicographic order:
        self.assertEqual(
            ordered,
            [b"", b"\x00", b"\x00a", b"a", b"a\x00", b"a ", b"\xff", b"\xff\x00"],
        )
        store.close()

    def test_batch_last_operation_wins(self):
        store = self.open()
        commit = store.write_batch(
            [("put", b"k", b"first"), ("delete", b"k"), ("put", b"k", b"final")]
        )
        self.assertEqual(store.get(b"k"), b"final")
        self.assertEqual(store.commit_number, commit)
        store.write_batch([("put", b"x", b"1"), ("delete", b"x")])
        self.assertIsNone(store.get(b"x"))
        store.write_batch([("delete", b"y"), ("put", b"y", b"back")])
        self.assertEqual(store.get(b"y"), b"back")
        store.close()

    def test_batch_builder_and_delete_missing_key(self):
        store = self.open()
        batch = Batch().put(b"a", b"1").delete(b"ghost").put(b"b", b"2")
        commit = store.write_batch(batch)
        self.assertEqual(commit, 1)
        self.assertEqual(store.get(b"a"), b"1")
        self.assertIsNone(store.get(b"ghost"))
        store.delete(b"ghost")  # deleting a missing key is legal
        self.assertEqual(store.commit_number, 2)
        store.close()

    def test_scan_bounds_left_closed_right_open(self):
        store = self.open()
        for key in (b"a", b"b", b"c", b"d"):
            store.put(key, key.upper())
        self.assertEqual([k for k, _ in store.scan(lower=b"b", upper=b"d")], [b"b", b"c"])
        self.assertEqual([k for k, _ in store.scan(upper=b"b")], [b"a"])
        self.assertEqual([k for k, _ in store.scan(lower=b"c")], [b"c", b"d"])
        self.assertEqual([k for k, _ in store.scan(lower=b"x")], [])
        self.assertEqual([k for k, _ in store.scan(lower=b"a", upper=b"a")], [])
        with self.assertRaises(InvalidOperation):
            store.scan(lower=b"d", upper=b"a")
        store.close()

    def test_scan_gives_each_key_once_with_newest_value(self):
        store = self.open()
        store.put(b"k", b"old")
        store.flush()
        store.put(b"k", b"new")
        store.flush()
        store.put(b"k", b"newest")
        seen = [pair for pair in store.scan()]
        self.assertEqual(seen, [(b"k", b"newest")])
        store.close()

    def test_commit_numbers_increase(self):
        store = self.open()
        commits = [
            store.put(b"a", b"1"),
            store.delete(b"a"),
            store.write_batch([]),
            store.put(b"b", b"2"),
        ]
        self.assertEqual(commits, [1, 2, 3, 4])
        self.assertEqual(store.commit_number, 4)
        store.close()

    def test_persistence_across_reopen(self):
        store = self.open()
        store.put(b"persist", b"yes")
        store.put(b"gone", b"soon")
        store.delete(b"gone")
        for i in range(30):  # force several flushes with the tiny budget
            store.put(b"k%02d" % i, b"value-%d" % i)
        store.close()
        reopened = self.open()
        self.assertEqual(reopened.get(b"persist"), b"yes")
        self.assertIsNone(reopened.get(b"gone"))
        self.assertEqual(reopened.get(b"k07"), b"value-7")
        self.assertEqual(len(list(reopened.scan())), 30 + 1)
        reopened.close()

    def test_closed_objects_raise(self):
        store = self.open()
        store.put(b"k", b"v")
        snapshot = store.snapshot()
        iterator = store.scan()
        next(iterator)
        iterator.close()
        snapshot.close()
        store.close()
        with self.assertRaises(ClosedError):
            snapshot.get(b"k")
        with self.assertRaises(ClosedError):
            snapshot.scan()
        with self.assertRaises(ClosedError):
            next(iterator)
        with self.assertRaises(ClosedError):
            store.get(b"k")
        with self.assertRaises(ClosedError):
            store.put(b"k", b"v2")

    def test_type_validation(self):
        store = self.open()
        with self.assertRaises(InvalidOperation):
            store.put("not-bytes", b"v")
        with self.assertRaises(InvalidOperation):
            store.put(b"k", 123)
        with self.assertRaises(InvalidOperation):
            store.write_batch([("update", b"k", b"v")])
        with self.assertRaises(InvalidOperation):
            store.write_batch([("put", b"k")])
        store.close()

    def test_second_writer_refused_in_process(self):
        from distagg.staging import StoreLockedError

        store = self.open()
        try:
            with self.assertRaises(StoreLockedError) as ctx:
                self.open()
            self.assertIn("already open for writing", str(ctx.exception))
        finally:
            store.close()
        # After close, the directory can be opened again.
        again = self.open()
        again.close()

    def test_read_only_open_and_write_refusal(self):
        from distagg.staging import ReadOnlyError

        store = self.open()
        store.put(b"k", b"v")
        store.close()
        reader = StagingStore.open(self.dir, read_only=True)
        self.assertEqual(reader.get(b"k"), b"v")
        with self.assertRaises(ReadOnlyError):
            reader.put(b"x", b"y")
        with self.assertRaises(ReadOnlyError):
            reader.flush()
        with self.assertRaises(ReadOnlyError):
            reader.compact()
        reader.close()


if __name__ == "__main__":
    unittest.main()
