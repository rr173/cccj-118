# distagg — incremental “publisher × tag” cost aggregation on SQLite

`distagg` maintains one derived table, keyed by `(publisher, tag)`:

| column | meaning |
|---|---|
| `pairs` | total multiplicity of matching (exposure, tag) pairs |
| `priced` | multiplicity of those pairs whose exposure has a non-null cost |
| `total` | weighted sum of costs over the priced pairs |
| `low`, `high` | minimum / maximum **supported** cost in the group |
| `creatives` | number of distinct creatives contributing to the group |

It is updated incrementally from two base relations that carry multiplicities:

* **E** — `(publisher, creative, cost)`; `cost` is an integer or `null`.
* **T** — `(creative, tag)`.

Input is a stream of transactions, each with a contiguous `seq` and a list of
signed `edits` against those relations.  Exactly identical rows may occur more
than once; they are stored as a non-negative integer weight.  The join is a
plain multiset inner join: a pair of base rows with weights `wE`, `wT`
contributes `wE·wT` pairs, and duplicate tag relations are **not** de-duplicated.
Strings are compared byte-for-byte (BINARY collation): no trimming, no case
folding.

## Install

```bash
python3 -m pip install .          # from the project root
# or, for development:
python3 -m pip install -e .
distagg --version
```

No third-party runtime dependencies; Python ≥ 3.9 and the standard-library
`sqlite3` module are enough.  Python 3.11 + SQLite 3.40 is what the test suite
was exercised on.

## Quick start

```bash
distagg demo /tmp/demo.db          # a packaged 9-transaction scenario
distagg summary /tmp/demo.db       # the derived table, as JSON
distagg base /tmp/demo.db          # the two base relations
distagg receipt /tmp/demo.db 3     # one stored receipt
distagg check /tmp/demo.db         # independent recomputation from base rows
distagg explain /tmp/demo.db       # documented index access model
distagg plans /tmp/demo.db         # EXPLAIN QUERY PLAN of the hot statements
```

Submit one transaction:

```bash
distagg submit analytics.db txn-0001.json
```

The transaction file is a single JSON object:

```json
{
  "seq": 1,
  "edits": [
    {"relation": "E", "row": {"publisher": "p", "creative": "c", "cost": 10}, "weight": 2},
    {"relation": "T", "row": {"creative": "c", "tag": "sale"}, "weight": 3},
    {"relation": "E", "row": {"publisher": "p", "creative": "c", "cost": null}, "weight": 1}
  ]
}
```

`weight` is a non-zero integer: positive adds copies, negative withdraws them.
Identical rows inside one transaction are summed **before** the final base
weight is checked, so “withdraw first, add later” inside one transaction works.

Exit status is `0` for a committed or idempotently replayed transaction, `1` for
a rejected one.  The receipt (or the error object) is JSON on stdout.

### Python API

```python
from distagg import submit_file, query_summary, query_base, get_receipt

result = submit_file("analytics.db", "txn-0001.json")
print(result.replayed, result.receipt["changed_groups"])
print(query_summary("analytics.db"))
print(query_base("analytics.db"))
```

## Offline row-level windows (`distagg.windowing`)

The aggregation engine only keeps group totals, so it cannot answer row-level
questions such as "the other rows before, at and after this record".  The
`window` subcommand is a separate **offline, one-shot** processor: it takes a
shuffled UTF-8 JSONL stream and a declaration and writes a new JSONL stream
with the same row count and source order, each row carrying a `computed`
object (`row_number`, `rank`, `dense_rank`, `lag`/`lead`, and one metric block
per named frame).  SQLite is used purely as a disk-backed cache, index and
sorter; frame membership, exclusions and all aggregates are computed in
project code with exact Python integers — no SQL `OVER`, window extension or
DataFrame window function.

```bash
distagg window run stream.jsonl stream.out.jsonl --declaration declaration.json
```

