"""The staging store: a small LSM tree (WAL + memtable + sorted segments).

Roles of the moving parts:

* **WAL** — every batch is appended as one frame and fsynced before its
  commit number is returned.  On open, complete frames newer than the
  manifest's commit are replayed into a fresh memtable; a torn tail is
  dropped, anything else invalid refuses the open.
* **Memtable** — bounded multi-version buffer.  Reaching the byte budget
  triggers a flush; one over-budget batch may exceed it transiently but is
  flushed immediately after it completes.
* **Segments** — immutable sorted runs with a sparse index, produced by
  flush and compact.  The manifest (swapped atomically via CURRENT) decides
  which segment files are part of the committed store; everything else in
  the directory (``*.tmp``, unreferenced ``seg-*.sst``/``wal-*.log``,
  outdated ``MANIFEST-*``) is recognised as leftover and removed on open.
* **Snapshots / iterators** — pin a commit number and the segment files of
  their view.  Compaction keeps, per key, the newest version at or below
  every pinned commit (plus the newest overall), and only drops a tombstone
  when no pinned reader and no older kept version can still observe it.
  Pinned files are deleted only after the last pin is released.

Pause points (for fault-injection tests; a ``pause(point, info)`` callback
may block or kill the process):

* ``wal_before_sync`` / ``wal_after_sync`` — around the WAL fsync that
  makes a batch durable;
* ``flush_segment_written`` — segment file complete, manifest not switched;
* ``flush_manifest_switched`` — manifest switched, old WAL not yet removed;
* ``compact_segment_written`` — merged segment complete, manifest not switched;
* ``compact_manifest_switched`` — manifest switched, old segments not yet removed.
"""

from __future__ import annotations

import heapq
import os
import threading
import weakref
from typing import Dict, Iterable, List, Optional, Tuple

from . import format as fmt
from .errors import (
    ClosedError,
    CorruptionError,
    InvalidOperation,
    ReadOnlyError,
    StagingError,
    StoreLockedError,
)
from .manifest import Manifest, load_manifest, store_manifest_atomic
from .memtable import MISSING, Memtable
from .segment import SegmentReader, SegmentWriter, _fsync_dir
from .wal import WalWriter, recover_wal

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

DEFAULT_MEMTABLE_BUDGET = 4 * 1024 * 1024
DEFAULT_BLOCK_SIZE = 4096

PAUSE_POINTS = (
    "wal_before_sync",
    "wal_after_sync",
    "flush_segment_written",
    "flush_manifest_switched",
    "compact_segment_written",
    "compact_manifest_switched",
)


