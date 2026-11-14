# distagg staging store — a small LSM engine

`distagg.staging` is an **independent disk staging store**: a small
log-structured merge (LSM) storage engine implemented in pure Python on top
of files.  It is used separately from the exposure/tag aggregation database
(which stays on SQLite); it does not extend the aggregation metrics and it
does not re-implement the old computation on a new backend.

It exists for the backfill workflow: corrections are accumulated locally as
key/value edits and handed downstream in key order.  Keys and values are
arbitrary byte strings — the empty string is a legal key and a legal value.
On the command line both are passed as hexadecimal; the Python API takes
`bytes` directly.  Keys are ordered by **unsigned-byte lexicographic order**
(Python `bytes` comparison); they are never interpreted as integers or text.

No SQLite, DuckDB, dbm or any other key/value engine is used to store
records: the WAL, the sorted segments and the manifest are self-encoded
formats described below.

## API overview

```python
from distagg.staging import StagingStore, Batch

store = StagingStore.open("/path/to/dir",
                          memtable_budget=4 * 1024 * 1024,  # byte budget of the write buffer
                          block_size=4096)                  # segment block target size

commit = store.put(b"key", b"value")        # increasing commit number
commit = store.delete(b"key")               # deleting a missing key is legal
commit = store.write_batch([("put", b"a", b"1"), ("delete", b"b"),
                            ("put", b"a", b"2")])   # last op per key wins
commit = store.write_batch(Batch().put(b"x", b"1").delete(b"y"))

value = store.get(b"key")                   # bytes, or None (missing/deleted)
value, stats = store.get_with_stats(b"key") # + which segments/blocks were read

snap = store.snapshot()                     # pinned read-only view
snap.commit_number
snap.get(b"key")                            # sees the store as of the snapshot
with snap.scan(lower=b"a", upper=b"z") as it:   # [lower, upper), key order
    for key, value in it: ...
snap.close()                                # releases versions + file pins

with store.scan(lower=b"a", upper=b"z") as it:  # also pins its view at creation
    ...

store.flush()                               # memtable -> new segment
store.compact()                             # merge all segments, reclaim versions
store.stats()                               # sizes, record counts, file inventory
store.close()
```

* Every successful `put`/`delete`/`write_batch` returns an **increasing
  commit number**.  One batch is one commit; readers see the state either
  before or after the whole batch, never a partial batch.  (There is no
  `seq` re-send protocol like the aggregation database has — commit numbers
  are simply handed out in order.)
* `scan` is **left-closed, right-open**, ascending, and yields each key at
  most once (the newest visible value; deleted keys are omitted).
* A batch may touch the same key repeatedly; the last operation in batch
  order wins.