```json
{
  "partition_by": ["region"],
  "order_by": "ts",
  "value": "amount",
  "lag_lead_k": 2,
  "windows": {
    "around":  {"mode": "ROWS",  "start": -1, "end": 1, "exclude": "none"},
    "same_ts": {"mode": "RANGE", "start": 0,  "end": 0, "exclude": "group"}
  }
}
```

```python
from distagg.windowing import WindowConfig, load_declaration, run_window_job

stats = run_window_job(
    "stream.jsonl", "stream.out.jsonl", load_declaration("declaration.json"),
    WindowConfig(batch_size=1000, spill_threshold=512),
)
```

Each frame emits `count_all`, `count_value`, `sum`, `min`, `max` (the last
four ignore null values).  `RANGE` includes every row at each matching
timestamp; `ROWS` cuts strictly by position; `exclude` is `none`, `current`
or `group`.  Endpoint arithmetic never wraps past 64 bits, frames that fall
fully outside a group are empty, and the destination is replaced atomically
only after the whole job succeeds.  A worked hand example, the bounded-memory
design (spillable monotonic deques, sliding row cache, near-linear passes) and
the resource knobs are documented in [`docs/WINDOWING.md`](docs/WINDOWING.md)
and demonstrated under `demo/windowing/`.

## Staging store (`distagg.staging`)

Alongside the aggregation database, the package ships an **independent disk
staging store**: a small LSM storage engine (WAL + memtable + sorted
segments with a sparse index, all self-encoded — no SQLite/dbm/etc.) for
buffering arbitrary `bytes` key/value edits before handing them downstream
in key order.  It does not extend the aggregation metrics and does not move
the existing computation to a new backend.

```bash
distagg staging put     /tmp/staging 6b6579 76616c   # keys/values as hex
distagg staging batch   /tmp/staging ops.json
distagg staging get     /tmp/staging 6b6579 --stats
distagg staging scan    /tmp/staging --lower 61 --upper 7a
distagg staging flush   /tmp/staging
distagg staging compact /tmp/staging
distagg staging stats   /tmp/staging
```

```python
from distagg.staging import StagingStore

store = StagingStore.open("/tmp/staging", memtable_budget=1 << 20)
commit = store.write_batch([("put", b"k", b"v1"), ("delete", b"gone")])
snap = store.snapshot()              # pinned read-only view at `commit`
store.put(b"k", b"v2")
assert snap.get(b"k") == b"v1"       # unaffected by writes/flush/compact
snap.close()
store.close()
```

Multi-version snapshots and pinned scan iterators, an atomic batch WAL with
checksums, explicit flush/compact with real reclamation numbers, single-writer
locking across processes, and crash recovery that keeps confirmed batches,
drops torn tails and refuses corrupted files.  See
[docs/STAGED_STORE.md](docs/STAGED_STORE.md) for the file formats, the
fault model and the pause points used for kill tests, and
`demo/staging_demo.py`, `demo/staging_scale_demo.py`,
`demo/staging_fault_demo.py` for runnable demonstrations.

### Batch TSV correction importer (`distagg staging import`)

A local batch-import entry point sits on top of the staging store for the
supplier-correction backfill.  It reads one UTF-8 TSV file (header row plus
supplier, correction id, hex key and hex value columns), classifies every
physical line, batches the acceptable corrections into the staging store,
and isolates bad rows with their original bytes, relative path and line
number — without touching the aggregation database or extending the
store's key/value semantics.

```bash
distagg staging import precheck CONFIG.json corrections.tsv state/   # classify only
distagg staging import run       CONFIG.json corrections.tsv store/ state/
distagg staging import status    state/
```

It strictly distinguishes empty fields, the empty byte string (`0x`) and
illegal hexadecimal; exact repeats of a correction id are duplicates while
differing repeats make the whole id a conflict (no later value ever
overwrites an earlier one).  Progress is durable per batch with the file
fingerprint, physical line and counts, so an interrupted run resumes at a
batch boundary, a re-run never double writes, and a changed file is refused
rather than spliced onto old progress.  Report-write failures never report
success; `status` distinguishes a missing report from committed data
batches.  See [docs/IMPORTER.md](docs/IMPORTER.md) and
[demo/staging_import_demo.py](demo/staging_import_demo.py).