def _as_bytes(value, what: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise InvalidOperation(f"{what} must be bytes, got {type(value).__name__}")


def _as_optional_bytes(value, what: str) -> Optional[bytes]:
    if value is None:
        return None
    return _as_bytes(value, what)


class Batch:
    """Builder for one atomic batch; the same key may be touched repeatedly.

    Operations apply in order and the last operation per key wins, e.g.
    ``put k, delete k, put k`` leaves the second put.  Readers only ever see
    the state before or after the whole batch.
    """

    def __init__(self) -> None:
        self._ops: List[Tuple[bytes, Optional[bytes]]] = []

    def put(self, key: bytes, value: bytes) -> "Batch":
        self._ops.append(("put", _as_bytes(key, "key"), _as_bytes(value, "value")))
        return self

    def delete(self, key: bytes) -> "Batch":
        self._ops.append(("delete", _as_bytes(key, "key")))
        return self

    @property
    def ops(self) -> list:
        return list(self._ops)

    def __len__(self) -> int:
        return len(self._ops)


def _normalize_ops(ops) -> List[Tuple[bytes, Optional[bytes]]]:
    """Validate operations and collapse them to one final op per key."""
    if isinstance(ops, Batch):
        ops = ops.ops
    collapsed: Dict[bytes, Optional[bytes]] = {}
    for op in ops:
        if not isinstance(op, (tuple, list)):
            raise InvalidOperation(f"batch op must be a tuple, got {type(op).__name__}")
        if len(op) == 3 and op[0] == "put":
            key = _as_bytes(op[1], "key")
            collapsed[key] = _as_bytes(op[2], "value")
        elif len(op) == 2 and op[0] in ("delete", "del"):
            key = _as_bytes(op[1], "key")
            collapsed[key] = None
        else:
            raise InvalidOperation(
                f"batch op must be ('put', key, value) or ('delete', key), got {op!r}"
            )
    return list(collapsed.items())


class StagingStore:
    """One open handle on a staging store directory.

    Use :meth:`open`.  A single process-wide writer is enforced by an
    exclusive ``flock`` on the directory's ``LOCK`` file; any number of
    readers (snapshots, scan iterators) may coexist with it in-process.
    """

    # ------------------------------------------------------------------ open
    @classmethod
    def open(
        cls,
        path: str,
        *,
        memtable_budget: int = DEFAULT_MEMTABLE_BUDGET,
        block_size: int = DEFAULT_BLOCK_SIZE,
        read_only: bool = False,
        pause=None,
    ) -> "StagingStore":
        if memtable_budget <= 0:
            raise InvalidOperation("memtable_budget must be positive")
        if block_size < 64:
            raise InvalidOperation("block_size must be at least 64")
        store = cls(
            path,
            memtable_budget=memtable_budget,
            block_size=block_size,
            read_only=read_only,
            pause=pause,
        )
        store._open()
        return store

    def __init__(self, path, *, memtable_budget, block_size, read_only, pause):
        self._dir = os.path.abspath(path)
        self._budget = memtable_budget
        self._block_size = block_size
        self._read_only = read_only
        self._pause = pause if pause is not None else (lambda point, info: None)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._lock_fd = None
        self._memtable = Memtable()
        # A memtable currently being written out by flush(); it stays part of
        # every read view until its segment joins the segment list, so a
        # snapshot taken mid-flush cannot miss its batches.
        self._flushing: Optional[Memtable] = None
        self._segments: List[SegmentReader] = []  # newest first
        self._all_readers: List[SegmentReader] = []  # live + obsolete-but-pinned
        self._commit = 0
        self._manifest = None
        self._manifest_id = 0
        self._next_file_id = 1
        self._wal: Optional[WalWriter] = None
        self._wal_name: Optional[str] = None
        self._snapshots: "weakref.WeakSet[Snapshot]" = weakref.WeakSet()
        self._iterators: "weakref.WeakSet[ScanIterator]" = weakref.WeakSet()
        self.deleted_files = 0  # statistics: obsolete files actually removed

    def _open(self) -> None:
        if self._read_only:
            if not os.path.isdir(self._dir):
                raise StagingError(f"no staging store at {self._dir}")
        else:
            os.makedirs(self._dir, exist_ok=True)
            self._acquire_lock()
        try:
            current_path = os.path.join(self._dir, "CURRENT")
            if os.path.exists(current_path):
                self._manifest, self._manifest_id = load_manifest(self._dir)
            else:
                if self._read_only:
                    raise CorruptionError(
                        f"{self._dir}: CURRENT is missing; the store is incomplete"
                    )
                leftovers = self._store_files_present()
                if leftovers:
                    raise CorruptionError(
                        f"{self._dir}: CURRENT is missing but store files remain "
                        f"({', '.join(sorted(leftovers))}); refusing to rebuild an empty store"
                    )
                self._init_fresh()
            if not self._read_only:
                self._remove_leftovers()
            self._next_file_id = self._manifest.next_file_id
            self._open_segments()
            self._recover_wal()
        except Exception:
            self._release_lock()
            raise

    def _acquire_lock(self) -> None:
        lock_path = os.path.join(self._dir, "LOCK")
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        if fcntl is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self._lock_fd)
                self._lock_fd = None
                raise StoreLockedError(
                    f"staging store at {self._dir} is already open for writing "
                    "by another process"
                )

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def _store_files_present(self) -> List[str]:
        names = []
        for entry in os.listdir(self._dir):
            if entry == "LOCK":
                continue
            if (
                entry.startswith("MANIFEST-")
                or entry.startswith("wal-")
                or entry.startswith("seg-")
                or entry.endswith(".tmp")
            ):
                names.append(entry)
        return names

    def _init_fresh(self) -> None:
        self._manifest_id = 1
        wal = fmt.wal_name(2)
        # The WAL file exists before the manifest that references it, so a
        # completed init is always consistent (a crash mid-init leaves files
        # that make the next open fail loudly rather than rebuild silently).
        fd = os.open(os.path.join(self._dir, wal), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.fsync(fd)
        os.close(fd)
        self._manifest = Manifest(commit=0, next_file_id=3, wal=wal, segments=[])
        store_manifest_atomic(self._dir, self._manifest, self._manifest_id)
        _fsync_dir(self._dir)

    def _remove_leftovers(self) -> None:
        """Delete files that cannot be part of the committed store.

        Anything not referenced by the live manifest — temp files, orphaned
        segments from a crash between segment write and manifest switch,
        outdated WALs and manifests — is recognised and removed here.
        """
        live_segments = {meta.file for meta in self._manifest.segments}
        for entry in os.listdir(self._dir):
            if entry in ("LOCK", "CURRENT"):
                continue
            path = os.path.join(self._dir, entry)
            remove = False
            if entry.endswith(".tmp"):
                remove = True
            elif entry.startswith("MANIFEST-"):
                remove = entry != fmt.manifest_name(self._manifest_id)
            elif fmt.parse_numbered_file(entry, "seg-", ".sst") is not None:
                remove = entry not in live_segments
            elif fmt.parse_numbered_file(entry, "wal-", ".log") is not None:
                remove = entry != self._manifest.wal
            if remove:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _open_segments(self) -> None:
        readers = []
        for meta in self._manifest.segments:
            path = os.path.join(self._dir, meta.file)
            if not os.path.exists(path):
                raise CorruptionError(
                    f"{self._dir}: manifest references missing segment {meta.file}",
                    path=path,
                )
            readers.append(SegmentReader.open(path, expected=meta))
        self._segments = readers
        self._all_readers = list(readers)

    def _recover_wal(self) -> None:
        wal_path = os.path.join(self._dir, self._manifest.wal)
        if not os.path.exists(wal_path):
            raise CorruptionError(
                f"{self._dir}: manifest references missing wal {self._manifest.wal}",
                path=wal_path,
            )
        batches, valid_length = recover_wal(wal_path)
        size = os.path.getsize(wal_path)
        if valid_length < size and not self._read_only:
            # Drop the torn tail so future appends start at a clean frame
            # boundary.  Only complete, checksum-valid batches survive.
            os.truncate(wal_path, valid_length)
        applied = 0
        skipped = 0
        highest = self._manifest.commit
        for commit, ops in batches:
            if commit <= self._manifest.commit:
                # Already inside a segment (crash between manifest switch and
                # WAL removal); replaying would apply the batch twice.
                skipped += 1
                continue
            self._memtable.apply_batch(commit, ops)
            highest = max(highest, commit)
            applied += 1
        self._commit = highest
        self._wal_name = self._manifest.wal
        if not self._read_only:
            self._wal = WalWriter(wal_path)
        self.recovered_batches = applied
        self.skipped_batches = skipped

    # --------------------------------------------------------------- helpers
    def _check_open(self) -> None:
        if self._closed:
            raise ClosedError("staging store is closed")

    def _check_writable(self) -> None:
        if self._read_only:
            raise ReadOnlyError("staging store was opened read-only")

    def _pause_point(self, point: str, info: dict) -> None:
        self._pause(point, info)

    def _memtables_locked(self) -> List[Memtable]:
        """The memtable chain of a read view, newest first.

        The live memtable holds the newest commits, the (optional) frozen
        one being flushed holds older ones, and the segments hold the rest.
        """
        memtables = [self._memtable]
        if self._flushing is not None:
            memtables.append(self._flushing)
        return memtables

    def _pin(self, readers: Iterable[SegmentReader]) -> None:
        for reader in readers:
            reader.pins += 1

    def _unpin(self, readers: Iterable[SegmentReader]) -> None:
        for reader in readers:
            reader.pins -= 1
            self._maybe_delete_reader(reader)

    def _maybe_delete_reader(self, reader: SegmentReader) -> None:
        """Delete an obsolete segment file once no snapshot/iterator needs it."""
        if reader.obsolete and reader.pins <= 0 and reader in self._all_readers:
            self._all_readers.remove(reader)
            reader.close()
            try:
                os.remove(reader.path)
                self.deleted_files += 1
            except FileNotFoundError:
                pass

    def _protected_commits_locked(self) -> List[int]:
        """Commits whose visible versions compaction must preserve."""
        commits = {self._commit}
        for snap in list(self._snapshots):
            if not snap.closed:
                commits.add(snap.commit_number)
        for it in list(self._iterators):
            if not it.closed:
                commits.add(it.commit_number)
        return sorted(commits)

    def _alloc_file_id_locked(self) -> int:
        file_id = self._next_file_id
        self._next_file_id += 1
        return file_id

    def _store_manifest(self, manifest: Manifest) -> None:
        self._manifest_id += 1
        store_manifest_atomic(self._dir, manifest, self._manifest_id)
        self._manifest = manifest

    # ------------------------------------------------------------------ write
    def put(self, key: bytes, value: bytes) -> int:
        """Set ``key`` to ``value``; returns the new commit number."""
        return self.write_batch([("put", key, value)])

    def delete(self, key: bytes) -> int:
        """Remove ``key`` (deleting a missing key is legal); returns the commit number."""
        return self.write_batch([("delete", key)])

    def write_batch(self, ops) -> int:
        """Apply one batch atomically and return its commit number.

        The batch is collapsed to the last operation per key, appended to the
        WAL as a single frame, fsynced, and only then becomes visible: every
        reader sees either the state before or after the whole batch.
        """
        collapsed = _normalize_ops(ops)
        with self._write_lock:
            with self._lock:
                self._check_open()
                self._check_writable()
                commit = self._commit + 1
                wal = self._wal
                memtable = self._memtable
            wal.append_batch(commit, collapsed)
            self._pause_point("wal_before_sync", {"commit": commit, "ops": len(collapsed)})
            wal.sync()
            self._pause_point("wal_after_sync", {"commit": commit, "ops": len(collapsed)})
            with self._lock:
                memtable.apply_batch(commit, collapsed)
                self._commit = commit
                over_budget = memtable.bytes_used >= self._budget
            if over_budget:
                # The budget is enforced right after the batch completes, so
                # one huge batch may exceed it transiently but never for long.
                self._flush_locked()
            return commit

    # ------------------------------------------------------------------ reads
    @property
    def commit_number(self) -> int:
        """The highest committed batch number."""
        with self._lock:
            return self._commit

    def get(self, key: bytes) -> Optional[bytes]:
        """Newest committed value for ``key`` or ``None`` (missing or deleted)."""
        value, _ = self._get(key, None)
        return value

    def get_with_stats(self, key: bytes):
        """Like :meth:`get` but also reports what the lookup actually read."""
        return self._get(key, {})

    def _get(self, key: bytes, stats: Optional[dict]):
        key = _as_bytes(key, "key")
        with self._lock:
            self._check_open()
            memtables = self._memtables_locked()
            readers = list(self._segments)
            commit = self._commit
            self._pin(readers)
            mem_result = MISSING
            for memtable in memtables:
                mem_result = memtable.get(key, commit)
                if mem_result is not MISSING:
                    break
        try:
            return self._get_in_segments(mem_result, readers, key, commit, stats), stats
        finally:
            with self._lock:
                self._unpin(readers)

    def _get_in_segments(self, mem_result, readers, key, commit, stats):
        if mem_result is not MISSING:
            if stats is not None:
                stats.update(
                    memtable_hit=True,
                    segments_considered=0,
                    segments_skipped_key_range=0,
                    segments_skipped_commit=0,
                    blocks_read=0,
                    bytes_read=0,
                )
            return mem_result  # bytes value, or None for a tombstone
        if stats is not None:
            stats.update(
                memtable_hit=False,
                segments_considered=0,
                segments_skipped_key_range=0,
                segments_skipped_commit=0,
                blocks_read=0,
                bytes_read=0,
            )
        best_commit = -1
        best_value = None
        for reader in readers:
            if key < reader.min_key or key > reader.max_key:
                if stats is not None:
                    stats["segments_skipped_key_range"] += 1
                continue
            if reader.min_commit > commit:
                if stats is not None:
                    stats["segments_skipped_commit"] += 1
                continue
            if best_commit >= 0 and reader.max_commit <= best_commit:
                # Newest-first order: nothing in this or older segments can
                # beat the version already found.
                if stats is not None:
                    stats["segments_skipped_commit"] += 1
                continue
            before_blocks, before_bytes = reader.blocks_read, reader.bytes_read
            versions = reader.find(key)
            if stats is not None:
                stats["segments_considered"] += 1
                stats["blocks_read"] += reader.blocks_read - before_blocks
                stats["bytes_read"] += reader.bytes_read - before_bytes
            if versions:
                for version_commit, value in versions:
                    if version_commit <= commit:
                        if version_commit > best_commit:
                            best_commit, best_value = version_commit, value
                        break
        return best_value if best_commit >= 0 else None

    def scan(self, lower: Optional[bytes] = None, upper: Optional[bytes] = None) -> "ScanIterator":
        """A merged iterator over ``[lower, upper)`` at the current commit.

        The iterator pins its view (memtable copy + segment files) at
        creation: later writes, flushes and compactions do not change what
        it yields, and the files it needs are not deleted before it closes.
        """
        lower, upper = self._check_bounds(lower, upper)
        with self._lock:
            self._check_open()
            items = [mt.items_snapshot(lower) for mt in self._memtables_locked()]
            readers = list(self._segments)
            commit = self._commit
            self._pin(readers)
            try:
                iterator = ScanIterator(self, items, readers, commit, lower, upper)
            except Exception:
                self._unpin(readers)
                raise
            self._iterators.add(iterator)
            return iterator

    def snapshot(self) -> "Snapshot":
        """A read-only view of the store at the current commit number."""
        with self._lock:
            self._check_open()
            snapshot = Snapshot(
                self, self._commit, self._memtables_locked(), list(self._segments)
            )
            self._pin(snapshot._readers)
            self._snapshots.add(snapshot)
            return snapshot

    def _check_bounds(self, lower, upper):
        lower = _as_optional_bytes(lower, "lower bound")
        upper = _as_optional_bytes(upper, "upper bound")
        if lower is not None and upper is not None and lower > upper:
            raise InvalidOperation(
                f"empty scan range: lower {lower!r} is greater than upper {upper!r}"
            )
        return lower, upper

    # ------------------------------------------------------------- maintenance
    def flush(self) -> Optional[dict]:
        """Write the memtable to a new segment and retire its WAL.

        Returns the new segment's manifest entry, or ``None`` when the
        memtable was empty.
        """
        with self._write_lock:
            with self._lock:
                self._check_open()
                self._check_writable()
            return self._flush_locked()

    def _flush_locked(self) -> Optional[dict]:
        with self._lock:
            if self._memtable.record_count == 0:
                return None
            frozen = self._memtable
            frozen_commit = self._commit
            seg_id = self._alloc_file_id_locked()
            wal_id = self._alloc_file_id_locked()
            next_file_id = self._next_file_id
            seg_name = fmt.segment_name(seg_id)
            wal_name = fmt.wal_name(wal_id)
            # Swap memtable and WAL atomically: batches committed from now on
            # land in the fresh memtable and the fresh WAL, so the frozen
            # memtable and the old WAL hold exactly the commits <= frozen_commit.
            # The frozen memtable stays in the read view (self._flushing)
            # until its segment joins the segment list.
            self._memtable = Memtable()
            self._flushing = frozen
            old_wal = self._wal
            old_wal_name = self._wal_name
            self._wal = WalWriter(os.path.join(self._dir, wal_name))
            self._wal_name = wal_name
        # Write the segment outside the lock; the frozen memtable is private.
        writer = SegmentWriter(os.path.join(self._dir, seg_name + ".tmp"), self._block_size)
        try:
            for key, versions in frozen.items_snapshot():
                for commit, value in reversed(versions):  # newest commit first
                    writer.add(key, commit, value)
            meta = writer.finish(os.path.join(self._dir, seg_name))
        except Exception:
            writer.abort()
            with self._lock:
                # Roll the frozen batches back into the live memtable and
                # restore the old WAL, so the failed flush changes nothing.
                restored = Memtable()
                for source in (frozen, self._memtable):
                    for key, versions in source.items_snapshot():
                        for commit, value in versions:
                            restored.apply_batch(commit, [(key, value)])
                self._memtable = restored
                self._flushing = None
                self._wal.close()
                try:
                    os.remove(os.path.join(self._dir, wal_name))
                except OSError:
                    pass
                self._wal = old_wal
                self._wal_name = old_wal_name
            raise
        self._pause_point(
            "flush_segment_written", {"segment": meta.to_json(), "commit": frozen_commit}
        )
        manifest = Manifest(
            commit=frozen_commit,
            next_file_id=next_file_id,
            wal=wal_name,
            segments=[meta] + [reader.meta for reader in self._segments],
        )
        self._store_manifest(manifest)
        self._pause_point(
            "flush_manifest_switched",
            {"segment": meta.to_json(), "commit": frozen_commit, "retired_wal": old_wal_name},
        )
        with self._lock:
            reader = SegmentReader.open(os.path.join(self._dir, seg_name), expected=meta)
            self._segments.insert(0, reader)
            self._all_readers.append(reader)
            self._flushing = None
        # The old WAL's frames are all inside the new segment; a crash before
        # this removal leaves an unreferenced file that the next open cleans.
        old_wal.close()
        try:
            os.remove(os.path.join(self._dir, old_wal_name))
        except OSError:
            pass
        return meta.to_json()

    def compact(self) -> dict:
        """Merge all on-disk segments into one, reclaiming dead versions.

        For every key, the merged segment keeps the newest version at or
        below each currently pinned commit (active snapshots and iterators)
        plus the newest version overall; everything else — overwritten values
        and tombstones that can no longer hide anything — is dropped.  A
        tombstone is never dropped while an older version of the same key is
        kept, so values cannot resurrect.  Foreground readers keep a
        consistent view throughout: the merged segment replaces the old ones
        only after it is complete, fsynced and the manifest switch succeeded,
        and old files are removed once the last pin on them is released.
        """
        with self._write_lock:
            with self._lock:
                self._check_open()
                self._check_writable()
                readers = list(self._segments)
                protected = self._protected_commits_locked()
                seg_id = self._alloc_file_id_locked()
                next_file_id = self._next_file_id
            # The readers stay live (and therefore undeletable) until the
            # manifest switch inside the merge; snapshots and iterators pin
            # them separately, so no extra pin is needed here.
            return self._compact_merge(readers, protected, seg_id, next_file_id)

    def _compact_merge(self, readers, protected, seg_id, next_file_id) -> dict:
        records_before = sum(reader.record_count for reader in readers)
        bytes_before = sum(reader.meta.bytes for reader in readers)
        if not readers:
            return {
                "segments_before": 0,
                "segments_after": 0,
                "records_before": 0,
                "records_after": 0,
                "bytes_before": 0,
                "bytes_after": 0,
                "protected_commits": protected,
                "deleted_files": [],
                "pending_delete_files": [],
            }
        seg_name = fmt.segment_name(seg_id)
        writer = SegmentWriter(os.path.join(self._dir, seg_name + ".tmp"), self._block_size)
        written = 0
        try:
            for key, versions in _merge_segment_records(readers):
                kept = _kept_versions(versions, protected)
                for commit, value in kept:
                    writer.add(key, commit, value)
                    written += 1
            if written:
                meta = writer.finish(os.path.join(self._dir, seg_name))
            else:
                writer.abort()
                meta = None
        except Exception:
            writer.abort()
            raise
        self._pause_point(
            "compact_segment_written",
            {"segment": meta.to_json() if meta else None, "records": written},
        )
        # manifest.commit means "batches up to this number are included in
        # the listed segments".  Compaction only rewrites segments — batches
        # still sitting in the WAL (not yet flushed) stay the WAL's
        # responsibility, so the covered commit does not move here.
        manifest = Manifest(
            commit=self._manifest.commit,
            next_file_id=next_file_id,
            wal=self._wal_name,
            segments=[meta] if meta else [],
        )
        self._store_manifest(manifest)
        self._pause_point(
            "compact_manifest_switched",
            {"segment": meta.to_json() if meta else None,
             "retired_segments": [reader.meta.file for reader in readers]},
        )
        deleted = []
        with self._lock:
            if meta is not None:
                new_reader = SegmentReader.open(os.path.join(self._dir, seg_name), expected=meta)
                self._segments = [new_reader]
                self._all_readers.append(new_reader)
            else:
                self._segments = []
            for reader in readers:
                reader.obsolete = True
            for reader in readers:
                path = reader.path
                self._maybe_delete_reader(reader)
                if not os.path.exists(path):
                    deleted.append(reader.meta.file)
            pending = [r.meta.file for r in self._all_readers if r.obsolete]
        return {
            "segments_before": len(readers),
            "segments_after": 1 if meta else 0,
            "records_before": records_before,
            "records_after": written,
            "bytes_before": bytes_before,
            "bytes_after": meta.bytes if meta else 0,
            "protected_commits": protected,
            "deleted_files": deleted,
            "pending_delete_files": pending,
        }

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict:
        """Sizes, record/version counts and file inventory of the store."""
        with self._lock:
            self._check_open()
            segments = []
            for reader in self._segments:
                segments.append(self._reader_stats(reader, obsolete=False))
            obsolete = [self._reader_stats(r, obsolete=True) for r in self._all_readers if r.obsolete]
            wal_bytes = 0
            if self._wal_name is not None:
                wal_path = os.path.join(self._dir, self._wal_name)
                if os.path.exists(wal_path):
                    wal_bytes = os.path.getsize(wal_path)
                if self._wal is not None:
                    wal_bytes += self._wal.pending_bytes
            return {
                "commit": self._commit,
                "read_only": self._read_only,
                "memtable": {
                    "bytes": self._memtable.bytes_used,
                    "keys": self._memtable.key_count,
                    "records": self._memtable.record_count,
                    "budget": self._budget,
                },
                "wal": {"file": self._wal_name, "bytes": wal_bytes},
                "segments": segments,
                "segment_count": len(segments),
                "total_segment_bytes": sum(s["bytes"] for s in segments),
                "total_segment_records": sum(s["records"] for s in segments),
                "obsolete_segments": obsolete,
                "protected_commits": self._protected_commits_locked(),
                "deleted_files_total": self.deleted_files,
            }

    @staticmethod
    def _reader_stats(reader: SegmentReader, obsolete: bool) -> dict:
        return {
            "file": reader.meta.file,
            "bytes": reader.meta.bytes,
            "records": reader.meta.records,
            "min_key_hex": reader.meta.min_key.hex(),
            "max_key_hex": reader.meta.max_key.hex(),
            "min_commit": reader.meta.min_commit,
            "max_commit": reader.meta.max_commit,
            "index_entries": reader.index_entries,
            "pinned_by_readers": reader.pins,
            "obsolete": obsolete,
        }

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        with self._write_lock:  # wait out any in-flight write/flush/compact
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                wal = self._wal
                self._wal = None
                readers = list(self._all_readers)
                self._all_readers = []
                self._segments = []
            if wal is not None:
                wal.close()
            for reader in readers:
                # Obsolete, unpinned files are still removed; live segment files
                # stay on disk (they belong to the committed store).
                if reader.obsolete and reader.pins <= 0:
                    try:
                        os.remove(reader.path)
                    except OSError:
                        pass
                reader.close()
            self._release_lock()

    def __enter__(self) -> "StagingStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _kept_versions(versions_desc, protected):
    """Versions of one key that a compacted segment must keep.

    ``versions_desc`` is ``[(commit, value|None), ...]`` newest first.  For
    every protected commit we keep the newest version at or below it; a
    tombstone that is the *only* kept version is dropped entirely (no older
    version exists anywhere that it still needs to hide).
    """
    kept_commits = set()
    for point in protected:
        for commit, _ in versions_desc:
            if commit <= point:
                kept_commits.add(commit)
                break
    kept = [(commit, value) for commit, value in versions_desc if commit in kept_commits]
    if len(kept) == 1 and kept[0][1] is None:
        return []
    return kept


def _merge_segment_records(readers):
    """Yield ``(key, versions_desc)`` groups merged across segment readers.

    Streams one block per reader at a time; never loads a whole segment.
    """
    sources = [_RecordStream(reader) for reader in readers]
    heap = []
    for index, source in enumerate(sources):
        group = source.next_group()
        if group is not None:
            heapq.heappush(heap, (group[0], index, group[1]))
    while heap:
        key = heap[0][0]
        versions = []
        while heap and heap[0][0] == key:
            _, index, group_versions = heapq.heappop(heap)
            versions.extend(group_versions)
            nxt = sources[index].next_group()
            if nxt is not None:
                heapq.heappush(heap, (nxt[0], index, nxt[1]))
        versions.sort(key=lambda item: item[0], reverse=True)
        yield key, versions


class _RecordStream:
    """Streams ``(key, [(commit, value), ...])`` groups from one segment."""

    def __init__(self, reader: SegmentReader, start_key: Optional[bytes] = None,
                 on_buffer=None):
        self._reader = reader
        if start_key is None:
            self._block_index = 0
        else:
            found = reader.find_block_index(start_key)
            # ``None`` means start_key precedes the first block's first key;
            # a range scan then simply starts at the first block (unlike a
            # point lookup, where it would mean "key cannot be present").
            self._block_index = found if found is not None else 0
        self._start_key = start_key
        self._records: Optional[list] = None
        self._pos = 0
        self._held_bytes = 0
        self._on_buffer = on_buffer

    def _release_block(self) -> None:
        if self._records is not None:
            if self._on_buffer is not None:
                self._on_buffer(-self._held_bytes)
            self._records = None
            self._held_bytes = 0

    def _load_next_block(self) -> bool:
        self._release_block()
        if self._block_index >= self._reader.index_entries:
            return False
        records, length = self._reader.read_block(self._block_index)
        self._block_index += 1
        self._records = records
        self._pos = 0
        self._held_bytes = length
        if self._on_buffer is not None:
            self._on_buffer(length)
        return True

    def next_group(self):
        while True:
            if self._records is None and not self._load_next_block():
                return None
            if self._pos >= len(self._records):
                self._release_block()
                continue
            key, commit, value = self._records[self._pos]
            if self._start_key is not None and key < self._start_key:
                self._pos += 1
                continue
            group = [(commit, value)]
            self._pos += 1
            while self._pos < len(self._records) and self._records[self._pos][0] == key:
                _, commit, value = self._records[self._pos]
                group.append((commit, value))
                self._pos += 1
            return key, group

    def close(self) -> None:
        self._release_block()


class Snapshot:
    """A read-only view of the store pinned at one commit number.

    ``get``/``scan`` on a snapshot always see the store as of
    :attr:`commit_number`, no matter what is written, flushed or compacted
    afterwards.  The snapshot holds read protection on the segment files of
    its view; they are released by :meth:`close` (or the context manager).
    Using a closed snapshot raises :class:`ClosedError`.
    """

    def __init__(self, store: StagingStore, commit: int, memtables, readers):
        self._store = store
        self._commit = commit
        self._memtables = list(memtables)
        self._readers = readers
        self._closed = False

    @property
    def commit_number(self) -> int:
        return self._commit

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise ClosedError("snapshot is closed")
        if self._store._closed:
            raise ClosedError("the owning store is closed")

    def get(self, key: bytes):
        key = _as_bytes(key, "key")
        store = self._store
        with store._lock:
            self._check_open()
            mem_result = MISSING
            for memtable in self._memtables:
                mem_result = memtable.get(key, self._commit)
                if mem_result is not MISSING:
                    break
        return store._get_in_segments(mem_result, self._readers, key, self._commit, None)

    def scan(self, lower: Optional[bytes] = None, upper: Optional[bytes] = None) -> "ScanIterator":
        store = self._store
        lower, upper = store._check_bounds(lower, upper)
        with store._lock:
            self._check_open()
            items = [mt.items_snapshot(lower) for mt in self._memtables]
            readers = list(self._readers)
            store._pin(readers)
            try:
                iterator = ScanIterator(store, items, readers, self._commit, lower, upper)
            except Exception:
                store._unpin(readers)
                raise
            store._iterators.add(iterator)
            return iterator

    def close(self) -> None:
        if self._closed:
            return
        store = self._store
        with store._lock:
            if self._closed:
                return
            self._closed = True
            store._snapshots.discard(self)
            store._unpin(self._readers)

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):  # best effort; explicit close() is the contract
        try:
            self.close()
        except Exception:
            pass


