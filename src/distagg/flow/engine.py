"""The asynchronous multi-node executor.

Lifecycle
---------
:func:`start_run` validates the graph and the configuration *synchronously*:
no source is opened, no result directory is created and no task exists until
the caller explicitly starts the handle.  ``await run.start()`` then

1. creates the fresh result directory (refusing to reuse an existing one),
2. installs one task per node, in topological order, in the *same* loop; each
   node opens its own source/sink file, so every node starts together,
3. lets each record flow through the graph before the next one is read
   upstream.  Nothing is pre-computed to disk before downstream starts.

Failure model
-------------
The first failure (source decode, operator error, sink write error) is
recorded once and every channel and credit pool is failed with that exception.
Waiters in ``Channel.get`` / ``Channel.put`` and in ``Run._acquire_credit``
are woken immediately, so no task remains blocked on a full or empty queue.
Nodes translate the broadcast into an orderly stop, close their files and
custom iterators and return.  The supervisor joins every task before the
summary is built, so ``RunSummary.error`` names the original node, the source
identity and the reason.

Cancellation uses the same path: ``run.cancel()``, cancelling the run task or
closing a result iterator broadcasts a stop, lets every node clean up, and
reports ``"cancelled"``.

Memory bound
------------
Two limits bound every buffer:

* each edge owns a fixed-size ring of ``capacity`` record references;
* each node has an *in-flight* allowance ``I`` (``max_inflight_per_node``):
  a producer takes one credit from the target's pool before pushing a record
  and the target returns it when it takes the record (sinks: after writing it).
  A paused sink therefore holds at most ``I`` taken-but-unwritten records, and
  its producers stop after at most ``I`` pushed-but-untaken records.

With ``C(e)`` the edge capacities and ``N`` the number of non-source nodes, a
run keeps at most

    sum over edges C(e)  +  N * I  +  result_capacity

record references alive, each encoded to at most ``max_record_bytes`` bytes
(plus the small identity envelope), one line buffer of at most
``max_line_bytes`` per source plus one read chunk, and one in-progress output
line per sink.  The bound is fixed when the run starts and does not grow with
the number of input lines; no per-record task is ever created.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .channel import Channel, ChannelClosed
from .config import (
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_MAX_RECORD_BYTES,
    FlowConfig,
    RunSummary,
)
from .errors import FlowCancelled, FlowError, FlowFailure, SourceRecordError
from .jsonline import decode_line
from .ops import make_explode, make_project, make_select
from .record import Record

RESULT_END = object()


class PauseGate:
    """A synchronous barrier in front of one node's input.

    While paused the node consumes no further records.  Data already accepted
    stays in bounded channels and credit pools, so back-pressure reaches the
    source.  ``resume()`` releases the node; nothing is dropped and no side
    branch is skipped.  ``wait_paused()`` is awaitable and resolves once the
    node is actually blocked at the barrier, which lets a test inspect
    ``run.snapshot()`` at exactly that moment.
    """

    def __init__(self, node_name: str):
        self.node_name = node_name
        self._event = asyncio.Event()
        self._event.set()
        self._paused = asyncio.Event()

    def pause(self) -> None:
        self._event.clear()

    def resume(self) -> None:
        self._event.set()

    @property
    def is_paused(self) -> bool:
        return not self._event.is_set()

    async def wait(self) -> None:
        if not self._event.is_set():
            self._paused.set()
            try:
                await self._event.wait()
            finally:
                self._paused.clear()
        else:
            await asyncio.sleep(0)

    def wake(self) -> None:
        """Open the barrier without recording a resume (used on failure)."""
        self._event.set()

    async def wait_paused(self) -> None:
        await self._paused.wait()


def _encoded_row(row: Dict[str, Any]) -> bytes:
    return json.dumps(row, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def record_size(record: Record) -> int:
    """Encoded size of the JSON row plus the source-identity envelope."""
    envelope = json.dumps(record.identity(), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    return len(_encoded_row(record.row)) + len(envelope)


def _clone_record(record: Record) -> Record:
    """Deep copy for broadcast: each branch owns its row and can mutate it."""
    return Record(record.source, record.line, record.expansion_path,
                  copy.deepcopy(record.row))


class _Node:
    """Shared node machinery: input channels, credits, lifecycle."""

    kind = "node"

    def __init__(self, run: "Run", name: str, spec):
        self.run = run
        self.name = name
        self.spec = spec
        self.in_edges: List[Channel] = run.in_edges.get(name, [])
        self.out_edges: List[Channel] = run.out_edges.get(name, [])
        self.gate: Optional[PauseGate] = run.gates.get(name)
        self.processed = 0
        self.emitted = 0

    # -- emission --------------------------------------------------------- #

    async def _produce(self, record: Record) -> None:
        """Hand one record to every outgoing edge.

        Several outgoing edges are a broadcast: the first successor gets the
        record itself, every further successor gets a deep copy, so a branch
        that mutates a nested object can never change what another branch has
        already received or is about to receive.
        """
        if not self.out_edges:
            return
        size = record_size(record)
        if size > self.run.max_record_bytes:
            raise FlowFailure(
                self.name,
                f"record is {size} bytes, exceeding max_record_bytes="
                f"{self.run.max_record_bytes}; records are never truncated",
                identity=record.identity(),
            )
        for index, channel in enumerate(self.out_edges):
            payload = record if index == 0 else _clone_record(record)
            await self.run._acquire_credit(channel.target)
            try:
                await channel.put(payload)
            except ChannelClosed:  # pragma: no cover - producer closed already
                raise FlowCancelled("output channel closed while sending")
            self.run._note_emitted(self.name, channel)
        self.emitted += 1

    def _accept(self, record: Record) -> None:
        self.processed += 1
        self.run._return_credit(self.name)

    async def _wait_gate(self) -> None:
        """Optional operations/test barrier in front of this node's input.

        A failure or cancellation elsewhere wakes the gate and the node
        returns at once instead of hanging on the barrier.
        """
        if self.gate is not None:
            await self.gate.wait()
        if self.run._failure is not None:
            raise self.run._failure
        if self.run._cancelled:
            raise FlowCancelled("run was cancelled by the caller")

    # -- lifecycle -------------------------------------------------------- #

    async def start(self) -> None:
        raise NotImplementedError

    async def finish(self) -> None:
        for channel in self.out_edges:
            channel.close()

    async def close(self) -> None:
        """Release file handles / iterators.  Must tolerate repeat calls."""

    def completion_state(self) -> Dict[str, Any]:
        return {}


# --------------------------------------------------------------------------- #
# source
# --------------------------------------------------------------------------- #

class SourceNode(_Node):
    kind = "source"

    def __init__(self, run: "Run", name: str, spec):
        super().__init__(run, name, spec)
        self.path = spec.options["path"]
        self.handle = None
        self.lines_pulled = 0
        self.lines_emitted = 0
        self.bytes_read = 0
        self.exhausted = False
        self._buffer = b""
        self._online = 0
        self._eof = False

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self.handle = await loop.run_in_executor(None, open, self.path, "rb")
        except OSError as exc:
            raise self.run._fail(self.name, "opening source", exc) from exc
        if self.run._failure is not None or self.run._cancelled:
            return
        limit = self.run.max_line_bytes
        while True:
            self.run._check_error()
            line = await self._next_line(limit)
            if line is None:
                break
            line_number, raw = line
            try:
                row = decode_line(raw, source=self.name,
                                  line_number=line_number, path=self.path)
            except SourceRecordError as exc:
                raise self.run._source_failure(exc) from exc
            record = Record(self.name, line_number, (), row)
            size = record_size(record)
            if size > self.run.max_record_bytes:
                raise FlowFailure(
                    self.name,
                    f"record is {size} bytes, exceeding "
                    f"max_record_bytes={self.run.max_record_bytes}; records "
                    "are never truncated to fit",
                    identity=record.identity(),
                )
            await self._produce(record)
            self.lines_emitted += 1
        self.exhausted = True

    async def _next_line(self, limit: int) -> Optional[Tuple[int, bytes]]:
        loop = asyncio.get_running_loop()
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = self._buffer[:newline]
                self._buffer = self._buffer[newline + 1:]
                return self._finish_line(raw, limit)
            if len(self._buffer) > limit + 1:
                # The partial line already exceeds the limit; do not keep
                # reading the rest of it into memory.
                self._buffer = b""
                raise FlowFailure(
                    self.name,
                    f"physical line {self._online + 1} exceeds "
                    f"max_line_bytes={limit}; the line is not truncated",
                    identity={"source": self.name, "line": self._online + 1,
                              "expansion_path": []},
                )
            if self._eof:
                if self._buffer:
                    raw = self._buffer
                    self._buffer = b""
                    return self._finish_line(raw, limit)
                return None
            chunk = await loop.run_in_executor(
                None, self.handle.read, self.run.config.source_read_chunk)
            if not chunk:
                self._eof = True
                continue
            self.bytes_read += len(chunk)
            self._buffer += chunk

    def _finish_line(self, raw: bytes, limit: int) -> Tuple[int, bytes]:
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        self._online += 1
        self.lines_pulled = self._online
        if len(raw) > limit:
            raise FlowFailure(
                self.name,
                f"physical line {self._online} is {len(raw)} bytes, exceeding "
                f"max_line_bytes={limit}",
                identity={"source": self.name, "line": self._online,
                          "expansion_path": []},
            )
        return self._online, raw

    async def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, handle.close)
            except OSError:
                pass

    def completion_state(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "lines_pulled": self.lines_pulled,
            "records_emitted": self.lines_emitted,
            "bytes_read": self.bytes_read,
            "exhausted": self.exhausted,
        }


# --------------------------------------------------------------------------- #
# one-input processing nodes
# --------------------------------------------------------------------------- #

class TransformNode(_Node):
    """select / project / explode: one input, lazily produced outputs."""

    def make_operator(self):
        raise NotImplementedError

    def __init__(self, run: "Run", name: str, spec):
        super().__init__(run, name, spec)
        self.apply = self.make_operator()

    async def start(self) -> None:
        channel = self.in_edges[0]
        while True:
            await self._wait_gate()
            try:
                record = await channel.get()
            except ChannelClosed:
                return
            except FlowError:
                return
            self._accept(record)
            try:
                for out in self.apply(record):
                    await self._produce(out)
            except (FlowError, asyncio.CancelledError):
                raise
            except BaseException as exc:  # noqa: BLE001
                raise self.run._fail(
                    self.name,
                    f"{self.kind} operator failed: {exc}",
                    exc,
                    identity=record.identity(),
                ) from exc


class SelectNode(TransformNode):
    kind = "select"

    def make_operator(self):
        opts = self.spec.options
        return make_select(opts["field"], opts["condition"], opts.get("value"))


class ProjectNode(TransformNode):
    kind = "project"

    def make_operator(self):
        return make_project(self.spec.options["fields"])


class ExplodeNode(TransformNode):
    kind = "explode"

    def make_operator(self):
        return make_explode(self.spec.options["field"])


class CustomNode(_Node):
    """Caller-supplied co-operative async one-to-many operator."""

    kind = "custom"

    def __init__(self, run: "Run", name: str, spec):
        super().__init__(run, name, spec)
        self.operator = spec.operator

    async def start(self) -> None:
        channel = self.in_edges[0]
        while True:
            await self._wait_gate()
            try:
                record = await channel.get()
            except ChannelClosed:
                return
            except FlowError:
                return
            self._accept(record)

            async def emit(out, _record=record):
                if not isinstance(out, Record):
                    raise TypeError(
                        "a custom operator must emit Record objects, got "
                        f"{type(out).__name__}")
                size = record_size(out)
                if size > self.run.max_record_bytes:
                    raise FlowFailure(
                        self.name,
                        f"custom operator emitted a {size}-byte record, "
                        f"exceeding max_record_bytes="
                        f"{self.run.max_record_bytes}; records are never "
                        "truncated",
                        identity=out.identity(),
                    )
                await self._produce(out)

            try:
                result = self.operator(record, emit)
                if hasattr(result, "__await__"):
                    result = await result
                # A custom operator may also return an async iterable of
                # Records (an async generator); each item is emitted as it is
                # produced and the iterator is closed on any exit path.
                if hasattr(result, "__aiter__"):
                    iterator = result.__aiter__()
                    try:
                        async for item in iterator:
                            await emit(item)
                    finally:
                        aclose = getattr(iterator, "aclose", None)
                        if aclose is not None:
                            await aclose()
                elif result is not None and not isinstance(result, bool):
                    raise TypeError(
                        "a custom operator must return None or an async "
                        f"iterable of records, got {type(result).__name__}")
            except (FlowError, asyncio.CancelledError):
                raise
            except BaseException as exc:  # noqa: BLE001
                raise self.run._fail(
                    self.name,
                    f"custom operator failed: {exc}",
                    exc,
                    identity=record.identity(),
                ) from exc


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #

class MergeNode(_Node):
    """Fair round-robin merge of several inputs.

    * order inside one input channel is never changed;
    * no global order between inputs is imposed;
    * an input that is momentarily empty never blocks another ready input: the
      loop always drains any buffered input before waiting;
    * the same source record arriving over two paths is delivered twice — no
      identity-based de-duplication happens here;
    * end-of-stream is per input: a source that exhausts closes only its own
      channel; the merge finishes only after *all* inputs delivered their end
      marker and their buffered records (an empty input's end marker takes
      part in the merge like any other).
    """

    kind = "merge"

    def __init__(self, run: "Run", name: str, spec):
        super().__init__(run, name, spec)
        self._cursor = 0

    async def start(self) -> None:
        channels = list(self.in_edges)
        ended = [False] * len(channels)
        while not all(ended):
            await self._wait_gate()
            progressed = False
            for offset in range(len(channels)):
                index = (self._cursor + offset) % len(channels)
                channel = channels[index]
                if ended[index]:
                    continue
                if channel.occupied:
                    record = await channel.get()
                    self._accept(record)
                    await self._produce(record)
                    self._cursor = (index + 1) % len(channels)
                    progressed = True
                    break
                if channel.closed:
                    ended[index] = True
                    continue
            if progressed:
                continue
            live = [i for i in range(len(channels)) if not ended[i]]
            if not live:
                break
            await self._wait_any(channels, live)

    async def _wait_any(self, channels, live) -> None:
        loop = asyncio.get_running_loop()
        waiters = []
        for index in live:
            channel = channels[index]
            waiter = loop.create_future()
            channel._get_waiter = waiter
            waiters.append((channel, waiter))
        try:
            done, _ = await asyncio.wait(
                [waiter for _c, waiter in waiters],
                return_when=asyncio.FIRST_COMPLETED)
            for waiter in done:
                exc = waiter.exception()
                if exc is not None:
                    raise exc
        finally:
            for channel, waiter in waiters:
                if channel._get_waiter is waiter:
                    channel._get_waiter = None
                if not waiter.done():
                    waiter.cancel()


# --------------------------------------------------------------------------- #
# sink
# --------------------------------------------------------------------------- #

class SinkNode(_Node):
    """Append every arriving record as one JSONL line, flushing after each.

    Output line shape::

        {"identity": {"source": ..., "line": N, "expansion_path": [...]},
         "row": {...}}

    ``records`` in the summary counts lines fully written and flushed.
    ``complete`` becomes true only when the whole run succeeded, every input
    delivered its end marker and the file closed cleanly.
    """

    kind = "sink"

    def __init__(self, run: "Run", name: str, spec):
        super().__init__(run, name, spec)
        self.relative_path = spec.options["path"]
        self.path: Optional[str] = None
        self.handle = None
        self.count = 0
        self.closed_cleanly = False
        self.saw_eos = False

    async def start(self) -> None:
        self.path = self.run.resolve_sink_path(self.relative_path)
        loop = asyncio.get_running_loop()
        try:
            self.handle = await loop.run_in_executor(None, self._open)
        except OSError as exc:
            raise self.run._fail(self.name, "opening sink", exc) from exc
        if self.run._failure is not None or self.run._cancelled:
            # A failure elsewhere happened while this file was being opened;
            # do not start consuming, the supervisor will clean up.
            return
        channels = list(self.in_edges)
        ended = [False] * len(channels)
        cursor = 0
        while not all(ended):
            if self.run._failure is not None:
                # Stop promptly when the run failed elsewhere; the supervisor
                # will cancel this task, but this path keeps cleanup real even
                # if cancellation is delivered late.
                return
            await self._wait_gate()
            channel = channels[cursor]
            if ended[cursor]:
                cursor = (cursor + 1) % len(channels)
                continue
            try:
                record = await channel.get()
            except ChannelClosed:
                ended[cursor] = True
                cursor = (cursor + 1) % len(channels)
                continue
            self.processed += 1
            payload = self._encode(record)
            try:
                await loop.run_in_executor(None, self._write, payload)
            except BaseException as exc:  # noqa: BLE001
                raise self.run._fail(
                    self.name,
                    f"sink write failed: {exc}",
                    exc,
                    identity=record.identity(),
                ) from exc
            self.count += 1
            self.run._return_credit(self.name)
            await self.run._publish_result(self.name, record)
            cursor = (cursor + 1) % len(channels)
        self.saw_eos = True

    def _open(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return open(self.path, "wb")

    def _encode(self, record: Record) -> bytes:
        try:
            body = _encoded_row(record.row)
        except (TypeError, ValueError) as exc:
            raise FlowFailure(
                self.name,
                f"record cannot be encoded as JSON: {exc}",
                identity=record.identity(),
                cause=exc,
            ) from exc
        limit = self.run.max_record_bytes
        if len(body) > limit:
            raise FlowFailure(
                self.name,
                f"record row is {len(body)} bytes, exceeding "
                f"max_record_bytes={limit}; output is never truncated",
                identity=record.identity(),
            )
        identity = json.dumps(record.identity(), ensure_ascii=False,
                              separators=(",", ":"))
        return (b'{"identity":' + identity.encode("utf-8") + b',"row":'
                + body + b"}\n")

    def _write(self, payload: bytes) -> None:
        self.handle.write(payload)
        self.handle.flush()

    async def finish(self) -> None:
        loop = asyncio.get_running_loop()
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            await loop.run_in_executor(None, self._close_file, handle)
        except BaseException as exc:  # noqa: BLE001
            raise self.run._fail(self.name, "closing sink", exc) from exc
        self.closed_cleanly = True

    @staticmethod
    def _close_file(handle) -> None:
        handle.flush()
        handle.close()

    async def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, handle.close)
            except OSError:
                pass

    def completion_state(self) -> Dict[str, Any]:
        return {"path": self.relative_path, "records": self.count,
                "closed": self.closed_cleanly}


NODE_CLASSES = {
    "source": SourceNode,
    "select": SelectNode,
    "project": ProjectNode,
    "explode": ExplodeNode,
    "merge": MergeNode,
    "sink": SinkNode,
    "custom": CustomNode,
}


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #

class Run:
    """One execution.  Constructed by :func:`distagg.flow.engine.start_run`."""

    def __init__(self, graph, config: Optional[FlowConfig] = None,
                 base_dir: Optional[str] = None):
        self.graph = graph
        self.config = config or FlowConfig()
        self.base_dir = base_dir
        graph.ensure_valid()
        if len(graph.nodes) > self.config.max_nodes:
            from .errors import GraphError

            raise GraphError([
                f"graph has {len(graph.nodes)} nodes, exceeding "
                f"max_nodes={self.config.max_nodes}"
            ])
        if len(graph.edges) > self.config.max_edges:
            from .errors import GraphError

            raise GraphError([
                f"graph has {len(graph.edges)} edges, exceeding "
                f"max_edges={self.config.max_edges}"
            ])
        self.max_line_bytes = _smaller_limit(
            graph.max_line_bytes, self.config.max_line_bytes,
            DEFAULT_MAX_LINE_BYTES)
        self.max_record_bytes = _smaller_limit(
            graph.max_record_bytes, self.config.max_record_bytes,
            DEFAULT_MAX_RECORD_BYTES)

        self.channels: List[Channel] = []
        self.in_edges: Dict[str, List[Channel]] = {}
        self.out_edges: Dict[str, List[Channel]] = {}
        self._build_channels()

        self.results_dir: Optional[str] = None
        self.results_dir_created = False
        self.nodes: Dict[str, _Node] = {}
        self.gates: Dict[str, PauseGate] = {}

        self._tasks: Dict[str, asyncio.Task] = {}
        self._credits: Dict[str, Dict[str, Any]] = {}
        for node in graph.nodes:
            if node.kind != "source":
                self._credits[node.name] = {
                    "available": self.config.max_inflight_per_node,
                    "waiters": [],
                }
        self._failure: Optional[BaseException] = None
        self._cancelled = False
        self._finished = False
        self._started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._per_node_emitted: Dict[str, int] = {}
        self._per_edge_emitted: Dict[Channel, int] = {}
        self._result_queue: Optional[asyncio.Queue] = None
        self._result_iterators_open = 0
        self._result_closed = False

    # -- construction ----------------------------------------------------- #

    def _build_channels(self) -> None:
        for node in self.graph.nodes:
            self.in_edges.setdefault(node.name, [])
            self.out_edges.setdefault(node.name, [])
        for edge in self.graph.edges:
            capacity = edge.capacity or self.config.default_capacity
            channel = Channel(edge.source, edge.target, capacity)
            self.channels.append(channel)
            self.out_edges[edge.source].append(channel)
            self.in_edges[edge.target].append(channel)

    def resolve_sink_path(self, relative: str) -> str:
        if self.results_dir is None:
            raise RuntimeError("result directory has not been created yet")
        base = os.path.abspath(self.results_dir)
        target = os.path.abspath(os.path.join(base, relative))
        if os.path.commonpath([target, base]) != base:
            raise ValueError(
                f"sink path {relative!r} escapes the result directory")
        return target

    # -- credits ---------------------------------------------------------- #

    async def _acquire_credit(self, node_name: str) -> None:
        pool = self._credits[node_name]
        while True:
            if self._failure is not None:
                raise self._failure
            if pool["available"]:
                pool["available"] -= 1
                return
            waiter = asyncio.get_running_loop().create_future()
            pool["waiters"].append(waiter)
            try:
                await waiter
            finally:
                if waiter in pool["waiters"]:
                    pool["waiters"].remove(waiter)

    def _return_credit(self, node_name: str) -> None:
        pool = self._credits.get(node_name)
        if pool is None:  # pragma: no cover - sources never hold credits
            return
        pool["available"] += 1
        if pool["waiters"]:
            waiter = pool["waiters"].pop(0)
            if not waiter.done():
                waiter.set_result(None)

    # -- accounting / errors ---------------------------------------------- #

    def _note_emitted(self, node_name: str, channel: Channel) -> None:
        self._per_node_emitted[node_name] = self._per_node_emitted.get(
            node_name, 0) + 1
        self._per_edge_emitted[channel] = self._per_edge_emitted.get(
            channel, 0) + 1

    def _record_failure(self, failure: BaseException) -> None:
        if isinstance(failure, FlowCancelled):
            # A stopped channel surfaces as FlowCancelled in some node; that
            # is the cancellation protocol, never a failure to report.
            if not self._cancelled:
                self._cancelled = True
            self._broadcast_stop(failure)
            self._wake_results()
            return
        if self._failure is not None:
            return
        self._failure = failure
        self._broadcast_stop(failure)
        self._wake_results()

    def _broadcast_stop(self, failure: BaseException) -> None:
        for channel in self.channels:
            channel.fail(failure)
        for pool in self._credits.values():
            for waiter in pool["waiters"]:
                if not waiter.done():
                    waiter.set_result(None)
        for gate in self.gates.values():
            gate.wake()

    def _fail(self, node: str, reason: str, cause: Optional[BaseException],
              identity: Optional[Dict[str, Any]] = None) -> BaseException:
        if isinstance(cause, FlowError):
            failure: BaseException = cause
        else:
            failure = FlowFailure(node, reason, identity=identity, cause=cause)
        self._record_failure(failure)
        return failure

    def _source_failure(self, error: SourceRecordError) -> BaseException:
        identity = {"source": error.source, "line": error.line_number,
                    "expansion_path": []}
        failure = FlowFailure(error.source, error.message, identity=identity,
                              cause=error, kind="SourceRecordError")
        failure.path = error.path
        self._record_failure(failure)
        return failure

    def _check_error(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._cancelled:
            raise FlowCancelled("run was cancelled by the caller")

    # -- results ---------------------------------------------------------- #

    def _wake_results(self) -> None:
        """Signal end-of-stream *after* records already queued are consumed.

        A sentinel is placed only once the run is finished and every declared
        result iterator has had the chance to drain; otherwise a failure that
        arrives before the consumer dequeued a row would hide that row.
        """
        self._result_closed = True
        queue = self._result_queue
        if queue is None or not self._finished:
            return
        self._put_result_end()

    def _put_result_end(self) -> None:
        queue = self._result_queue
        if queue is None:
            return
        while True:
            try:
                queue.put_nowait(RESULT_END)
                return
            except asyncio.QueueFull:
                # Drop queued rows only after the consumer-state check below;
                # otherwise retry after a slot frees up.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    return

    async def _publish_result(self, sink_name: str, record: Record) -> None:
        queue = self._result_queue
        if queue is None or self._result_closed:
            return
        item = (sink_name, record.identity(), record.row)
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            await queue.put(item)

    # -- start ------------------------------------------------------------ #

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("run already started")
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._prepare_results_dir()
        if self.config.result_stream:
            self._result_queue = asyncio.Queue(
                maxsize=self.config.result_capacity)
        self._create_nodes()
        for name in self.graph.order:
            self._tasks[name] = self._loop.create_task(
                self._run_node(self.nodes[name]), name=f"flow:{name}")

    def _prepare_results_dir(self) -> None:
        target = self.config.results_dir
        if target is None:
            base = self.base_dir or os.getcwd()
            target = os.path.join(base, f"flow-run-{uuid.uuid4().hex[:12]}")
        target = os.path.abspath(target)
        if os.path.lexists(target):
            raise FlowFailure(
                "__run__",
                f"result directory {target!r} already exists; each execution "
                "needs a fresh directory and old directories are never "
                "overwritten",
            )
        parent = os.path.dirname(target) or "."
        try:
            os.makedirs(parent, exist_ok=True)
            os.mkdir(target, 0o700)
        except OSError as exc:
            raise FlowFailure(
                "__run__",
                f"cannot create result directory {target!r}: {exc}",
                cause=exc) from exc
        self.results_dir = target
        self.results_dir_created = True

    def _create_nodes(self) -> None:
        self.nodes = {}
        for name in self.graph.order:
            spec = self.graph.node(name)
            self.nodes[name] = NODE_CLASSES[spec.kind](self, name, spec)

    async def _run_node(self, node: _Node) -> None:
        normal = False
        try:
            await node.start()
            normal = True
        except FlowError as exc:
            self._record_failure(exc)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._record_failure(FlowFailure(
                node.name, f"unexpected node failure: {exc}", cause=exc))
        finally:
            if normal and self._failure is None and not self._cancelled:
                try:
                    await node.finish()
                except FlowError as exc:
                    self._record_failure(exc)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001
                    self._record_failure(FlowFailure(
                        node.name,
                        f"unexpected failure while closing: {exc}", cause=exc))
            try:
                await node.close()
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001
                pass

    # -- waiting / cancellation ------------------------------------------ #

    async def _supervise(self) -> None:
        pending = set(self._tasks.values())
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    self._record_failure(exc)

    async def _shutdown(self) -> None:
        """Cancel any task still running and wait for every node to clean up."""
        pending = [task for task in self._tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            _done, stubborn = await asyncio.wait(pending, timeout=5.0)
            if stubborn:
                # A custom operator that deliberately swallows cancellation
                # must not hang the run forever; give up on those tasks rather
                # than waiting without a bound.
                await asyncio.gather(*stubborn, return_exceptions=True)

    def _run_ended_itself(self) -> bool:
        """True when every node task already finished on its own."""
        return bool(self._tasks) and all(task.done() for task in self._tasks.values())

    async def _join(self) -> None:
        if (self._failure is not None or self._cancelled) and \
                not self._run_ended_itself():
            # Something went wrong while nodes were still running: stop them.
            await self._shutdown()
        else:
            await self._supervise()
        self._finished = True
        if self._result_queue is not None:
            self._result_closed = True
            self._put_result_end()

    def _cancel_requested(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._broadcast_stop(FlowCancelled("run was cancelled by the caller"))
        self._wake_results()

    def cancel(self) -> None:
        """Co-operative stop; safe to call from the run's event loop."""
        if self._finished or self._cancelled:
            return
        self._cancel_requested()
        for task in self._tasks.values():
            if not task.done():
                task.cancel()

    # -- summary ---------------------------------------------------------- #

    def summary(self) -> RunSummary:
        if self._failure is not None:
            status = "failed"
        elif self._cancelled:
            status = "cancelled"
        else:
            status = "success"
        sinks = {name: {"paused": bool(node.gate is not None
                                       and node.gate.is_paused)}
                 for name, node in self.nodes.items() if node.kind == "sink"}
        for name, node in self.nodes.items():
            if node.kind != "sink":
                continue
            complete = (
                status == "success"
                and node.closed_cleanly
                and node.saw_eos
                and all(channel.is_drained() for channel in node.in_edges)
            )
            sinks[name].update({
                "path": node.relative_path,
                "records": node.count,
                "complete": complete,
                "closed_cleanly": node.closed_cleanly,
            })
        sources = {
            name: node.completion_state()
            for name, node in self.nodes.items() if node.kind == "source"
        }
        edges = [channel.stats() for channel in self.channels]
        capacity = sum(channel.capacity for channel in self.channels)
        peak_sum = sum(channel.max_occupied for channel in self.channels)
        error = None
        if self._failure is not None:
            error = (self._failure.to_dict()
                     if isinstance(self._failure, FlowError)
                     else {"error": "flow_failure",
                           "message": str(self._failure)})
        result_buffer = (self.config.result_capacity
                         if self.config.result_stream else 0)
        data: Dict[str, Any] = {
            "status": status,
            "ok": status == "success",
            "graph": self.graph.name,
            "results_dir": self.results_dir,
            "error": error,
            "sources": sources,
            "sinks": sinks,
            "edges": edges,
            "limits": {
                "max_line_bytes": self.max_line_bytes,
                "max_record_bytes": self.max_record_bytes,
                "default_capacity": self.config.default_capacity,
                "max_inflight_per_node": self.config.max_inflight_per_node,
                "result_capacity": self.config.result_capacity,
            },
            "buffers": {
                "edge_capacity_records": capacity,
                "edge_records_at_end": sum(c.occupied for c in self.channels),
                "edge_peak_occupancy_sum": peak_sum,
                "inflight_credit_total": (
                    len(self._credits) * self.config.max_inflight_per_node),
                "result_capacity": result_buffer,
                "max_line_bytes_per_source": self.max_line_bytes,
                "theoretical_record_bound": (
                    capacity
                    + len(self._credits) * self.config.max_inflight_per_node
                    + result_buffer),
            },
        }
        return RunSummary(data)

    async def snapshot(self) -> Dict[str, Any]:
        """Live counters; safe to call while the run is in progress."""
        return {
            "edges": [channel.stats() for channel in self.channels],
            "credits": {
                name: pool["available"] for name, pool in self._credits.items()
            },
            "sources": {
                name: node.completion_state()
                for name, node in self.nodes.items() if node.kind == "source"
            },
            "sinks": {
                name: node.completion_state()
                for name, node in self.nodes.items() if node.kind == "sink"
            },
        }

    async def close(self) -> None:
        if self._started and not self._finished:
            self.cancel()
            await self._shutdown()
            self._finished = True
        for node in self.nodes.values():
            try:
                await node.close()
            except BaseException:  # noqa: BLE001
                pass
        self._result_closed = True
        if self._result_queue is not None:
            self._put_result_end()