### Copybook mainframe record decoder (`distagg copybook`)

A separate offline entry point decodes production records exported from a
mainframe: RDW-framed files of EBCDIC (cp037) text, packed COMP-3 and
binary COMP quantities, and record layouts whose length varies with a
count field.  A restricted COBOL copybook subset (01-49 levels, FILLER,
`PIC X(n)` / `9(n)` / `[S]9(n)V9(m)`, `USAGE DISPLAY|COMP|COMP-3`, fixed
and `DEPENDING ON` OCCURS, and config-selected REDEFINES branches) is
compiled once into a reusable layout; anything outside the subset is
rejected with the copybook line and field.  This entry point does layout
compilation and byte decoding only — it does not reuse the supplier TSV
importer's numbering, isolation or resume protocols.

```bash
distagg copybook check  order_record.cpy --select selectors.json   # layout report
distagg copybook decode order_record.cpy orders.dat --select selectors.json
```

`check` prints every field with full path, type, fixed/variable span,
OCCURS and REDEFINES relations, and honest dynamic offsets behind
variable-length arrays.  `decode` streams the file record by record
(constant memory) and prints one nested JSON object per line — groups stay
objects, OCCURS stay arrays, FILLER is omitted, only the selected
REDEFINES branch appears.  Bad records exit non-zero with record number,
file byte offset, field path and reason on stderr; already-decoded lines
stay emitted.  See [docs/COPYBOOK.md](docs/COPYBOOK.md) for the exact
subset and semantics, and `demo/copybook_demo.py`,
`demo/copybook_fault_demo.py`, `demo/copybook_stream_demo.py` for runnable
demonstrations built on independently constructed demo bytes.

## Local asynchronous flow executor (`distagg flow`)

A supplier file often has to feed two consumers at once: operations wants an
exception detail list, the warehouse wants the exploded, acceptable sub-items,
and some records must go to both.  `distagg flow` runs one finite graph over a
UTF-8 JSONL file: the source reads each physical line exactly once, records
flow through `select` / `project` / `explode` / `merge` operators as they
arrive, and several sinks are written in the same run.  It is a self-contained
asyncio executor with fixed-capacity channels — no external stream engine, no
rereading the input per branch, no unbounded buffering.

```bash
distagg flow check graph.json
distagg flow run   graph.json --results /tmp/flow-run-1 --capacity 2 --max-inflight 2
```

```json
{
  "nodes": [
    {"name": "in",         "type": "source",  "path": "imports.jsonl"},
    {"name": "exceptions", "type": "select",  "field": "status", "condition": "eq", "value": "error"},
    {"name": "acceptable", "type": "select",  "field": "status", "condition": "ne", "value": "error"},
    {"name": "items",      "type": "explode", "field": "items"},
    {"name": "flattened",  "type": "project", "fields": {"order_id": "id", "item": "items"}},
    {"name": "merged",     "type": "merge"},
    {"name": "exception_report", "type": "sink", "path": "exceptions.jsonl"},
    {"name": "ledger",           "type": "sink", "path": "ledger.jsonl"}
  ],
  "edges": [
    {"from": "in", "to": "exceptions"}, {"from": "in", "to": "acceptable"},
    {"from": "exceptions", "to": "exception_report"},
    {"from": "acceptable", "to": "items"}, {"from": "items", "to": "flattened"},
    {"from": "flattened", "to": "merged"}, {"from": "exceptions", "to": "merged"},
    {"from": "merged", "to": "ledger", "capacity": 2}
  ]
}
```

Every output line carries `{"identity": {"source", "line", "expansion_path"},
"row": ...}`.  The identity is the source name, the physical 1-based line and
the 0-based index appended by each `explode`; a business key is never
substituted, and `merge` never de-duplicates — a line that arrives over two
paths is delivered twice.  Several edges out of one node are a broadcast (each
successor gets an independent copy), not a work split.

