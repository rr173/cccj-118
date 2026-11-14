#!/usr/bin/env python3
"""Fault-injection demonstration with real processes, checked from outside.

Usage:  PYTHONPATH=src python3 demo/staging_fault_demo.py [DIR]

Each scenario uses the real CLI in a child process (or direct file damage)
and then inspects the store from this process:

  1. SIGKILL before the WAL fsync  -> the unconfirmed batch vanishes cleanly
  2. SIGKILL after the WAL fsync   -> the confirmed batch survives
  3. SIGKILL after the flush segment is written but before the manifest
     switch                            -> data recovered from the WAL
  4. SIGKILL after the compact manifest switch but before old segments
     are deleted                        -> new segment wins, leftovers cleaned
  5. truncated WAL tail                -> only complete frames recovered
  6. corrupted mid-log frame           -> open refuses, reports the offset
  7. corrupted segment file            -> open refuses; nothing is rebuilt
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from distagg.staging import CorruptionError, StagingStore


def env():
    result = dict(os.environ)
    result["PYTHONPATH"] = SRC + os.pathsep + result.get("PYTHONPATH", "")
    return result


def cli(args):
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True, env=env(),
    )


def cli_get(store_dir, key):
    proc = cli(["staging", "get", store_dir, key.encode().hex()])
    return json.loads(proc.stdout)


def show(title):
    print("\n=== %s ===" % title)


def wal_path(store_dir):
    return os.path.join(store_dir, [n for n in os.listdir(store_dir) if n.endswith(".log")][0])


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="staging-fault-")
    if os.path.exists(base):
        shutil.rmtree(base)
    os.makedirs(base)
    print("scratch directory:", base)

    show("1. killed before WAL fsync: the unconfirmed batch is gone, store is fine")
    store_dir = os.path.join(base, "s1")
    cli(["staging", "put", store_dir, "aa".encode().hex(), "01"])
    proc = cli(["staging", "put", store_dir, "bb".encode().hex(), "02",
                "--kill-at", "wal_before_sync"])
    print("killer process exit:", proc.returncode, "(killed by SIGKILL)")
    print("aa:", cli_get(store_dir, "aa"), " bb:", cli_get(store_dir, "bb"))

    show("2. killed after WAL fsync: the confirmed batch survives")
    proc = cli(["staging", "put", store_dir, "cc".encode().hex(), "03",
                "--kill-at", "wal_after_sync"])
    print("killer process exit:", proc.returncode)
    print("cc:", cli_get(store_dir, "cc"))

    show("3. killed after the flush segment was written, before manifest switch")
    store_dir = os.path.join(base, "s3")
    for i in range(4):
        cli(["staging", "put", store_dir, ("k%d" % i).encode().hex(), ("v%d" % i).encode().hex()])
    proc = cli(["staging", "flush", store_dir, "--kill-at", "flush_segment_written"])
    print("killer process exit:", proc.returncode)
    print("files left behind:", sorted(os.listdir(store_dir)))
    store = StagingStore.open(store_dir)
    print("after reopen: k0..k3 =", [store.get(("k%d" % i).encode()) for i in range(4)],
          " commit:", store.commit_number)
    print("segments after cleanup:", [n for n in os.listdir(store_dir) if n.endswith(".sst")])
    store.close()

    show("4. killed after the compact manifest switch, before old segments are deleted")
    store_dir = os.path.join(base, "s4")
    for generation in range(3):
        for i in range(3):
            cli(["staging", "put", store_dir, ("k%d" % i).encode().hex(), ("g%d" % generation).encode().hex()])
        cli(["staging", "flush", store_dir])
    print("segments before compact:", len([n for n in os.listdir(store_dir) if n.endswith(".sst")]))
    proc = cli(["staging", "compact", store_dir, "--kill-at", "compact_manifest_switched"])
    print("killer process exit:", proc.returncode)
    print("files left behind:", sorted(n for n in os.listdir(store_dir) if n.endswith(".sst")))
    store = StagingStore.open(store_dir)
    print("after reopen: k0..k2 =", [store.get(("k%d" % i).encode()) for i in range(3)],
          " segments:", len([n for n in os.listdir(store_dir) if n.endswith(".sst")]))
    store.close()

    show("5. truncated WAL tail: only complete frames are recovered")
    store_dir = os.path.join(base, "s5")
    for i in range(4):
        cli(["staging", "put", store_dir, ("k%d" % i).encode().hex(), ("v%d" % i).encode().hex()])
    path = wal_path(store_dir)
    size = os.path.getsize(path)
    with open(path, "r+b") as handle:
        handle.truncate(size - 3)  # tear the last frame apart
    print("truncated the WAL from %d to %d bytes" % (size, size - 3))
    store = StagingStore.open(store_dir)
    print("visible: k0..k3 =", [store.get(("k%d" % i).encode()) for i in range(4)],
          " commit:", store.commit_number)
    store.close()

    show("6. corrupted mid-log frame: open refuses and reports the offset")
    store_dir = os.path.join(base, "s6")
    for i in range(4):
        cli(["staging", "put", store_dir, ("k%d" % i).encode().hex(), ("v%d" % i).encode().hex()])
    path = wal_path(store_dir)
    with open(path, "r+b") as handle:
        data = handle.read()
        handle.seek(12)  # inside the first frame's payload, not the tail
        handle.write(bytes([data[12] ^ 0xFF]))
    try:
        StagingStore.open(store_dir)
        print("ERROR: the store opened despite the corruption")
    except CorruptionError as exc:
        print("open refused as required:")
        print(" ", exc)
        print("  (file: %s, offset: %s)" % (exc.path, exc.offset))

    show("7. corrupted segment file: open refuses, nothing is rebuilt")
    store_dir = os.path.join(base, "s7")
    for i in range(3):
        cli(["staging", "put", store_dir, ("k%d" % i).encode().hex(), ("v%d" % i).encode().hex()])
    cli(["staging", "flush", store_dir])
    segment = [n for n in os.listdir(store_dir) if n.endswith(".sst")][0]
    with open(os.path.join(store_dir, segment), "r+b") as handle:
        handle.seek(-8, os.SEEK_END)
        handle.write(b"\xff\xff")
    try:
        StagingStore.open(store_dir)
        print("ERROR: the store opened despite the corruption")
    except CorruptionError as exc:
        print("open refused as required:")
        print(" ", exc)
    print("the files are untouched:", sorted(os.listdir(store_dir)))


if __name__ == "__main__":
    main()
