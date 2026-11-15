# Local asynchronous flow executor (`distagg.flow`)

`distagg.flow` is an independent local execution engine for one finite
processing graph over UTF-8 JSONL files.  A supplier file often has to feed two
consumers at once: operations needs an exception detail list, the warehouse
needs the exploded, acceptable sub-items, and some records must go to both
paths.  Instead of rereading the file once per branch, you declare the graph
once; the source reads each physical line once, records flow through the
operators as they arrive, and several sinks are written in the same run.

No existing stream-processing engine is used: nodes are plain coroutines in one
`asyncio` event loop exchanging records over fixed-capacity channels.

```bash
distagg flow check GRAPH.json            # validate only, opens nothing
distagg flow run   GRAPH.json --results /tmp/flow-run-1 \
    --capacity 2 --max-inflight 2
```

```python
import asyncio
from distagg.flow import FlowConfig, load_graph, start_run

async def main():
    graph = load_graph("graph.json")          # validated synchronously
    async with start_run(graph, FlowConfig("/tmp/flow-run-1")) as run:
        summary = await run.wait()
    assert summary.ok, summary.error

asyncio.run(main())
```

Exit status: `0` for a complete success, `1` for a failure, a cancelled run or
an invalid graph, `2` for a command-line usage error.  A failed or cancelled run
prints `"status": "failed"` / `"cancelled"`, never a success summary.

## Graph definition (JSON)

```json
{
  "name": "supplier-split-merge",
  "max_line_bytes": 1048576,
  "max_record_bytes": 1048576,
  "nodes": [
    {"name": "in",         "type": "source",  "path": "imports.jsonl"},
    {"name": "exceptions", "type": "select",  "field": "status",
     "condition": "eq", "value": "error"},
    {"name": "acceptable", "type": "select",  "field": "status",
     "condition": "ne", "value": "error"},
    {"name": "items",      "type": "explode", "field": "items"},
    {"name": "flattened",  "type": "project",
     "fields": {"order_id": "id", "item": "items"}},
    {"name": "merged",     "type": "merge"},
    {"name": "exception_report", "type": "sink", "path": "exceptions.jsonl"},
    {"name": "ledger",           "type": "sink", "path": "ledger.jsonl"}
  ],
  "edges": [
    {"from": "in",         "to": "exceptions"},
    {"from": "in",         "to": "acceptable"},
    {"from": "exceptions", "to": "exception_report"},
    {"from": "acceptable", "to": "items"},
    {"from": "items",      "to": "flattened"},
    {"from": "flattened",  "to": "merged"},
    {"from": "exceptions", "to": "merged"},
    {"from": "merged",     "to": "ledger", "capacity": 2}
  ]
}
```

* `name`, `max_line_bytes`, `max_record_bytes` are optional; unknown members
  are rejected so a typo cannot silently change the meaning.
* `path` of a source is resolved relative to the graph file; a sink path is
  relative to the fresh result directory and may not escape it.
* `capacity` on an edge is an optional positive integer; it overrides
  `FlowConfig.default_capacity` / `--capacity`.
* Duplicate JSON members anywhere in the document, non-JSON constants and
  invalid UTF-8 are rejected as graph errors.
* A `custom` node is **Python-API only** — a graph file never loads code.

### Node rules

| type | inputs | outputs | members |
|---|---|---|---|
| `source` | 0 | ≥1 | `path` |
| `select` | exactly 1 | ≥1 | `field`, `condition`, `value` |
| `project` | exactly 1 | ≥1 | `fields`: output name → input name |
| `explode` | exactly 1 | ≥1 | `field` (top-level array) |
| `merge` | ≥2 | ≥1 | — |
| `sink` | ≥1 | 0 | `path` |

Any number of outgoing edges means **broadcast**: every successor receives a
copy of every record. It never splits work, and each successor's copy is
independent (the first gets the record, the others get deep copies, so mutating
a nested object on one branch cannot affect another branch).

### Validation, before any side effect

`load_graph`, `parse_graph` and `start_run` validate the whole graph
synchronously and raise `GraphError` with every problem found. Validation
rejects duplicate node names, edges to unknown nodes, duplicate edges,
self-loops and every cycle, wrong input counts (sources 0, sinks 0, `merge`
≥2, everything else exactly 1), sinks with no input, non-sink nodes with no
output, and nodes from which no sink is reachable. It also rejects two sinks
writing the same path, sink paths inside `..` or absolute paths, graph node
counts above `max_nodes` / `--max-nodes` and edge counts above `max_edges`.