The graph is validated in full before any source is opened or any output
directory is created: duplicate names, dangling or duplicate edges, cycles,
wrong input counts (`merge` needs two, everything else one; sources and sinks
have none), and nodes that cannot reach a sink are all rejected.  At runtime
each edge is a fixed-capacity channel and each node has a bounded in-flight
allowance, so a slow sink stalls its producers and eventually the source
within a bound fixed at start-up (Σ edge capacities + per-node in-flight
allowance + result buffer).  The summary reports real per-edge `sent`,
`received` and `peak_occupancy`, the source's actual `lines_pulled`, per-sink
counts and whether each sink is complete.  The first operator/source/sink
failure stops the whole run, keeps the original node and record identity, and
wakes tasks blocked on full or empty queues; cancellation and Ctrl-C follow
the same cooperative cleanup.  Every execution uses a fresh result directory.

```python
from distagg.flow import FlowGraph, FlowConfig, load_graph, start_run

graph = load_graph("graph.json")
async with start_run(graph, FlowConfig("/tmp/flow-run-1")) as run:
    summary = await run.wait()
assert summary.ok, summary.error
```

The Python API additionally accepts caller-supplied co-operative async
one-to-many operators (`graph.custom(name, async_fn)`); a graph file never
loads code.  See [docs/FLOW.md](docs/FLOW.md) for the full format, operator
semantics, failure protocol, memory model and measured scale figures, and
`demo/flow/flow_demo.py` for the narrated, asserted demonstration (split and
merge of a messy supplier file with hand-listed identities, broadcast
isolation, paused sinks at capacity 1 and 2, empty inputs, differing source
speeds, operator/sink faults and cancellation).

## Controlled-vocabulary annotation (`distagg lexicon`)

Supplier remarks mention the same material under short names, old names and
mixed case.  `distagg lexicon` keeps the original text untouched and marks
every mention with a unified term id: a maintainer compiles a dictionary of
aliases into a read-only SQLite matching library, and an integrator scans a
remark JSONL file with that library to get the hit positions and the chosen
terms.  It is batch standardisation from a dictionary to business text — no
model inference, no rewriting of the original.

```bash
distagg lexicon compile dictionary.jsonl terms.sqlite
distagg lexicon check   terms.sqlite
distagg lexicon scan    terms.sqlite remarks.jsonl out.jsonl --mode resolved
```

```python
from distagg.lexicon import compile_dictionary, scan_remarks

compile_dictionary("dictionary.jsonl", "terms.sqlite")
stats = scan_remarks("terms.sqlite", "remarks.jsonl", "out.jsonl", mode="all")
```

Each dictionary line gives a unique `term_id`, an integer `priority`
(booleans rejected) and a non-empty `aliases` array of
`{"text", "boundary"}` (`any` or `ascii_word`); the same alias text may
appear under several terms and nothing is dropped for name-sharing.
Matching folds both sides identically (per-character `casefold`, then each
whitespace run becomes one space) and reports half-open intervals in
Unicode characters of the *original* text — `strasse` matches `Straße`
(slice `Straße`), `ss` matches `ß`, a single `s` never matches inside `ß`,
and `ascii_word` only treats ASCII letters/digits/`_` as word characters
(Chinese neighbours do not block a hit).  `all` returns every candidate
(sorted by start, end, term id, alias index); `resolved` greedily keeps
non-overlapping mentions (leftmost start, longest span, then priority,
term id, alias index; adjacent hits allowed).

The matcher is a self-contained Aho–Corasick automaton stored in SQLite
(states, transitions, failure links, output links — one output row per
alias, inheritance via output-link chains, no copied hit lists).  Compile
and scan stream through fixed-capacity LRU caches: neither the trie, the
failure links nor the dictionary is ever mirrored in Python objects, and
the corpus size is never part of the scan's memory bound.  Compile refuses
to overwrite and never leaves a half-built openable library; scan publishes
its result atomically only after the last input line is read.  See
[docs/LEXICON.md](docs/LEXICON.md) for the full contract and
`demo/lexicon_demo.py`, `demo/lexicon_scale_demo.py` (105k aliases, real
timings/RSS) for the asserted demonstrations.

