"""distagg.flow — a local asynchronous, bounded-memory flow executor.

The package turns one UTF-8 JSONL file into several JSONL outputs by running a
user-defined finite graph of ``source`` / ``select`` / ``project`` / ``explode``
/ ``merge`` / ``sink`` nodes.  Each record carries its source name, physical
line number and ``expansion_path``; a branch broadcast gives every successor
its own copy; a ``merge`` is a fair round-robin join that never de-duplicates.

Nothing here calls an existing stream engine: nodes are plain coroutines in one
asyncio loop talking over fixed-capacity channels.

Quick start (Python)::

    from distagg.flow import FlowConfig, load_graph, start_run

    graph = load_graph("graph.json")
    async with start_run(graph, FlowConfig("/tmp/flow-run-1")) as run:
        summary = await run.wait()
    assert summary.ok

Quick start (terminal)::

    distagg flow check graph.json
    distagg flow run   graph.json --results /tmp/flow-run-1 --capacity 2

Custom operators are Python-API only (a graph file never loads code)::

    def annotate(record, emit):
        async def go():
            row = dict(record.row)
            row["nested"] = dict(row["nested"], seen=True)
            await emit(record.__class__(record.source, record.line,
                                        record.expansion_path, row))
        return go()

    graph.custom("annotate", annotate)

See ``docs/FLOW.md`` for the JSON format, semantics, the failure/cancellation
protocol and the exact memory bound.
"""

from .config import FlowConfig, RunSummary
from .engine import FlowRun, PauseGate, start_run
from .errors import (
    FlowCancelled,
    FlowError,
    FlowFailure,
    GraphError,
    SourceRecordError,
)
from .graph import Edge, FlowGraph, Node, load_graph, parse_graph
from .ops import COMPARATORS, CustomOperator, get_field, is_absent, make_custom
from .record import Record

__all__ = [
    "FlowConfig",
    "RunSummary",
    "FlowRun",
    "PauseGate",
    "start_run",
    "FlowGraph",
    "Node",
    "Edge",
    "load_graph",
    "parse_graph",
    "Record",
    "COMPARATORS",
    "CustomOperator",
    "make_custom",
    "is_absent",
    "get_field",
    "FlowError",
    "GraphError",
    "FlowFailure",
    "FlowCancelled",
    "SourceRecordError",
]
