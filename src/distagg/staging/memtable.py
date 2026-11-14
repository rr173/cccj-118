"""In-memory write buffer (memtable) for the staging store.

The memtable is a multi-version map ``key -> [(commit, value|None), ...]``
with versions in ascending commit order (batches only ever append, and
commit numbers increase).  It is bounded by a configurable byte budget;
when a batch pushes it past the budget the store flushes it to a segment.
A single over-budget batch may exceed the budget transiently, but the store
flushes immediately after that batch completes, so the buffer cannot grow
without bound.

Readers never iterate the live dict directly: scans take a copied, sorted
item list under the store lock, and point lookups are single dict probes.
After a flush the store simply drops its reference; snapshots and iterators
that still hold the frozen memtable keep it alive as long as they need.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

#: Sentinel for "the memtable holds no version of this key at or below the
#: requested commit" — distinct from ``None`` which is a deletion tombstone.
MISSING = object()

# Rough per-record bookkeeping overhead on top of raw key/value bytes.
RECORD_OVERHEAD = 24


class Memtable:
    def __init__(self) -> None:
        self._versions = {}  # bytes -> list[(commit, value|None)], commit ascending
        self.bytes_used = 0
        self.record_count = 0

    def apply_batch(self, commit: int, ops) -> None:
        """Append one committed batch; ``ops`` are already collapsed per key."""
        for key, value in ops:
            versions = self._versions.get(key)
            if versions is None:
                versions = self._versions[key] = []
            versions.append((commit, value))
            self.record_count += 1
            self.bytes_used += RECORD_OVERHEAD + len(key) + (len(value) if value is not None else 0)

    def get(self, key: bytes, commit: int):
        """Newest version at or below ``commit``: bytes, ``None`` (tombstone) or MISSING."""
        versions = self._versions.get(key)
        if not versions:
            return MISSING
        for version_commit, value in reversed(versions):
            if version_commit <= commit:
                return value
        return MISSING

    def items_snapshot(self, lower: Optional[bytes] = None) -> List[Tuple[bytes, list]]:
        """A stable, sorted copy of ``(key, versions)`` pairs for merging.

        Called under the store lock, so the copy cannot race with
        ``apply_batch``.  Bounded by the memtable budget (plus at most one
        over-budget batch), never by the size of the whole store.
        """
        items = sorted(self._versions.items())
        if lower is not None:
            items = [item for item in items if item[0] >= lower]
        return [(key, list(versions)) for key, versions in items]

    @property
    def key_count(self) -> int:
        return len(self._versions)