## Semantics in detail

* `pairs` counts every matching (exposure copy, tag copy) pair.  A null cost
  still counts in `pairs` but not in `priced`, `total`, `low`, `high`.
* `priced` is the multiplicity of pairs with a non-null cost; `total` is
  `Σ cost·wE·wT` over those pairs; `low`/`high` are the numerically smallest /
  largest **non-null** cost that has a positive contribution in the group.
* If `priced == 0`, then `total`, `low`, `high` are `null`.  `pairs` may still
  be positive (a group supported only by null-cost exposures).
* Negative costs are valid and participate as ordinary integers (reversals).
* When `pairs` reaches 0 the group is deleted: it no longer appears in
  `query_summary`/`distagg summary`.  A group never exists with `pairs == 0`.
* All counts and money values are computed with Python arbitrary-precision
  integers and stored as canonical decimal TEXT, so nothing ever passes through
  a float and no sum overflows 64 bits.

### Validation and idempotency

A whole transaction is rejected (with nothing written, no receipt, and `seq`
not consumed) if:

* a field is missing, unexpected, or has the wrong type;
* `publisher`/`creative`/`tag` is empty or not a string; `cost` is neither an
  integer nor `null`; a boolean is used where an integer is required;
* `weight` is zero, non-integer, or a boolean;
* the final base weight of any row would be negative;
* `seq` is not the next expected value and is not an already-committed seq;
* `seq` was already committed with a different effective meaning (conflict).

Re-sending a committed `seq` with the same meaning — any edit order, any
splitting/merging of identical rows, any JSON field order, and with
cancelling pairs dropped — returns the **original receipt byte-for-byte**
(`replayed: true`) and changes nothing.  The conflict check compares against
the stored original meaning, not against the current data, so it keeps working
after the original changes have been withdrawn by later transactions.

`edits: []` is a legitimate no-op transaction.

## Incremental equations and read timing

See the module docstring of `src/distagg/engine.py` for the full derivation.
In short, for each touched creative `c`, the engine reads *all* old base rows of
`c` via the creative index (`E_o`, `T_o`), forms `E_n = E_o + Δ`, `T_n = T_o + Δ`
in memory, and applies

```
Δpairs(p,t)   = (Σ_v E_n(p,v))·T_n(t) − (Σ_v E_o(p,v))·T_o(t)
Δpriced(p,t)  = (Σ_{v≠null} E_n(p,v))·T_n(t) − (Σ_{v≠null} E_o(p,v))·T_o(t)
Δtotal(p,t)   = (Σ_{v≠null} v·E_n(p,v))·T_n(t) − (Σ_{v≠null} v·E_o(p,v))·T_o(t)
```

to the affected groups.  Because both sides are read *before* either is
written and the comparison is old→new exactly once, a transaction that changes
both relations cannot double count: adding 2 exposure copies and 3 tag copies
in one transaction adds exactly 6 pairs.

Persistent support rows make the extremes and the distinct-creative count
deletion-safe:

* `group_cost_support(gid, cost) = Σ_c E(p,c,cost)·T(c,t)` — every non-null
  cost with positive support.  `idx_gcs_low` is a partial index on
  `(gid, cost_key COLLATE DECIMAL)`, so `low`/`high` are one index probe each
  in either direction; deleting the current extreme just re-probes the index.
* `group_creatives(gid, creative)` — per-creative contribution.
  `creatives` changes only when such a row appears or disappears, so removing
  the last support of one creative does not require scanning the group.

A submit reads:

1. the stored receipt for `seq` (idempotency/conflict) and `meta.next_seq`;
2. the old base rows of each touched creative only (`idx_base_e_creative`,
   `base_t` primary key);
