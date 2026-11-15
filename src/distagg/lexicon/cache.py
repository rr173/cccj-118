"""A tiny fixed-capacity LRU cache with honest access statistics.

Both the library compiler and the scanner keep their hot automaton data
(transitions, state info, per-state outputs) in instances of this class.
The capacity is fixed at construction and never exceeded, so the Python
footprint of the automaton is bounded by configuration — not by the size
of the dictionary.  ``hits``, ``misses`` and ``peak`` are real counters,
reported in compile/scan summaries.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Hashable, Optional


class BoundedCache:
    """Least-recently-used cache with a hard entry cap and counters."""

    __slots__ = ("capacity", "hits", "misses", "peak", "_data")

    def __init__(self, capacity: int):
        if isinstance(capacity, bool) or not isinstance(capacity, int) \
                or capacity < 1:
            raise ValueError(
                f"cache capacity must be a positive integer, got {capacity!r}")
        self.capacity = capacity
        self.hits = 0
        self.misses = 0
        self.peak = 0
        self._data: "OrderedDict[Hashable, Any]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: Hashable, default: Any = None) -> Any:
        try:
            value = self._data.pop(key)
        except KeyError:
            self.misses += 1
            return default
        self._data[key] = value  # reinsert as most recently used
        self.hits += 1
        return value

    def put(self, key: Hashable, value: Any) -> None:
        if key in self._data:
            self._data.pop(key)
        elif len(self._data) >= self.capacity:
            self._data.popitem(last=False)
        self._data[key] = value
        if len(self._data) > self.peak:
            self.peak = len(self._data)

    def get_or_load(self, key: Hashable, loader) -> Any:
        """Return ``cache[key]``, calling ``loader()`` and caching on miss.

        ``None`` is a legitimate cached value here (a negative transition
        result), so misses are detected with a sentinel, not by value.
        """
        sentinel = object()
        value = self.get(key, sentinel)
        if value is sentinel:
            value = loader()
            self.put(key, value)
        return value

    def stats(self) -> dict:
        return {
            "capacity": self.capacity,
            "hits": self.hits,
            "misses": self.misses,
            "peak_entries": self.peak,
        }
