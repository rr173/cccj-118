# distagg TSV correction importer

The importer is a **local batch-import entry point** for the backfill
workflow: it reads one supplier correction file (UTF-8 TSV), writes the
acceptable corrections into the existing
[staging store](STAGED_STORE.md) in configured row batches, and isolates
every unacceptable row — preserving its original bytes, the file's
relative path and the physical line number — for operations staff.

It deliberately has a narrow scope:

* it only adds the `distagg staging import ...` command group and a
  `distagg.staging.importer` module; the exposure/tag aggregation database
  is never opened or modified;
* a correction is exactly one `("put", key, value)` staging batch op on
  arbitrary `bytes`, so the store's key/value semantics are not extended
  (no namespaces, envelopes or extra record types);
* the existing `distagg` and `distagg staging` entry points and the
  store's independent read/write behaviour are unchanged.

## Input and configuration

Each input file is UTF-8 TSV.  The first physical line declares the column
names; every later physical line is one record with, at minimum, the
supplier, the in-file correction id, a byte key and a value, both as
hexadecimal text.  Extra columns may be declared as allowed.

```json
{
  "format": 1,
  "supplier_column": "supplier",
  "correction_id_column": "correction_id",
  "key_column": "key_hex",
  "value_column": "value_hex",
  "required_columns": ["supplier", "correction_id", "key_hex", "value_hex"],
  "allowed_extra_columns": ["note"],
  "supplier": {"expected": "acme"},
  "batch_rows": 100
}
```

`supplier.expected` is optional; when present, any row whose supplier text
differs is isolated.  `batch_rows` is the maximum number of importable
rows per staging batch.  Configuration is validated up front and a
fingerprint of its canonical JSON is recorded, so a changed configuration
cannot silently resume an old run.

### Empty field vs empty byte string vs illegal hexadecimal

The three cases are strictly distinguished and never conflated:

| raw field text | meaning | outcome |
|---|---|---|
| `""` (empty field) | missing data | isolated, `empty_field` (on a required column) |
| `0x` | the empty byte string `b""` | importable |
| `6162` or `0x6162` | the bytes `b"ab"` | importable |
| `abc` | odd number of hex digits | isolated, `hex_odd_length` |
| `0xzz`, `xy` | non-hex characters | isolated, `hex_bad_character` |

The `0x` prefix is the only way to express the empty byte string; without
it an empty field means "no data".  The empty byte string is a legal key
*and* a legal value in the staging store.

## Classification

Every physical line is parsed from the raw file bytes (a malformed line
can never damage the parsing of the next) and assigned exactly one status:

* **importable** — well-formed and parseable;
* **duplicate** — a later row with the same correction id whose *every*
  column text is identical to the first row's;
* **conflict** — rows sharing a correction id whose fields differ.  All
  rows of a conflicted id are isolated, so a later value can never
  overwrite an earlier one;
* **quarantined** — any other malformed row, with a stable reason code:
  `blank_line`, `field_count_mismatch`, `invalid_utf8`, `empty_field`,
  `supplier_mismatch`, `hex_odd_length`, `hex_bad_character`.

Rows are processed in file order.  Bad rows and conflicts do not block
unrelated legal rows, but no row belonging to a conflicted correction id
ever reaches the store.  Classification is deterministic: precheck and
execution on the same bytes always agree.

## Commands

```bash
# Classify only: writes a report, never opens the staging store.
distagg staging import precheck CONFIG TSV STATE_DIR \
    [--root DIR] [--allow-symlinks] [--import-id ID] [--max-samples N]

# Execute: batch the importable rows into STORE_DIR, record progress in
# STATE_DIR (resumable across interruptions).
distagg staging import run CONFIG TSV STORE_DIR STATE_DIR \
    [--root DIR] [--allow-symlinks] [--import-id ID] [--max-samples N] \
    [--kill-after-batch N] [--fail-report-write]

# Inspect progress without opening the staging store.
distagg staging import status STATE_DIR
```

All output is JSON on stdout; failures are JSON on stdout plus a one-line
summary on stderr and exit code 1 (a SIGKILL test hook returns `-9`).

`--root` constrains the input path: it must resolve inside the root, every
path component is checked with `lstat`, and symlinks are refused unless
`--allow-symlinks` is given explicitly.  Paths that escape the root (via
`..` or an absolute path), dangling symlinks and non-file paths are
rejected with `import_path_error`; the input is never modified or copied.

## Batching, progress and resumability

Importable rows are split, in file order, into contiguous batches of at
most `batch_rows`.  Each batch is one atomic staging `write_batch` (one
store commit).  After a batch is confirmed, a record is appended durably to
`STATE_DIR/batches.jsonl` and `meta.json` is rewritten with:

* the input file's SHA-256 fingerprint and size,
* the configuration fingerprint,
* the last physical line reached and the batch index,
* per-class counts and the staging commit number.

On re-run, the file and configuration fingerprints must match the recorded
ones.  Confirmed batches are skipped, so the same file is never written
twice.  If the file content (or configuration) changed, the run is refused
with `import_fingerprint_mismatch`; to import the new content, start a new
import identity in a fresh state directory (or pass an explicit
`--import-id`).  New content is never spliced onto an old file's progress.

If a hard crash lands a store batch but not its ledger record, the resume
run reconciles the boundary against the live store: a fully present batch
is recorded as `reconciled`; a partially present batch raises
`import_resume_ambiguous` rather than guessing.

## Reports and the quarantine ledger

```
STATE_DIR/
  LOCK                  flock held for the duration of a run
  meta.json             identity, fingerprints, counts, last boundary
  batches.jsonl         one durable record per confirmed batch
  quarantine.jsonl      full original bytes (base64) of every isolated row
  reports/
    precheck.json       precheck output
    run.json            execution output (atomically replaced)
```

The report covers every row's status (import/duplicate/quarantine/conflict),
the per-reason counts, bounded sample locations with truncated raw bytes
and the field indexes at fault, the batch plan and the confirmed commit
range in the store.  It stores the file's **relative** path, never
credentials and never a full copy of the input file; full isolated bytes
live only in `quarantine.jsonl`.

`import status` distinguishes:

* `complete` — data finished and the run report exists;
* `data_committed_report_missing` — batches are committed but the report
  was never finished (a report-write failure cannot be reported as
  success);
* `interrupted_batches_confirmed` / `started_no_batch_confirmed` —
  resumable states, with the exact confirmed batch/line boundary;
* `precheck_only` — no execution metadata exists.

## Error codes

| code | meaning |
|---|---|
| `import_config_error` | missing/invalid configuration fields |
| `import_path_error` | symlink, path escape, missing or non-file input |
| `import_header_error` | header is not valid UTF-8, or has missing/duplicate/undeclared columns |
| `import_fingerprint_mismatch` | file content, configuration, identity or target store changed |
| `import_state_error` | progress directory is damaged or internally inconsistent |
| `import_resume_ambiguous` | a batch boundary cannot be reconciled with the store |
| `import_report_write_failed` | report write failed after data batches committed |

## Reproducible demo and tests

```bash
PYTHONPATH=src python3 -I demo/staging_import_demo.py
python3 -m unittest tests.test_staging_import_unit \
                      tests.test_staging_import_integration \
                      tests.test_staging_import_cli
python3 -I tests/data/imports/generate_fixtures.py   # byte-deterministic fixtures
```

The shipped fixtures under `tests/data/imports/` are regenerated
deterministically (no randomness); a test asserts regeneration produces
byte-identical files.