3. the affected group rows (`ux_agg_groups_pub_tag`), the specific support and
   creative rows being changed, and two index probes for the new `low`/`high`.

It then writes only those rows plus the receipt.  It never runs a global
JOIN/GROUP BY, never clears and rebuilds the result, and never loads the
database into memory.  The only whole-table operation is the explicit
diagnostic `distagg check`.

## Concurrency, crash safety, `seq`

One transaction is one SQLite write transaction (`BEGIN IMMEDIATE`, WAL,
configurable busy timeout).  Base rows, support rows, the summary row, the
receipt and `meta.next_seq` commit atomically.  Two writers simply serialise;
the loser either observes the winner’s receipt (same `seq`, replay) or gets a
`sequence_gap` (different `seq`) and can simply retry.  A process killed before
`COMMIT` leaves nothing behind; a process killed after `COMMIT` leaves the full
committed state and the re-submission is a replay.

For testing, `distagg submit` accepts:

* `--pause-points before_commit,after_commit` — which pause points to honour;
* `--pause-signal-file PATH` — write the reached point (and pid) to a file;
* `--pause-wait-file PATH` — block until the file is deleted;
* `--pause-seconds N` — sleep;
* `--kill-at before_commit|after_commit` — SIGKILL this very process at the
  point (no cleanup) so a test can observe the result from outside.

The `after_commit` pause first waits until a separate connection can see the
committed receipt, so “killed after commit” really means “committed”.

## Access statistics in a receipt

Every receipt has `stats.access` with grouped row counts and the index used per
operation, plus `stats.touched_join_keys`, `stats.signed_contributions`
(per-group `pairs_delta`, `priced_delta`, `total_delta`, `creatives_delta`) and
`stats.changed_groups` (created/updated/removed), and
`stats.touched_join_keys.base_rows_read_per_creative`.  This is what the
“one modified creative among many” demonstration below reports.

## Stored state

Base relations (`base_e`, `base_t`), the derived table (`agg_groups`), the
support rows (`group_cost_support`, `group_creatives`), receipts and the next
`seq` all live in one SQLite file.  Only the edits of the current transaction
and the old rows of its creatives are held in memory; nothing else is loaded.
The file is self-contained and can be copied or reopened by a new process at
any time.

## Tests and demonstrations

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

The suite includes:

* `tests/test_validation.py` — strict validation, exact strings, idempotency,
  conflicts, temporary negatives, cancelling edits, sequence gaps;
* `tests/test_semantics.py` — duplicate tags, two-sided changes, tag migration,
  cost correction, extreme deletion, null-only groups, negative costs,
  arbitrary-precision sums;
* `tests/test_random_reference.py` — random transactions checked step by step
  against `tests/reference.py`, an independent naive interpreter that
  re-aggregates from scratch after every transaction;
* `tests/test_crash_and_concurrency.py` — real subprocesses killed before/after
  commit, six writers racing on the same `seq`;
* `tests/test_incrementality.py` — the same edit on 30 vs 3000 creatives reads
  the same number of rows, all from point/range index probes.
* `tests/test_window_*.py` — strict JSONL/declaration validation, the hand
  window example, randomized differential checks against an independent naive
  interpreter (`tests/reference_window.py`), an external SQLite window-function
  cross-check (64-bit-safe data), edge cases (null values, beyond-64-bit
  integers, int64 timestamp limits, empty input) and atomic-output/CLI
  failures;
* `tools/window_stress.py` — long single-peer, whole-group-frame and
  many-small-group stress runs at N and 2N, recording wall time, rows read and
  peak RSS to show bounded memory and near-linear scaling.
* `tests/test_lexicon_*.py` — folding/mapping unit tests, strict dictionary
  validation, hand-computed matching cases (Straße/ß, whitespace runs,
  ASCII-word boundaries, adjacency, empty remarks), randomized differential
  checks against `tests/reference_lexicon.py` (an independent per-position
  enumerator), library integrity and shuffle invariance, CLI failure
  protocols, and a moderate-scale bounded-memory test.