def _smaller_limit(graph_value: Optional[int], config_value: Optional[int],
                   default: int) -> int:
    return min(value for value in (graph_value, config_value, default)
               if value is not None)


class FlowRun:
    """The user-facing async handle for one execution.

    Typical use::

        graph = load_graph("graph.json")
        async with start_run(graph, FlowConfig("/tmp/run-1")) as run:
            summary = await run.wait()
        assert summary.ok

    ``await run.start()`` starts the nodes and returns; ``wait()`` returns the
    summary after every node finished.  ``iter_results()`` streams the rows
    sinks wrote (requires ``FlowConfig(result_stream=True)``).
    """

    def __init__(self, run: Run):
        self.run = run
        self._summary: Optional[RunSummary] = None
        self._started = False
        self._closed = False
        self._task: Optional[asyncio.Task] = None

    @property
    def results_dir(self) -> Optional[str]:
        return self.run.results_dir

    async def start(self) -> "FlowRun":
        if self._started:
            raise RuntimeError("run already started")
        self._started = True
        self._task = asyncio.get_running_loop().create_task(
            self._execute(), name="flow:run")
        await asyncio.sleep(0)
        return self

    async def _execute(self) -> None:
        try:
            await self.run.start()
            await self.run._join()
        except asyncio.CancelledError:
            # Cancelling the run task is the caller's cancellation request:
            # clean every node up, then let the cancellation propagate.
            self.run._cancel_requested()
            await self.run._shutdown()
            self.run._finished = True
            raise
        except BaseException as exc:  # noqa: BLE001
            self.run._record_failure(exc)
            await self.run._shutdown()
            self.run._finished = True

    async def wait(self) -> RunSummary:
        """Wait for every node; the returned summary never claims false success.

        If the caller cancels this await (``task.cancel()``, ``wait_for``
        timeout, Ctrl-C), the run is stopped co-operatively, its files and
        iterators are closed, and the ``CancelledError`` is re-raised after the
        cleanup.  ``run.run.summary()`` is still available afterwards.
        """
        if self._summary is not None:
            return self._summary
        if self._task is not None and not self._task.done():
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                self.cancel()
                try:
                    await asyncio.shield(self._task)
                except BaseException:  # noqa: BLE001 - cleanup best effort
                    pass
                self._summary = self.run.summary()
                raise
            except BaseException:  # noqa: BLE001 - run recorded its failure
                pass
        if self._summary is None:
            self._summary = self.run.summary()
        return self._summary

    async def run_to_completion(self) -> RunSummary:
        if not self._started:
            await self.start()
        return await self.wait()

    def add_gate(self, node_name: str) -> PauseGate:
        """Install a pause barrier in front of one node's input (before start)."""
        spec = self.run.graph.node(node_name)
        if spec.kind == "source":
            raise ValueError("a source has no input to gate")
        gate = PauseGate(node_name)
        self.run.gates[node_name] = gate
        return gate

    async def snapshot(self) -> Dict[str, Any]:
        return await self.run.snapshot()

    async def iter_results(self):
        """Yield ``(sink_name, identity, row)`` in the order sinks wrote them.

        Requires ``FlowConfig(result_stream=True)``.  The buffer is bounded by
        ``result_capacity``; a stalled consumer pushes back through the sinks,
        the channels and finally the source.  Closing this iterator early
        requests a cooperative stop.
        """
        queue = self.run._result_queue
        if queue is None:
            raise RuntimeError(
                "result streaming is disabled; start the run with "
                "FlowConfig(result_stream=True)")
        self.run._result_iterators_open += 1
        try:
            while True:
                item = await queue.get()
                if item is RESULT_END:
                    return
                sink_name, identity, row = item
                yield sink_name, identity, row
        finally:
            self.run._result_iterators_open -= 1
            if not self.run._finished and not self.run._cancelled:
                self.cancel()
            if self.run._finished and self.run._result_iterators_open == 0:
                try:
                    queue.put_nowait(RESULT_END)
                except asyncio.QueueFull:
                    pass

    def results(self):
        return self.iter_results()

    def cancel(self) -> None:
        self.run.cancel()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None and not self._task.done():
            self.cancel()
            try:
                await asyncio.shield(self._task)
            except BaseException:  # noqa: BLE001
                pass
        await self.run.close()
        if self._summary is None:
            self._summary = self.run.summary()

    async def __aenter__(self) -> "FlowRun":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.aclose()
        return False


def start_run(graph, config: Optional[FlowConfig] = None,
              *, base_dir: Optional[str] = None) -> FlowRun:
    """Validate a graph/configuration and return an (unstarted) run handle.

    All static checks run synchronously.  No business input is opened, no
    output file or result directory is created and no task exists afterwards.
    """
    return FlowRun(Run(graph, config, base_dir=base_dir))