No source is opened, no result directory created and no task started until
`await run.start()`.  A bad graph therefore leaves no trace: the CLI's
`flow check` and `flow run` both refuse before touching the business file.

## Records and identity

Every record carries four fields:

| field | meaning |
|---|---|
| `source` | source node name |
| `line` | physical 1-based line number in that source's file |
| `expansion_path` | `[]` originally; `explode` appends the child index (from 0) |
| `row` | the JSON object being processed |

The tuple `(source, line, expansion_path)` is the source identity and is what
every sink writes in its envelope:

```json
{"identity": {"source": "in", "line": 1, "expansion_path": [1]},
 "row": {"order_id": "A-1", "item": {"sku": "s1", "qty": 2}}}
```

A business primary key is never used for this, and `merge` never de-duplicates
by identity: the same physical line that reaches a merge over two paths is
delivered twice. A nested `explode` keeps appending indices, so `[0, 1]` is
the second child of the first item.

## Operator semantics

### `select`

`field` plus `condition` and `value`:

* `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `is_null`;
* `value` is an integer, string, boolean or null (and must be omitted for
  `is_null`);
* `eq`/`ne` keep booleans and integers apart: `true` is not `1`;
* an ordinary comparison never passes if the field is absent **or** null.
  Only `is_null` selects a present null; an absent field is not null;
* ordering comparisons require two integers (booleans excluded) or two
  strings. Any other runtime pairing (`int` vs `str`, `bool` vs `int`, a float
  sneaked in through a custom operator) is an operator error that fails the
  run with the node and record identity;
* a null field value (or null comparison value) simply does not pass an
  ordering comparison — it is not an error.

### `project`

`fields` maps *output* names to *input* names. Only named fields are copied;
nothing is guessed. A missing input field is an error naming the record; a
null value is copied as `null`. Values are deep-copied, which is also why a
projected branch's nested objects are independent of any other branch.

### `explode`

`field` must hold an array. Each item produces one child record with the same
`source`/`line`, the parent's `expansion_path` plus the item's 0-based index,
and a copy of the parent row with that field replaced by the item. Other fields
are preserved. An empty array produces zero records; two equal items are still
two children (different indices). A non-array is an error; an absent field is
an error (absent is not "empty"). Children are produced one at a time, so a
large array is never materialised as a list by the executor.

### `merge`

Fair round-robin over its inputs:

* order **inside** one input channel is preserved exactly;
* no global order between inputs is imposed or promised;
* an input that is momentarily empty never blocks a ready input: the merge
  drains any buffered input before waiting, and when all live inputs are
  momentarily empty it waits on all of them at once (one wakeup is enough);
* an empty input's end-of-stream is handled like any other: a source that
  exhausts closes only its own channel, and the merge ends only after every
  input delivered its end marker and its buffered records;
* no record is de-duplicated.

### `source` strictness

Each physical line must be one JSON object. The run fails with the source name
and physical 1-based line for:

* a blank/whitespace-only line;
* invalid UTF-8;
* invalid JSON, including `NaN`/`Infinity`/`-Infinity`;
* a duplicate member name in any object of the line;
* a non-object top-level value;
* a line longer than `max_line_bytes` (rejected while reading — the rest of
  the line is never buffered and nothing is truncated);
* a record whose encoded row plus identity exceeds `max_record_bytes`.

The limits are inclusive and apply to bytes of the encoded payload; the
effective value is the smallest of the graph's declaration, the config's
override and the documented default (1 MiB each). A limit applied to an
exploded or custom-operator output is checked before it is pushed downstream.

## Python API

```python
from distagg.flow import FlowGraph, FlowConfig, start_run, Record

graph = FlowGraph(name="supplier")
graph.source("in", "imports.jsonl")          # absolute path here
graph.select("ok", "status", "ne", "error")
graph.sink("out", "out.jsonl")
graph.add_edge("in", "ok")
graph.add_edge("ok", "out")
graph.validate()                              # raises GraphError on problems