Independent differential run (also available as a test):

```bash
python3 tools/crosscheck.py --seed 9 --transactions 40
```

### Window demonstration

```bash
demo/windowing/run_demo.sh /tmp/window-demo
```

It runs the hand-computed example `(10,5), (10,null), (12,-2)` through the
four packaged frames and prints the per-row `computed` objects, including the
tie handling, `exclude=group` emptying the self frame, and a lag reference
that legitimately points at a null value.  See `docs/WINDOWING.md` for the
full semantics and the disk/bounded-memory design.

### Local-update demonstration

```bash
python3 demo/local_update.py /tmp/local.db --unrelated 20000
```

It builds a database with `--unrelated` unrelated creatives plus one hot
creative, prints the database size, then applies one transaction that touches
only the hot creative and prints the receipt’s access statistics: join keys
touched, rows read per index, signed contributions and changed groups.  A
`distagg check` afterwards verifies the derived table.  The number of rows read
is independent of `--unrelated`; there is no full re-aggregation.

`demo/demo_transactions/` contains the same nine transactions as JSON files;
`demo/replay.sh /tmp/replay.db` submits them one by one with the CLI and prints
the summary after each step.

## Layout

```
src/distagg/          package (validate, engine, db, api, cli, demo, stats)
src/distagg/staging/  independent LSM staging store (wal, segment, manifest,
                      memtable, store, cli) plus the TSV correction importer
                      (importer, import_cli)
src/distagg/copybook/ copybook-driven mainframe record decoder (parser,
                      compiler, decoder, cli)
src/distagg/windowing/ offline row-level window processor (strict JSONL,
                      declaration, codec, spillable deque, interval
                      aggregator, disk-backed engine, cli)
tests/                pytest suite + independent reference interpreters
tests/test_staging_*.py, tests/reference_staging.py
                      staging store tests (unittest-compatible) + history model
tests/test_staging_import_*.py, tests/data/imports/
                      importer tests and deterministic TSV fixtures
tests/test_copybook_*.py
                      copybook parser/decoder/record/CLI tests
tests/test_window_*.py, tests/reference_window.py
                      row-level window tests + independent naive interpreter
tests/test_lexicon_*.py, tests/reference_lexicon.py
                      lexicon tests (folding, dictionary validation, hand
                      cases, CLI/faults, scale) + independent per-position
                      reference implementation
demo/                 local-update demo, replay script, transaction files,
                      staging store demos (semantics, scale, fault injection)
                      and the batch TSV import demo
demo/lexicon_demo.py, demo/lexicon_scale_demo.py
                      lexicon semantics demo (hand-checked, reference
                      cross-check, process isolation, faults) and the
                      105k-alias scale demo
demo/copybook/        ORDER-FEED sample copybook, selector config and the
                      independent byte-construction script for the demo data
demo/windowing/       hand-computed window example, declaration and demo script
src/distagg/flow/     local asynchronous flow executor (graph format, strict
                      JSONL, bounded channels, operators, merge, sinks, cli)
src/distagg/lexicon/  controlled-vocabulary annotation (strict dictionary
                      JSONL, disk-based Aho-Corasick compiler, read-only
                      library, bounded-cache scanner, cli)
demo/flow/            split/merge demo data, graph and asserted walkthrough
tools/crosscheck.py   standalone random differential checker
tools/window_stress.py bounded-memory / scaling driver for the window job
tools/flow_stress.py  bounded-memory / scaling driver for the flow executor
docs/STAGED_STORE.md  staging store formats and fault model
docs/IMPORTER.md      TSV correction importer: config, classification, resume
docs/COPYBOOK.md      copybook subset, decoding semantics, commands and API
docs/WINDOWING.md     row-level window semantics, disk layout and memory model
docs/FLOW.md          flow graph format, operator semantics, fault protocol and
                      memory model
docs/LEXICON.md       lexicon dictionary format, matching conventions,
                      selection rules, memory model and failure protocol
```
