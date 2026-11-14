# distagg — operations guide

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

This guide is the operator-facing description: database layout, invariants,
error codes, receipt fields, recovery behaviour and the runbooks for the
acceptance scenarios.  It is a copy of the README plus the extra runbooks below.

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

Independent differential run (also available as a test):

```bash
python3 tools/crosscheck.py --seed 9 --transactions 40
```

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
src/distagg/         package (validate, engine, db, api, cli, demo, stats)
tests/               pytest suite + independent reference interpreter
demo/                local-update demo, replay script, transaction files
tools/crosscheck.py  standalone random differential checker
```


## Runbooks for the acceptance scenarios

All commands assume the package is installed (`distagg` on `PATH`) and
`DB` points at a database file.

### Duplicate tags and exact strings

```bash
distagg submit "$DB" demo/demo_transactions/01.json   # 2 E copies × 3 T copies = 6
distagg summary "$DB"
```

### Two-sided change in one transaction

```bash
distagg submit "$DB" demo/demo_transactions/02.json   # −1 E copy and −1 T copy → 2 pairs
```

### Tag migration / cost correction

```bash
distagg submit "$DB" demo/demo_transactions/03.json   # remove old tag, add new tag in one txn
distagg submit "$DB" demo/demo_transactions/04.json   # remove old cost row, add new cost row
```

### Duplicate cost support

```bash
distagg submit "$DB" demo/demo_transactions/07.json
```

### Null-cost-only group, then empty group

```bash
distagg submit "$DB" demo/demo_transactions/08.json   # priced=0, total/low/high null
distagg submit "$DB" demo/demo_transactions/09.json   # group disappears
```

### Cancelling edits, final negative weight

```bash
distagg submit "$DB" demo/demo_transactions/06.json   # no-op
# a transaction whose net weight drives a base row negative exits 1, prints
# {"error":"negative_weight",...} and leaves next_seq unchanged
```

### Equivalent / conflicting resend, sequence gaps

```bash
distagg submit "$DB" demo/demo_transactions/01.json   # replayed: true, same receipt
distagg receipt "$DB" 1                               # identical to the first response
# a different meaning for a committed seq exits 1 with {"error":"seq_conflict"}
```

### Two writers

```bash
distagg submit "$DB" a.json & distagg submit "$DB" b.json & wait
# same seq → one commit and replays; different seq → loser gets sequence_gap
# and succeeds when retried after the winner
```

### Kill before / after commit

```bash
distagg submit "$DB" txn.json --pause-points after_commit \
    --pause-signal-file /tmp/pause --pause-wait-file /tmp/hold &
# wait for /tmp/pause, then: kill -9 <pid>
# before_commit → nothing applied, re-submit succeeds
# after_commit  → data and receipt present, re-submit replays the receipt
```

### Local scope

```bash
python3 demo/local_update.py /tmp/local.db --unrelated 20000
```

### Diagnostic invariants

```bash
distagg check "$DB"        # exit 0 and {"ok": true}
distagg plans "$DB"        # EXPLAIN QUERY PLAN for the hot statements
```

## Error codes

| code | meaning |
|---|---|
| `invalid_transaction` | malformed object, wrong type, empty string, zero/bool weight, seq < 1 |
| `negative_weight` | final base weight would be negative; whole transaction rejected |
| `sequence_gap` | seq is beyond the next expected value; retry after filling the gap |
| `seq_conflict` | seq already committed with a different effective meaning |
| `database_error` | storage-level problem (e.g. unsupported schema version) |

## Receipt fields

`seq`, `status`, `fingerprint`, `committed_at`, `net_edits`,
`touched_creatives`, `base_changes`, `contributions`, `changed_groups`
(`before`/`after`/`pairs_delta`/`priced_delta`/`total_delta`/`creatives_delta`/
`action`), `groups_created`, `groups_updated`, `groups_removed`, `stats`
(`touched_join_keys`, `signed_contributions`, `changed_groups`,
`base_row_changes`, `access`).
