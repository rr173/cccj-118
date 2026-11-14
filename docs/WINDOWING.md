# Offline row-level window processing

The `distagg window run` command turns one shuffled UTF-8 JSONL transaction
stream plus a window declaration into a new JSONL stream that has the **same
row count and source order**, each row carrying a `computed` object.  It
answers row-level questions ("the rows before, at and after this one") that
the group-total aggregation in the rest of distagg cannot express.

## Command and API

```bash
distagg window run INPUT.jsonl OUTPUT.jsonl --declaration DECL.json \
    [--sqlite-cache-size-kib N] [--batch-size N] \
    [--spill-threshold N] [--max-row-bytes N] [--work-dir DIR]
```

```python
from distagg.windowing import (
    WindowConfig, load_declaration, run_window_job,
)

declaration = load_declaration("decl.json")
stats = run_window_job(
    "input.jsonl", "output.jsonl", declaration,
    WindowConfig(batch_size=1000, spill_threshold=512,
                 sqlite_cache_size_kib=8192, max_row_bytes=16 * 1024 * 1024),
)
```

The command exits non-zero on any failure and prints one JSON error object on
stdout plus a one-line summary on stderr.

## Declaration

```json
{
  "partition_by": ["region", "segment"],
  "order_by": "ts",
  "value": "amount",
  "lag_lead_k": 2,
  "windows": {
    "around":  {"mode": "ROWS",  "start": -1, "end": 1, "exclude": "none"},
    "same_ts": {"mode": "RANGE", "start": 0,  "end": 0, "exclude": "group"}
  }
}
```

* `partition_by`: non-empty list of field names.  Values must be strings or
  null; null groups with null, strings keep their exact text, and composite
  keys preserve component boundaries via a tagged, escaped blob codec.
* `order_by`: one field holding a signed 64-bit integer timestamp.  Null,
  float and boolean are rejected.
* `value`: arbitrary-precision integer or null.  Bools and floats are
  rejected; negatives and values beyond 64 bits are preserved exactly.
* `lag_lead_k`: positive integer distance.
* `windows`: one or more named frames.  `mode` is `ROWS` or `RANGE`,
  `start <= end` are mathematical integers (they may exceed 64 bits), and
  `exclude` is `none`, `current` or `group`.

Unknown declaration members are rejected so a typo cannot silently disable
part of a job.

## Semantics

Within a partition, rows are ordered by `(timestamp, source_row)` and given a
1-based position `i` and timestamp `t`.  Rows sharing a timestamp form one
peer (tie) group regardless of their source row.

* **ROWS** selects positions in `[i + start, i + end]`.
* **RANGE** selects every row whose timestamp is in `[t + start, t + end]`,
  i.e. all peers at each included timestamp.
* Both ends are inclusive; out-of-group parts are clipped.  A frame whose two
  endpoints both fall outside the group is **empty** — an endpoint is never
  hard-clamped onto the boundary to invent a member.
* Endpoint arithmetic uses Python integers, so `t + offset` never wraps even
  when it overflows 64 bits.
* After membership, `current` removes the current row and `group` removes the
  whole same-timestamp peer (only the part that lies inside the frame).
* Every frame emits `count_all` (all members), `count_value`, `sum`, `min`,
  `max` (the latter four ignore `value = null`).  With no non-null value,
  `sum/min/max` are null and both counts are 0.

Each row also receives, independent of every frame:

* `row_number` — the 1-based position in the `(ts, source_row)` order;
* `rank` — the position of the first row of the current peer;
* `dense_rank` — the 1-based number of distinct peer groups seen so far;
* `lag` / `lead` — the row `k` positions back / forward by **position**,
  returning `{"source_row", "value"}`.  A referenced null value is still a
  valid reference (it is never skipped); crossing the partition boundary
  yields null.

The output row shape is fixed and delivered ordered by `source_row`, never in
the internal timestamp order:

```json
{"source_row": 3, "row": { ...original object... },
 "computed": {"row_number": ..., "rank": ..., "dense_rank": ...,
              "lag": ..., "lead": ..., "windows": {"name": { ...5 metrics... }}}}
```

## Hand example

For one group with ordered pairs `(10, 5), (10, null), (12, -2)`:

* row 1 under RANGE `[0,0]` exclude `none` sees both timestamp-10 rows:
  `count_all=2, count_value=1, sum=min=max=5`; with exclude `group` the frame
  is empty.
* row 2 under ROWS `[-1,1]` exclude `current` keeps the values 5 and -2:
  `sum=3, min=-2, max=5`.
