"""Streaming interval aggregates and the bounded row cache.

The engine asks, for every output row and every frame, for the five metrics
over one (or two) inclusive position interval(s) inside the current group.
Naively rescanning each interval is quadratic.  Instead every monotonic
sequence of intervals gets one :class:`IntervalAggregator`:

* interval bounds only move forward over the sequence of queries, so a pair
  of cursors visits every group position at most once;
* sum and the non-null count are running counters updated while the cursors
  move;
* min/max are maintained with monotonic deques
  (:mod:`distagg.windowing.deque`) whose entries are (position, value);
* values are fetched through :class:`RowCache`, a forward read-through cache
  backed by the SQLite scratch table that never holds more than one batch.

Everything is pure Python integer arithmetic, so arbitrarily large values
are handled exactly and SQLite's 64-bit sum limitation never comes into
play.  Memory is bounded by the batch/spill thresholds regardless of group
or window size.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from .deque import SpillableDeque


class RowCache:
    """Bounded sliding cache for rows of one group.

    ``fetch(pos)`` returns ``(value_or_none, raw_json)``.  The streaming
    cursors mostly move right, so misses past the high watermark pull one
    fixed-size batch (the window never holds more than two batches).  A
    position that has already been evicted (a lagging cursor) is served with
    a single indexed point query against the scratch table instead of being
    re-buffered, so neither a huge RANGE frame nor a jump can force the whole
    group into memory.
    """

    def __init__(self, db, part_blob: bytes, batch_size: int):
        self._db = db
        self._part = part_blob
        self._batch_size = max(1, int(batch_size))
        self._low = 1                       # first position still cached
        self._high = 0                      # last position loaded
        self._values: List[Any] = []        # index pos - self._low
        self._raws: List[str] = []

    def _point_query(self, pos: int):
        row = self._db.execute(
            "SELECT value_text, raw_json FROM records "
            "WHERE part_key = ? AND pos = ?",
            (self._part, pos),
        ).fetchone()
        if row is None:
            raise IndexError(f"position {pos} does not exist in this group")
        value_text, raw_json = row
        return (int(value_text) if value_text is not None else None), raw_json

    def _load_batch(self, pos: int) -> None:
        rows = self._db.execute(
            "SELECT pos, value_text, raw_json FROM records "
            "WHERE part_key = ? AND pos > ? ORDER BY pos LIMIT ?",
            (self._part, self._high, self._batch_size),
        ).fetchall()
        if not rows:
            return
        if self._high == 0:
            self._low = rows[0][0]
        for _p, value_text, raw_json in rows:
            self._values.append(int(value_text) if value_text is not None else None)
            self._raws.append(raw_json)
            self._high = _p
        # Keep at most two batches resident.
        overflow = len(self._values) - 2 * self._batch_size
        if overflow > 0:
            self._values = self._values[overflow:]
            self._raws = self._raws[overflow:]
            self._low += overflow

    def fetch(self, pos: int):
        if self._low <= pos <= self._high:
            index = pos - self._low
            return self._values[index], self._raws[index]
        if pos > self._high:
            self._load_batch(pos)
            if self._low <= pos <= self._high:
                index = pos - self._low
                return self._values[index], self._raws[index]
        # Evicted or beyond the loaded batch: one indexed point lookup.
        return self._point_query(pos)

    def release_before(self, watermark: int) -> None:
        drop = watermark - self._low
        if drop > 0:
            self._values = self._values[drop:]
            self._raws = self._raws[drop:]
            self._low = watermark


class IntervalAggregator:
    """Monotonic-cursor aggregator for inclusive ``[left, right]`` intervals.

    Both endpoints of successive queries must be non-decreasing.  Empty
    intervals (``left > right``) may be passed; they skip the update and
    return the empty-aggregate shape.
    """

    def __init__(self, db, deque_id_start: int, row_cache: RowCache,
                 spill_threshold: int):
        self._cache = row_cache
        self._left = 0
        self._right = 0
        self._count_all = 0
        self._count_value = 0
        self._sum = 0
        self._min_deque = SpillableDeque(db, deque_id_start, spill_threshold)
        self._max_deque = SpillableDeque(db, deque_id_start + 1, spill_threshold)

    # -- monotonic primitives -------------------------------------------
    def _advance_right_to(self, target: int) -> None:
        """Grow the right cursor so positions <= ``target`` have been seen."""
        while self._right < target:
            self._right += 1
            value, _raw = self._cache.fetch(self._right)
            self._count_all += 1
            if value is None:
                continue
            self._count_value += 1
            self._sum += value
            # Monotonic max deque: discard tail values dominated by the new
            # one; each position is inserted and removed at most once.
            while self._max_deque and self._max_deque.peek_tail()[1] <= value:
                self._max_deque.pop_right()
            self._max_deque.push(self._right, value)
            # Monotonic min deque.
            while self._min_deque and self._min_deque.peek_tail()[1] >= value:
                self._min_deque.pop_right()
            self._min_deque.push(self._right, value)

    def _advance_left_to(self, removed: int) -> None:
        """Shrink from the left so positions <= ``removed`` leave the window.

        The maintained membership is the half-open range ``(L, R]``; callers
        pass ``left - 1`` to realize an inclusive ``[left, right]`` interval.
        """
        while self._left < removed:
            self._left += 1
            value, _raw = self._cache.fetch(self._left)
            self._count_all -= 1
            if self._min_deque.head_position == self._left:
                self._min_deque.pop_left()
            if self._max_deque.head_position == self._left:
                self._max_deque.pop_left()
            if value is None:
                continue
            self._count_value -= 1
            self._sum -= value

    def update(self, left: int, right: int) -> Tuple[int, int, Optional[int],
                                                     Optional[int], Optional[int]]:
        if left > right:
            return 0, 0, None, None, None
        # Included positions are (L, R]; moving L to left-1 makes the
        # effective window the inclusive interval [left, right].
        removed = left - 1
        if removed < self._left or right < self._right:
            raise AssertionError(
                "interval endpoints must be non-decreasing across queries"
            )
        self._advance_right_to(right)
        self._advance_left_to(removed)
        # Cache eviction is driven by the engine once all streams have been
        # queried for the row, since streams may sit at different low marks.
        if self._count_value == 0:
            minimum = maximum = None
        else:
            minimum = self._min_deque.peek_head()[1]
            maximum = self._max_deque.peek_head()[1]
        return self._count_all, self._count_value, self._sum, minimum, maximum

    def reset(self) -> None:
        """Drop all state when the engine starts a new partition group."""
        self._left = 0
        self._right = 0
        self._count_all = 0
        self._count_value = 0
        self._sum = 0
        self._min_deque.clear()
        self._max_deque.clear()
