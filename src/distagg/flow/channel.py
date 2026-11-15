"""Bounded single-consumer asynchronous channels used between nodes.

Design
------
Every edge owns one :class:`Channel`.  A channel is a small fixed-size ring of
record slots plus two asyncio futures: one waiter for "a record became
available" and one for "a slot became free".  Its entire memory footprint is
``capacity`` record references — there is deliberately **no**

* unbounded ``asyncio.Queue``,
* ``put_nowait`` that silently drops or grows,
* task per record,
* background copying/feeding task.

A producer does not get its own buffer: it pushes a record into the channel
and waits for the consumer to actually take it out.  The ring slot is only
released by :meth:`Channel.get`, so "how many records are assigned to the
downstream node but not yet consumed" is exactly ``len < capacity`` at every
instant.

Back-pressure therefore travels from a slow sink back through every channel
to the source.  A source only reads the next physical line after its outgoing
edge accepted the previous record.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from .errors import FlowCancelled


class Channel:
    """One capacity-limited record channel."""

    __slots__ = (
        "source", "target", "capacity", "sent", "received", "max_occupied",
        "closed", "_ring", "_head", "_size", "_put_waiter", "_get_waiter",
        "_error", "event",
    )

    def __init__(self, source: str, target: str, capacity: int):
        self.source = source
        self.target = target
        self.capacity = capacity
        self.sent = 0
        self.received = 0
        self.max_occupied = 0
        self.closed = False
        self._ring: List[Any] = [None] * capacity
        self._head = 0
        self._size = 0
        self._put_waiter: Optional[asyncio.Future] = None
        self._get_waiter: Optional[asyncio.Future] = None
        self._error: Optional[BaseException] = None
        # Optional diagnostic: an asyncio.Event set on every occupancy change.
        # It is created lazily by pause hooks, never used by the fast path.
        self.event: Optional[asyncio.Event] = None

    # -- introspection ---------------------------------------------------- #

    @property
    def occupied(self) -> int:
        return self._size

    def stats(self) -> Dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "capacity": self.capacity,
            "sent": self.sent,
            "received": self.received,
            "peak_occupancy": self.max_occupied,
        }

    # -- normal operation -------------------------------------------------- #

    def _wake(self, waiter_name: str) -> None:
        waiter = getattr(self, waiter_name)
        if waiter is not None:
            if not waiter.done():
                waiter.set_result(None)
            setattr(self, waiter_name, None)
        if self.event is not None:
            self.event.set()

    async def put(self, value: Any) -> None:
        """Block until the record is in the ring; never overfills it."""
        while True:
            self._check_error()
            if self.closed:  # pragma: no cover - defensive
                raise FlowCancelled("channel closed while sending")
            if self._size < self.capacity:
                index = (self._head + self._size) % self.capacity
                self._ring[index] = value
                self._size += 1
                self.sent += 1
                if self._size > self.max_occupied:
                    self.max_occupied = self._size
                self._wake("_get_waiter")
                return
            waiter = asyncio.get_running_loop().create_future()
            self._put_waiter = waiter
            await waiter
            self._put_waiter = None

    async def get(self) -> Any:
        """Take the next record, blocking while the ring is empty."""
        while True:
            self._check_error()
            if self._size:
                value = self._ring[self._head]
                self._ring[self._head] = None
                self._head = (self._head + 1) % self.capacity
                self._size -= 1
                self.received += 1
                self._wake("_put_waiter")
                return value
            if self.closed:
                raise self._closed_error()
            waiter = asyncio.get_running_loop().create_future()
            self._get_waiter = waiter
            await waiter
            self._get_waiter = None

    def close(self) -> None:
        """End-of-stream marker: the consumer drains what is buffered, then
        receives :class:`ChannelClosed` (a private nested type)."""
        if self.closed:
            return
        self.closed = True
        self._wake("_get_waiter")
        self._wake("_put_waiter")

    def is_drained(self) -> bool:
        return self.closed and self._size == 0

    # -- failure path ------------------------------------------------------ #

    def fail(self, error: BaseException) -> None:
        """Record the run's first failure and wake every waiter immediately.

        The buffered records are discarded: the run is over, its state is not
        reused, and keeping them alive would only delay cleanup.
        """
        if self._error is None:
            self._error = error
        self.closed = True
        self._ring = [None] * self.capacity
        self._size = 0
        self._head = 0
        self._wake("_get_waiter")
        self._wake("_put_waiter")
        if self.event is not None:
            self.event.set()

    def _check_error(self) -> None:
        if self._error is not None:
            raise self._error

    def _closed_error(self) -> BaseException:
        return ChannelClosed(self)


class ChannelClosed(Exception):
    """The channel is closed and fully drained (normal end of stream)."""

    def __init__(self, channel: Channel):
        super().__init__(f"channel {channel.source!r} -> {channel.target!r} closed")
        self.channel = channel