* Using a closed store/snapshot/iterator raises `ClosedError`.
* One process holds the write lock at a time (an exclusive `flock` on the
  directory's `LOCK` file); a second writer — in another process or the same
  one — gets `StoreLockedError`.  Inside the owning process, one writer and
  any number of readers (snapshots, iterators) may be active concurrently.
  `StagingStore.open(path, read_only=True)` opens a lock-free read-only
  handle for inspection.

## Directory layout

```
<dir>/
  LOCK                 flock'd by the writing process for its lifetime
  CURRENT              one line: the name of the live manifest
  MANIFEST-<id>        JSON: which files form the committed store
  wal-<id>.log         the write-ahead log, a sequence of batch frames
  seg-<id>.sst         sorted immutable segments
  *.tmp                files being written; never committed, deleted on open
```

**Which files belong to the current valid store is decided solely by the
manifest named in `CURRENT`.**  Everything else — temp files, segments from
a crashed flush/compaction, outdated WALs and manifests — is recognised on
open and removed, never mistaken for committed content.

### Manifest

`MANIFEST-<id>` is a JSON document `{"payload": ..., "crc32": ...}` where
`crc32` covers the canonical JSON of the payload:

```json
{
  "kind": "distagg-staging-manifest",
  "format": 1,
  "commit": 41,               // batches <= 41 are included in the segments below
  "next_file_id": 18,
  "wal": "wal-000017.log",    // frames for batches > 41 live here
  "segments": [               // newest first
    {"file": "seg-000016.sst", "min_key_hex": "...", "max_key_hex": "...",
     "min_commit": 30, "max_commit": 41, "records": 1234, "bytes": 56789}
  ]
}
```

A manifest switch writes `MANIFEST-<id+1>` (via `.tmp` + fsync + rename +
directory fsync) and only then repoints `CURRENT` (again via `.tmp` +
rename + directory fsync).  A crash leaves either the old or the new pair
consistent.  A missing/corrupt `CURRENT`, a corrupt manifest (bad JSON, bad
CRC, unknown fields), or references to missing files **refuse the open** —
the store is never silently rebuilt as empty.

### Write-ahead log

Each committed batch is exactly **one frame**, so a crash can leave a batch
fully present or fully absent, never half applied:

```
magic:       4 bytes  b"DWAL"
type:        1 byte   0x01 = batch
payload_len: uvarint
header_crc:  4 bytes  crc32(magic | type | payload_len)
payload:     commit uvarint | op_count uvarint | ops...
             op = op_type(1: 0=delete, 1=put) | key_len uvarint | key
                  [| value_len uvarint | value ]
payload_crc: 4 bytes  crc32(payload)
```

The batch is collapsed before logging (one final op per key).  The writer
buffers the frame and performs `write` + `fsync` in one step; the commit
number is returned to the caller only after the fsync.

**Recovery** decodes frames from the start:

* a frame that is *incomplete* (header or payload cut off) is a torn tail
  from a crash — recovery keeps the complete, checksum-valid prefix and the
  file is truncated back to it before new appends;
* a *complete* frame with bad magic, bad header CRC, bad payload CRC or a
  malformed payload is real corruption (a torn write can only shorten a
  frame, never forge checksums) — recovery raises `CorruptionError` naming
  the file and the exact frame offset;
* frames with `commit <= manifest.commit` are already inside a segment
  (crash between manifest switch and WAL removal) and are skipped, so
  reopening never applies a batch twice.

### Segments

A segment is an immutable sorted run of versioned records:

```
[data block]...[index section][footer][footer_len:4][magic b"DSG1":4]

block:   records... | crc32:4          records ordered (key asc, commit desc)
record:  flags(1: bit0 = tombstone) | commit uvarint
         | key_len uvarint | key [| value_len uvarint | value ]
index:   count uvarint | per block: first_key | block_offset | block_len
         | crc32:4
footer:  min_key | max_key | min_commit | max_commit | record_count
         | index_offset | index_len | crc32:4
```

* All versions of one key live in a single block (blocks only close before
  a new key), so a **point lookup reads at most one block per candidate
  segment**, found by binary-searching the sparse index.  Only the sparse
  index is cached in memory; record data is read on demand.
* Every block and the index carry CRC-32 checksums, verified on each read;
  the footer is verified when the segment is opened.  Any mismatch raises
  `CorruptionError` — nothing is rebuilt silently.
* Lookups skip segments whose key range or commit range cannot contain a
  visible version; `get_with_stats` reports exactly what was read.

## Memtable, flush, compact

The write buffer is a bounded multi-version map.  When a batch pushes it
past `memtable_budget` it is flushed; **one over-budget batch may exceed
the budget transiently, but the flush happens immediately after that batch
completes**, so the buffer cannot grow without bound.

**Flush** freezes the memtable (atomically swapping in a fresh one together
with a fresh WAL), writes it as a new segment (`.tmp` → fsync → rename →
directory fsync), then switches the manifest to `commit = <frozen commit>`,
listing the new segment and the new WAL.  Only then is the old WAL removed.

**Compact** merges *all* on-disk segments into one (streaming, one block
per input segment at a time).  For each key it keeps the newest version at
or below **every currently pinned commit** (active snapshots and iterators)
plus the newest version overall; everything else — overwritten values and
useless tombstones — is dropped.  A tombstone is dropped only when it is
the *sole* kept version of its key (no older version exists anywhere that
it still needs to hide), so values cannot resurrect.  The merged segment
replaces the old ones only after it is complete, fsynced and the manifest
switch succeeded; old files are deleted once the last snapshot/iterator
pin on them is released.  Foreground reads keep a consistent view
throughout.  The `compact` result and `stats()` report real segment
counts, record (version) counts and file bytes so reclamation is visible.

## Snapshots and iterators

`store.snapshot()` pins the current commit number and the segment files of
its view; `get`/`scan` on it see exactly that commit, unaffected by later
writes, deletes, flushes or compactions.  `store.scan(...)` (without an
explicit snapshot) also pins its view at creation.  Both must be closed;
exhausted iterators close themselves.  Closing releases the read
protection: versions compaction must keep and segment files deletion must
wait for.  Snapshots are in-process only — reopening the store restores the
latest committed state, which is all a restart needs.

## Pause points for fault-injection tests

The CLI's mutating commands accept `--pause-points`, `--pause-signal-file`,
`--pause-wait-file`, `--pause-delay` and `--kill-at POINT` (SIGKILLs the
real process at the point):

| point | meaning |
|---|---|
| `wal_before_sync` / `wal_after_sync` | around the WAL fsync that makes a batch durable |
| `flush_segment_written` | segment complete, manifest not yet switched |
| `flush_manifest_switched` | manifest switched, old WAL not yet removed |
| `compact_segment_written` | merged segment complete, manifest not yet switched |
| `compact_manifest_switched` | manifest switched, old segments not yet removed |

Kills at every point are covered by `tests/test_staging_crash.py`:
confirmed batches survive, unconfirmed batches vanish all-or-nothing,
orphaned files are recognised and cleaned on the next open, and recovery
never applies a batch twice.

## CLI

```bash
distagg staging put     DIR 6b6579 76616c       # key/value as hex ("" = empty)
distagg staging delete  DIR 6b6579
distagg staging batch   DIR ops.json            # [{"op":"put","key":"..","value":".."}, ...]
distagg staging get     DIR 6b6579 [--stats]
distagg staging scan    DIR [--lower HEX] [--upper HEX] [--limit N]
distagg staging flush   DIR
distagg staging compact DIR
distagg staging stats   DIR
```

All output is JSON.  `compact` prints before/after segment counts, record
counts and bytes; `stats` prints the full file inventory.

### Batch TSV correction importer

`distagg staging import ...` is a separate entry point that feeds
supplier-correction TSV files into this store without changing its
key/value semantics.  It classifies every row (importable / duplicate /
conflict / quarantined), writes importable rows as ordinary `put` batches
of a configured size, records a fingerprinted, resumable batch ledger, and
keeps the original bytes of isolated rows in a quarantine file.  It never
opens the aggregation database.  See [IMPORTER.md](IMPORTER.md) for the
configuration, reason codes, resume and report model.

```bash
distagg staging import precheck CONFIG.json corrections.tsv STATE_DIR
distagg staging import run       CONFIG.json corrections.tsv DIR STATE_DIR
distagg staging import status    STATE_DIR
```

## Reproducible demos

```bash
PYTHONPATH=src python3 demo/staging_demo.py        # short-key semantics tour
PYTHONPATH=src python3 demo/staging_scale_demo.py  # segments, lookup stats, scan memory
PYTHONPATH=src python3 demo/staging_fault_demo.py  # crashes, truncation, corruption
python3 -m unittest discover -s tests -p "test_staging_*.py"   # the test suite
```
