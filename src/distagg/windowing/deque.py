"""Bounded-memory deque used by the monotonic min/max aggregators.

A plain in-memory :class:`collections.deque` could grow without limit on a
very long window (for example one RANGE frame covering a whole giant group).
:class:`SpillableDeque` keeps entries in memory only up to a configurable
threshold; once exceeded it moves the whole deque into a SQLite table inside
the scratch database and continues with SQL head/tail operations.

Each deque has a stable numeric id; all spilled deques share one table.  The
Python-side entry count and head position are always tracked in memory, so
length and emptiness checks never touch the disk.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Optional, Tuple


class SpillableDeque:
    """A deque of ``(position, value)`` pairs with a hard in-memory ceiling."""

    def __init__(self, db, deque_id: int, spill_threshold: int):
        self._db = db
        self._id = deque_id
        self._threshold = max(1, int(spill_threshold))
        self._memory: Deque[Tuple[int, Any]] = deque()
        self._spilled = False
        self._count = 0
        self._head_pos: Optional[int] = None
        self._tail_pos: Optional[int] = None
        self._seq = 0  # monotonic insertion sequence while spilled

    # -- properties ------------------------------------------------------
    def __len__(self) -> int:
        return self._count

    def __bool__(self) -> int:
        return self._count > 0

    @property
    def spilled(self) -> bool:
        return self._spilled

    @property
    def head_position(self) -> Optional[int]:
        return self._head_pos

    @property
    def tail_position(self) -> Optional[int]:
        return self._tail_pos

    # -- internals -------------------------------------------------------
    def _spill(self) -> None:
        rows = [
            (self._id, seq, pos, str(value) if value is not None else None)
            for seq, (pos, value) in enumerate(self._memory)
        ]
        self._db.executemany(
            "INSERT INTO deque_store(deque_id, seq, pos, value_text) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        self._seq = len(self._memory)
        self._memory.clear()
        self._spilled = True

    def _ensure_capacity(self) -> None:
        if not self._spilled and self._count >= self._threshold:
            self._spill()

    # -- deque operations ------------------------------------------------
    def push(self, pos: int, value: Any) -> None:
        self._ensure_capacity()
        if self._spilled:
            self._db.execute(
                "INSERT INTO deque_store(deque_id, seq, pos, value_text) "
                "VALUES (?, ?, ?, ?)",
                (self._id, self._seq, pos,
                 str(value) if value is not None else None),
            )
            self._seq += 1
        else:
            self._memory.append((pos, value))
        self._count += 1
        self._tail_pos = pos
        if self._head_pos is None:
            self._head_pos = pos

    def pop_left(self) -> Tuple[int, Any]:
        if self._count == 0:
            raise IndexError("pop from an empty SpillableDeque")
        if self._spilled:
            row = self._db.execute(
                "SELECT pos, value_text FROM deque_store "
                "WHERE deque_id = ? ORDER BY seq LIMIT 1",
                (self._id,),
            ).fetchone()
            self._db.execute(
                "DELETE FROM deque_store WHERE deque_id = ? AND seq = ("
                "SELECT seq FROM deque_store WHERE deque_id = ? ORDER BY seq LIMIT 1)",
                (self._id, self._id),
            )
            pos, value_text = row
            value = int(value_text) if value_text is not None else None
        else:
            pos, value = self._memory.popleft()
        self._count -= 1
        if self._count == 0:
            self._head_pos = None
            self._tail_pos = None
        else:
            self._head_pos = self._peek_position("head")
        return pos, value

    def pop_right(self) -> Tuple[int, Any]:
        if self._count == 0:
            raise IndexError("pop from an empty SpillableDeque")
        if self._spilled:
            row = self._db.execute(
                "SELECT pos, value_text FROM deque_store "
                "WHERE deque_id = ? ORDER BY seq DESC LIMIT 1",
                (self._id,),
            ).fetchone()
            self._db.execute(
                "DELETE FROM deque_store WHERE deque_id = ? AND seq = ("
                "SELECT seq FROM deque_store WHERE deque_id = ? "
                "ORDER BY seq DESC LIMIT 1)",
                (self._id, self._id),
            )
            pos, value_text = row
            value = int(value_text) if value_text is not None else None
        else:
            pos, value = self._memory.pop()
        self._count -= 1
        if self._count == 0:
            self._head_pos = None
            self._tail_pos = None
        else:
            self._tail_pos = self._peek_position("tail")
        return pos, value

    def _peek_position(self, side: str) -> Optional[int]:
        if self._count == 0:
            return None
        order = "ASC" if side == "head" else "DESC"
        if self._spilled:
            row = self._db.execute(
                f"SELECT pos FROM deque_store WHERE deque_id = ? "
                f"ORDER BY seq {order} LIMIT 1",
                (self._id,),
            ).fetchone()
            return row[0]
        pair = self._memory[0] if side == "head" else self._memory[-1]
        return pair[0]

    def peek_head(self) -> Tuple[int, Any]:
        if self._count == 0:
            raise IndexError("peek on an empty SpillableDeque")
        if self._spilled:
            pos, text = self._db.execute(
                "SELECT pos, value_text FROM deque_store "
                "WHERE deque_id = ? ORDER BY seq LIMIT 1",
                (self._id,),
            ).fetchone()
            return pos, (int(text) if text is not None else None)
        return self._memory[0]

    def peek_tail(self) -> Tuple[int, Any]:
        if self._count == 0:
            raise IndexError("peek on an empty SpillableDeque")
        if self._spilled:
            pos, text = self._db.execute(
                "SELECT pos, value_text FROM deque_store "
                "WHERE deque_id = ? ORDER BY seq DESC LIMIT 1",
                (self._id,),
            ).fetchone()
            return pos, (int(text) if text is not None else None)
        return self._memory[-1]

    def clear(self) -> None:
        if self._spilled:
            self._db.execute("DELETE FROM deque_store WHERE deque_id = ?", (self._id,))
        self._memory.clear()
        self._count = 0
        self._head_pos = None
        self._tail_pos = None
        self._seq = 0