config = FlowConfig(
    "/tmp/run-1",            # fresh result directory (must not exist)
    default_capacity=8,      # edge capacity when the graph does not set one
    max_inflight_per_node=8, # records assigned to one node but not finished
    max_line_bytes=None,     # None: graph value or 1 MiB
    max_record_bytes=None,
    result_stream=False,     # stream rows to the caller too
    result_capacity=64,
)

run = start_run(graph, config)
summary = asyncio.run(run.run_to_completion())
```

`FlowRun` is also an async context manager; `await run.start()` starts and
returns, `await run.wait()` returns the `RunSummary` after all nodes finished,
`run.cancel()` requests a co-operative stop, `await run.aclose()` releases
everything and can be called twice.

### Custom one-to-many operators

```python
from distagg.flow import Record

async def enrich(record, emit):
    await emit(record)                        # zero, one or many times
    row = dict(record.row, checked=True)
    await emit(Record(record.source, record.line,
                      record.expansion_path, row))

graph.custom("enrich", enrich)                # API only, never a graph file
```

`emit` is awaitable and delivers the record immediately; an operator may
`await asyncio.sleep`, I/O or anything else between emissions. It must never
build and return the whole batch. Returning an async iterable of `Record`
instead of calling `emit` is accepted too; it is closed with `aclose()` on
every exit path (success, failure and cancellation). Records it emits must be `Record` objects and
obey `max_record_bytes`. `asyncio.CancelledError` is not swallowed by the
runtime; a custom operator that deliberately swallows cancellation defeats only
its own turn's promptness, and the supervisor cancels it after a bounded grace
period.

### Pause gates (operations and tests)

`gate = run.add_gate("sink_name")` installs a barrier in front of any node's
input (except a source). While paused the node consumes nothing; records
already accepted stay inside the bounded channels and credit pools, so
back-pressure reaches the source. `await gate.wait_paused()` resolves once the
node is actually blocked, so a test can inspect the live counters at that
exact moment. `gate.resume()` continues; nothing is dropped.

## Runtime, failure and cancellation protocol

* All nodes start in the same event loop at the same time; each record flows
  through the graph before the next one is read upstream. The upstream is never
  fully materialised to disk before downstream starts.
* The **first** failure — source decode, operator error, sink write error,
  sink open error — is recorded once and broadcast: every channel, every
  producer blocked on a full channel, every consumer blocked on an empty one
  and every pause gate is woken. Nodes close their files/iterators and return;
  the supervisor joins all tasks (cancelling a task that ignores cancellation
  after a bounded grace period). The summary reports the original node, the
  source identity of the record being processed and the reason.
* A source that exhausts closes only its own outgoing channels; a merge waits
  for all inputs.
* Cancellation is the same protocol with the outcome `"cancelled"`. It is
  triggered by `run.cancel()`, by cancelling the run task or awaiting
  `wait()` inside `asyncio.wait_for`/`task.cancel()`, by closing an open
  result iterator early, and by `SIGINT`/`SIGTERM` in the CLI.
* `RunSummary`:

```json
{
  "status": "success" | "failed" | "cancelled",
  "ok": true,
  "results_dir": "/tmp/run-1",
  "error": {"error": "flow_failure", "node": "boom", "reason": "...",
            "identity": {"source": "in", "line": 5, "expansion_path": []}},
  "sources": {"in": {"lines_pulled": 5, "records_emitted": 5, ...}},
  "sinks":   {"out": {"path": "out.jsonl", "records": 3,
                      "complete": false, "closed_cleanly": false,
                      "paused": false}},
  "edges":   [{"from": "in", "to": "out", "capacity": 2,
               "sent": 5, "received": 3, "peak_occupancy": 2}],
  "buffers": {"edge_capacity_records": 2, "edge_peak_occupancy_sum": 2,
              "inflight_credit_total": 10, ...}
}
```

`complete` is true only for a successful run whose sink received every record
(its inputs were drained) and whose file closed cleanly. Failure and
cancellation leave already-written lines in place and report the exact counts;
they never print a success summary or return exit code 0.

## Fresh result directories

Each execution needs a directory that does not exist yet. An existing path is
refused; old runs are never overwritten. If `results_dir` is omitted a unique
`flow-run-<random>` directory is created next to the current directory. Sinks
create their parent directories inside it and are written with live flushes, so
partial output is visible during a run.

## Memory model

Let `C(e)` be each edge's capacity and `I` the per-node in-flight limit
(`max_inflight_per_node`). A producer must take a credit from the target node's
pool before pushing; the target returns it when it takes the record (a sink:
after writing and flushing it). Therefore:

* each edge stores at most `C(e)` record references;
* at most `I` records are pushed-but-not-yet-taken for any one node, and at
  most `I` are taken-but-not-yet-written for a sink;
* the result stream, when enabled, adds at most `result_capacity` row
  references.

The run keeps at most

    Σ C(e)  +  I · (number of non-source nodes)  +  result_capacity

record references, each at most `max_record_bytes` bytes plus the small
identity envelope, one line buffer of at most `max_line_bytes` per source plus
one read chunk, and one in-progress output line per sink. This bound is fixed
when the run starts; it does not grow with the number of lines, and the engine
never creates one task per record.

`RunSummary.buffers` reports the configured bound. The per-edge `peak_occupancy`
and `sent`/`received` counters are actual measurements, not configuration. The
source's `lines_pulled` is the number of physical lines actually read, so a
paused sink shows directly how far the source got.

The result stream (`FlowConfig(result_stream=True)`, or any consumer of
`iter_results()`) is bounded too. If the consumer stops reading, the result
buffer fills, the next sink write waits, its input channel fills, the credit
pool drains, and the source stops pulling. No sink is skipped and no record is
dropped.

## Measured behaviour

`tools/flow_stress.py` runs the same graph over N and 2N synthetic rows,
consuming every output row one at a time, and prints the real peak RSS
(`ru_maxrss` high-water mark), the highest observed edge occupancy and the line
counts. A representative run on the development machine (capacity 2,
in-flight 2, 6 edges):

| source rows | output rows | peak occupancy | declared record bound | peak RSS |
|---|---|---|---|---|
| 20 000 | 80 000 | 2 | 28 | 23 556 KiB |
| 40 000 | 160 000 | 2 | 28 | 23 872 KiB |

Doubling the input doubled the time and line counts but left the occupancy,
the declared bound and peak RSS essentially unchanged — the buffers do not
grow with the input. Re-run with `--json` to get the figures for your machine;
the script fails (non-zero) if memory grows with the input or an edge's peak
occupancy exceeds its capacity.

## Demo

`python3 demo/flow/flow_demo.py [WORKDIR]` performs and asserts, printing
every hand-checkable figure:

1. `demo/flow/split_merge.json` on `demo/flow/imports.jsonl` (5 lines with a
   duplicated business id, a null quantity and an empty array). It prints the
   ledger line by line with source line, expansion path and multiplicity:
   lines 2 and 3 via the error branch (whole records), items `(1,0)`, `(1,1)`,
   `(4,0)`, `(5,0)` via the exploded branch. The duplicate item and the two
   equal business ids are both kept.
2. A custom operator edits `nested.mark` on one branch; the other branch's
   file still shows `original`.
3. A paused sink with capacity 1 and 2: the source is shown stalled at 4/6
   lines (of 80), the other branch keeps writing, and after resuming all 80
   records on each branch arrive exactly once in order. Empty source, empty
   arrays and two sources with very different speeds are covered too.
4. An operator failure at physical line 5, a real `/dev/full` sink write
   failure, cancellation while a channel is full, and a rejected bad graph
   (checked to create no result directory and leave the source untouched).
   Each fault shows the first-failure identity, no leftover tasks and a
   reopenable partial output whose count matches the summary.

## Tests

```bash
python3 -m pytest tests/test_flow_*.py
python3 tools/flow_stress.py --rows 5000     # reproducible scale assertions
python3 demo/flow/flow_demo.py               # the narrated demo
```

* `tests/test_flow_graph.py` — JSON format, every validation rule, rejection
  before any side effect.
* `tests/test_flow_ops.py` — select/null/bool-vs-int rules, project
  missing-field and deep-copy behaviour, explode paths and errors.
* `tests/test_flow_runtime.py` — split/merge hand identities, broadcast
  isolation, paused sinks at capacities 1 and 2 with resumed completion,
  empty sources/arrays, different source speeds, strict source decoding,
  byte limits, operator/sink failures, cancellation while full, fresh
  directories, result streaming and its early close.
* `tests/test_flow_demo.py` — the packaged demo values.
* `tests/test_flow_cli.py` — `flow check`/`flow run` contracts, exit codes,
  existing commands still available.
* `tests/test_flow_stress.py` — the scale driver as a test.
