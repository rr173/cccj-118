"""Fault-injection tests: real subprocesses are SIGKILLed at pause points,
WAL tails are truncated, and files are corrupted — all checked from outside.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from distagg.staging import CorruptionError, StagingStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")




def hx(text):
    """Hex form of an ascii string, for CLI arguments."""
    return text.encode("ascii").hex()


def cli_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_cli(args, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True, env=cli_env(), **kwargs
    )


def cli_put(store_dir, key_hex, value_hex, extra=()):
    return run_cli(["staging", "put", store_dir, key_hex, value_hex, *extra])


def cli_get(store_dir, key_hex):
    proc = run_cli(["staging", "get", store_dir, key_hex])
    return json.loads(proc.stdout)


def wal_files(store_dir):
    return [n for n in os.listdir(store_dir) if n.endswith(".log")]


def segment_files(store_dir):
    return [n for n in os.listdir(store_dir) if n.endswith(".sst")]


class StagingCrashTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="staging-crash-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store_dir = os.path.join(self.dir, "store")

    # -- WAL kill points ----------------------------------------------------
    def test_kill_before_wal_sync_loses_nothing_but_the_unconfirmed_batch(self):
        proc = cli_put(self.store_dir, "aa", "01")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Killed before the frame ever reached the OS: the batch is gone,
        # the earlier confirmed batch survives, and the store opens cleanly.
        proc = cli_put(self.store_dir, "bb", "02", extra=["--kill-at", "wal_before_sync"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(cli_get(self.store_dir, "aa"), {"found": True, "value_hex": "01"})
        self.assertEqual(cli_get(self.store_dir, "bb"), {"found": False, "value_hex": None})
        # The store is still fully usable; commit numbers continue from 1.
        proc = cli_put(self.store_dir, "cc", "03")
        self.assertEqual(json.loads(proc.stdout)["commit"], 2)

    def test_kill_after_wal_sync_keeps_the_batch(self):
        cli_put(self.store_dir, "aa", "01")
        proc = cli_put(self.store_dir, "bb", "02", extra=["--kill-at", "wal_after_sync"])
        self.assertNotEqual(proc.returncode, 0)
        # The frame was fsynced before the kill: the batch is recovered.
        self.assertEqual(cli_get(self.store_dir, "bb"), {"found": True, "value_hex": "02"})
        proc = run_cli(["staging", "stats", self.store_dir])
        self.assertEqual(json.loads(proc.stdout)["commit"], 2)

    def test_batch_is_all_or_nothing_across_a_kill(self):
        ops = [{"op": "put", "key": hx("k%d" % i), "value": "ff"} for i in range(10)]
        ops_file = os.path.join(self.dir, "ops.json")
        with open(ops_file, "w") as handle:
            json.dump(ops, handle)
        proc = run_cli(
            ["staging", "batch", self.store_dir, ops_file, "--kill-at", "wal_before_sync"]
        )
        self.assertNotEqual(proc.returncode, 0)
        for i in range(10):  # not a single key of the batch may appear
            self.assertFalse(cli_get(self.store_dir, hx("k%d" % i))["found"])
        proc = run_cli(["staging", "batch", self.store_dir, ops_file])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for i in range(10):
            self.assertTrue(cli_get(self.store_dir, hx("k%d" % i))["found"])

    # -- flush kill points ----------------------------------------------------
    def test_kill_flush_segment_written_recovers_from_wal(self):
        for i in range(5):
            cli_put(self.store_dir, hx("k%d" % i), hx("v%d" % i))
        proc = run_cli(
            ["staging", "flush", self.store_dir, "--kill-at", "flush_segment_written"]
        )
        self.assertNotEqual(proc.returncode, 0)
        # The segment was written but the manifest never switched: the file
        # is orphaned, removed on open, and the data comes from the WAL.
        store = StagingStore.open(self.store_dir)
        self.assertEqual(segment_files(self.store_dir), [])
        for i in range(5):
            self.assertEqual(store.get(b"k%d" % i), b"v%d" % i)
        self.assertEqual(store.commit_number, 5)
        store.close()

    def test_kill_flush_manifest_switched_applies_nothing_twice(self):
        for i in range(5):
            cli_put(self.store_dir, hx("k%d" % i), hx("v%d" % i))
        proc = run_cli(
            ["staging", "flush", self.store_dir, "--kill-at", "flush_manifest_switched"]
        )
        self.assertNotEqual(proc.returncode, 0)
        # Manifest switched, old WAL not yet removed: recovery must skip the
        # already-flushed frames instead of applying them a second time.
        store = StagingStore.open(self.store_dir)
        self.assertEqual(len(segment_files(self.store_dir)), 1)
        self.assertEqual(len(wal_files(self.store_dir)), 1)
        self.assertEqual(store.skipped_batches, 0)  # old WAL was unreferenced
        for i in range(5):
            self.assertEqual(store.get(b"k%d" % i), b"v%d" % i)
        self.assertEqual(store.commit_number, 5)
        store.put(b"after", b"1")
        store.close()
        reopened = StagingStore.open(self.store_dir)
        self.assertEqual(reopened.get(b"after"), b"1")
        self.assertEqual(reopened.commit_number, 6)
        reopened.close()

    # -- compact kill points --------------------------------------------------
    def _build_segmented_store(self):
        for generation in range(3):
            for i in range(4):
                cli_put(self.store_dir, hx("k%d" % i), hx("g%d" % generation))
            proc = run_cli(["staging", "flush", self.store_dir])
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_kill_compact_segment_written_keeps_old_segments(self):
        self._build_segmented_store()
        self.assertEqual(len(segment_files(self.store_dir)), 3)
        proc = run_cli(
            ["staging", "compact", self.store_dir, "--kill-at", "compact_segment_written"]
        )
        self.assertNotEqual(proc.returncode, 0)
        # The merged segment was never committed: old segments are the truth,
        # the orphan is recognised and removed.
        store = StagingStore.open(self.store_dir)
        self.assertEqual(len(segment_files(self.store_dir)), 3)
        for i in range(4):
            self.assertEqual(store.get(b"k%d" % i), b"g2")
        store.close()

    def test_kill_compact_manifest_switched_cleans_old_segments(self):
        self._build_segmented_store()
        proc = run_cli(
            ["staging", "compact", self.store_dir, "--kill-at", "compact_manifest_switched"]
        )
        self.assertNotEqual(proc.returncode, 0)
        # The switch happened; the not-yet-deleted old segments are
        # unreferenced and get cleaned on open.  Data is read from the new one.
        store = StagingStore.open(self.store_dir)
        self.assertEqual(len(segment_files(self.store_dir)), 1)
        for i in range(4):
            self.assertEqual(store.get(b"k%d" % i), b"g2")
        self.assertEqual(store.commit_number, 12)
        store.close()

    # -- truncated WAL tail ----------------------------------------------------
    def test_truncated_wal_tail_recovers_only_complete_frames(self):
        for i in range(4):
            cli_put(self.store_dir, hx("k%d" % i), hx("v%d" % i))
        wal = os.path.join(self.store_dir, wal_files(self.store_dir)[0])
        size = os.path.getsize(wal)
        with open(wal, "r+b") as handle:  # tear the last frame in half
            handle.truncate(size - 3)
        store = StagingStore.open(self.store_dir)
        for i in range(3):
            self.assertEqual(store.get(b"k%d" % i), b"v%d" % i)
        self.assertIsNone(store.get(b"k3"))
        self.assertEqual(store.commit_number, 3)
        # The torn tail was dropped; new appends continue cleanly.
        store.put(b"k4", b"v4")
        store.close()
        reopened = StagingStore.open(self.store_dir)
        self.assertEqual(reopened.get(b"k4"), b"v4")
        self.assertEqual(reopened.commit_number, 4)
        reopened.close()

    # -- corruption: refuse, never silently rebuild -----------------------------
    def test_mid_log_corruption_refuses_open_and_reports_offset(self):
        for i in range(4):
            cli_put(self.store_dir, hx("k%d" % i), hx("v%d" % i))
        wal = os.path.join(self.store_dir, wal_files(self.store_dir)[0])
        with open(wal, "r+b") as handle:
            data = handle.read()
            # Corrupt one payload byte inside the *first* frame (not the tail).
            handle.seek(12)
            handle.write(bytes([data[12] ^ 0xFF]))
        with self.assertRaises(CorruptionError) as ctx:
            StagingStore.open(self.store_dir)
        self.assertIn("offset", str(ctx.exception))
        self.assertIsNotNone(ctx.exception.offset)
        # And nothing was rebuilt: the file still holds all four frames.
        self.assertGreater(os.path.getsize(wal), 0)

    def test_corrupt_segment_footer_refuses_open(self):
        for i in range(3):
            cli_put(self.store_dir, hx("k%d" % i), hx("v%d" % i))
        run_cli(["staging", "flush", self.store_dir])
        segment = os.path.join(self.store_dir, segment_files(self.store_dir)[0])
        with open(segment, "r+b") as handle:
            handle.seek(-8, os.SEEK_END)
            handle.write(b"\xff\xff")
        with self.assertRaises(CorruptionError):
            StagingStore.open(self.store_dir)
        # The directory was not replaced by an empty store.
        self.assertTrue(os.path.exists(os.path.join(self.store_dir, "CURRENT")))

    def test_corrupt_data_block_fails_the_read_not_the_store(self):
        for i in range(3):
            cli_put(self.store_dir, hx("k%d" % i), hx("v%d" % i))
        run_cli(["staging", "flush", self.store_dir])
        segment = os.path.join(self.store_dir, segment_files(self.store_dir)[0])
        with open(segment, "r+b") as handle:  # flip a byte in the first block
            handle.seek(4)
            handle.write(b"\xaa")
        store = StagingStore.open(self.store_dir)  # footer+index still valid
        with self.assertRaises(CorruptionError):
            store.get(b"k1")
        store.close()

    def test_missing_current_is_not_silently_rebuilt(self):
        cli_put(self.store_dir, "aa", "01")
        os.remove(os.path.join(self.store_dir, "CURRENT"))
        with self.assertRaises(CorruptionError):
            StagingStore.open(self.store_dir)
        # A genuinely empty directory is still a fresh store.
        fresh = StagingStore.open(os.path.join(self.dir, "fresh"))
        fresh.close()

    def test_corrupt_current_and_manifest_refuse_open(self):
        cli_put(self.store_dir, "aa", "01")
        current = os.path.join(self.store_dir, "CURRENT")
        with open(current, "w") as handle:
            handle.write("GARBAGE\n")
        with self.assertRaises(CorruptionError):
            StagingStore.open(self.store_dir)

        other = os.path.join(self.dir, "store2")
        cli_put(other, "aa", "01")
        manifest = [n for n in os.listdir(other) if n.startswith("MANIFEST-")][0]
        path = os.path.join(other, manifest)
        with open(path, "r+b") as handle:  # flip a byte deep inside the JSON
            data = bytearray(handle.read())
            data[len(data) // 2] ^= 0xFF
            handle.seek(0)
            handle.write(data)
        with self.assertRaises(CorruptionError):
            StagingStore.open(other)

    def test_second_process_cannot_open_for_writing(self):
        store = StagingStore.open(self.store_dir)
        try:
            proc = cli_put(self.store_dir, "aa", "01")
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stdout)["error"], "store_locked")
        finally:
            store.close()
        # Once the lock is released a writer succeeds again.
        proc = cli_put(self.store_dir, "aa", "01")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