class ScanIterator:
    """Merges the memtable view and the pinned segments, newest commit wins.

    Memory is bounded: one data block per segment plus the (budget-bounded)
    memtable copy.  Closing early — or exhausting the iterator — releases
    the file pins and read protection it holds.
    """

    def __init__(self, store: StagingStore, memtable_items_list, readers, commit, lower, upper):
        self._store = store
        self._readers = readers
        self._commit = commit
        self._upper = upper
        self._closed = False
        self._exhausted = False
        self.records_scanned = 0
        self.keys_emitted = 0
        self._buffered_bytes = sum(
            len(key) + sum(len(v) if v is not None else 0 for _, v in versions)
            for items in memtable_items_list
            for key, versions in items
        )
        self.peak_buffered_bytes = self._buffered_bytes
        self._sources = []
        self._heap = []
        for items in memtable_items_list:
            self._sources.append(_MemtableGroupSource(items))
        for reader in readers:
            self._sources.append(
                _RecordStream(reader, start_key=lower, on_buffer=self._buffer_delta)
            )
        for index, source in enumerate(self._sources):
            group = source.next_group()
            if group is not None:
                heapq.heappush(self._heap, (group[0], index, group[1]))

    def _buffer_delta(self, delta: int) -> None:
        self._buffered_bytes += delta
        if self._buffered_bytes > self.peak_buffered_bytes:
            self.peak_buffered_bytes = self._buffered_bytes

    @property
    def commit_number(self) -> int:
        return self._commit

    @property
    def closed(self) -> bool:
        return self._closed

    def __iter__(self) -> "ScanIterator":
        return self

    def __next__(self):
        if self._closed:
            if self._exhausted:
                raise StopIteration
            raise ClosedError("scan iterator is closed")
        if self._store._closed:
            raise ClosedError("the owning store is closed")
        while self._heap:
            key = self._heap[0][0]
            if self._upper is not None and key >= self._upper:
                break
            best_commit = -1
            best_value = None
            while self._heap and self._heap[0][0] == key:
                _, index, versions = heapq.heappop(self._heap)
                for version_commit, value in versions:
                    self.records_scanned += 1
                    if self._commit >= version_commit > best_commit:
                        best_commit, best_value = version_commit, value
                nxt = self._sources[index].next_group()
                if nxt is not None:
                    heapq.heappush(self._heap, (nxt[0], index, nxt[1]))
            if best_commit >= 0 and best_value is not None:
                self.keys_emitted += 1
                return key, best_value
        self._exhausted = True
        self.close()
        raise StopIteration

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for source in self._sources:
            close = getattr(source, "close", None)
            if close is not None:
                close()
        store = self._store
        with store._lock:
            store._iterators.discard(self)
            store._unpin(self._readers)

    def __enter__(self) -> "ScanIterator":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):  # best effort; explicit close() is the contract
        try:
            self.close()
        except Exception:
            pass


class _MemtableGroupSource:
    def __init__(self, items):
        self._items = items
        self._index = 0

    def next_group(self):
        if self._index >= len(self._items):
            return None
        key, versions = self._items[self._index]
        self._index += 1
        # Memtable versions ascend by commit; present newest first.
        return key, [(commit, value) for commit, value in reversed(versions)]

    def close(self) -> None:
        self._items = []