* row 3 under ROWS `[1,2]` is an empty frame.
* ranks are `1, 1, 3`; dense ranks are `1, 1, 2`; with `k=1` row 3's lag
  points at row 2 whose value is null.

This scenario ships under `demo/windowing/` and is asserted value-by-value in
`tests/test_window_hand_example.py`; reproduce it with
`demo/windowing/run_demo.sh`.

## On-disk intermediate representation

All scratch state lives in a private SQLite database inside a temporary
workspace created next to the output (or under `--work-dir`).  SQLite provides
storage, indexes and ordering only — **no SQL `OVER`, window extension or
DataFrame window function is used anywhere in the product**.

| table | role |
|---|---|
| `records(source_row PK, part_key BLOB, ts, pos, peer_start, peer_end, value_text TEXT, raw_json TEXT)` | every input row; values and raw JSON are stored as text so arbitrary-precision integers never touch SQLite's 64-bit integer SUM |
| `deque_store(deque_id, seq, pos, value_text)` | spill area for monotonic min/max deques that exceed the in-memory threshold |
| `results(source_row PK, payload TEXT)` | finished output objects, read back in source order |

Indexes: `(part_key, ts, source_row)` drives deterministic ordering and RANGE
endpoint seeks; `(part_key, pos)` drives the position scans, lag/lead point
lookups and the sliding row cache.

### Passes

1. **Ingest** — strict JSONL parse and type validation, batched inserts.
   Blank lines, duplicate JSON members and wrong types fail with the physical
   line number; identical rows stay distinct.
2. **Ordering** — one streaming scan of the `(part_key, ts, source_row)`
   index assigns `pos`, `peer_start` and `peer_end`.  A large peer is updated
   with a single indexed statement, never materialized as a Python list.
3. **Computation** — partitions are streamed in position order.  RANGE
   endpoints are index seeks whose exact membership is decided in Python with
   mathematical integers; ROWS endpoints are integer arithmetic.  Metrics come
   from monotonic interval aggregators.
4. **Delivery** — `results` are read ordered by `source_row` into a temporary
   output file, fsynced, and only then `os.replace`d onto the destination.

## Bounded memory and near-linear cost

The engine never holds a whole partition, peer group or frame in a Python
list.

* **Sliding row cache** (`aggregator.RowCache`) keeps at most two batches of
  rows per partition; a cursor that falls behind the evicted prefix is served
  by an indexed point lookup instead of reloading the group.
* **Interval aggregator** maintains each inclusive interval with two cursors
  that only move forward.  Sum and the non-null count are running counters;
  min/max use monotonic deques (`SpillableDeque`) that spill into
  `deque_store` once they pass `spill_threshold`.  Each position is inserted
  and removed at most once, so the per-frame work over a whole job is linear.
* **Two monotonic streams per frame** carry the pieces before and after an
  excluded row/peer; splitting them keeps every stream's endpoints
  non-decreasing even across a large same-timestamp peer.
* RANGE endpoint seeks are index-assisted and stop as soon as the ordered
  scan crosses the interval, so they do not rescan the frame per output row.

With the number of declared frames fixed, total work is near-linear in the
number of input rows (indexed sort/scan is `N log N` at most); there is no
quadratic per-row frame enumeration.

### Peak memory composition

Peak resident memory is bounded by, and dominated by:

1. the SQLite page cache, sized by `--sqlite-cache-size-kib`;
2. at most two `--batch-size` row batches in the sliding cache, plus the
   insert/update/result batches of the same size;
3. at most `--spill-threshold` entries per monotonic deque while it stays
   resident (there are four deques per frame);
4. one parsed input object and one output payload under construction;
5. the single-line cap `--max-row-bytes` bounds the largest individual record.

`tools/window_stress.py` runs a long single peer, a whole-group frame and many
small groups at N and 2N with a cache deliberately smaller than the input, and
records wall time, rows read and the child's peak RSS from `/proc/<pid>/status`
(VmHWM).  Doubling the input yields roughly a 1.5–1.8x time increase while peak
RSS stays essentially flat, confirming there is no group-sized buffering or
per-row frame rescan.

## Failure and cleanup semantics

* Input and output must be different files (compared by real path).
* All work, including the output, lands in the temporary workspace first.  A
  bad final input line, an invalid declaration or a write failure aborts
  before `os.replace`, so an existing result is never overwritten or
  truncated.
* The temporary workspace is removed in a `finally` block and only the
  directory this job created is ever deleted.
* The job processes one complete file; no long-term revision, watermark or
  resume state is maintained.
