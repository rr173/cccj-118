#!/usr/bin/env python3
"""Reproducible end-to-end demonstration of the TSV correction importer.

Usage::

    PYTHONPATH=src python3 -I demo/staging_import_demo.py [WORKDIR]

The script regenerates its own fixture TSV files (deterministic, no
randomness) inside an empty working directory, then drives the real
``distagg staging import`` command line through:

  1. a precheck that classifies every row without touching the store;
  2. an execution that batches the acceptable corrections into the staging
     store while bad rows, duplicates and conflicts are isolated;
  3. a hard SIGKILL at a batch boundary followed by a resume, proving the
     final keys match a clean one-shot run and nothing is double counted;
  4. a modified file whose resume is refused on fingerprint mismatch;
  5. a simulated report-write failure where the committed data range is
     still queryable through ``import status``.

The original ``distagg`` entry point and the independent staging store are
used unchanged; the importer only adds the ``staging import`` command group.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

CONFIG = {
    "format": 1,
    "supplier_column": "supplier",
    "correction_id_column": "correction_id",
    "key_column": "key_hex",
    "value_column": "value_hex",
    "required_columns": ["supplier", "correction_id", "key_hex", "value_hex"],
    "allowed_extra_columns": ["note"],
    "supplier": {"expected": "acme"},
    "batch_rows": 2,
}

HEADER = "supplier\tcorrection_id\tkey_hex\tvalue_hex\tnote\n"


def line(supplier, cid, key, value, note=""):
    return f"{supplier}\t{cid}\t{key}\t{value}\t{note}\n"


def build_files(workdir):
    in_dir = os.path.join(workdir, "in")
    os.makedirs(in_dir)
    with open(os.path.join(in_dir, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(CONFIG, handle, indent=2, sort_keys=True)
    # Five importable rows (batches of 2 -> 2/2/1), one duplicate, one
    # conflict pair, and one row per quarantine reason.
    tsv = HEADER
    tsv += line("acme", "C001", "61", "62", "a")
    tsv += line("acme", "C002", "0x", "0x", "empty-bytes")
    tsv += line("acme", "C003", "6b", "76", "c")
    tsv += line("acme", "C001", "61", "62", "a")          # exact duplicate
    tsv += line("acme", "C004", "", "01", "empty-field")  # empty field
    tsv += line("acme", "C005", "abc", "01", "odd-hex")   # odd length
    tsv += line("rival", "C006", "61", "62", "supplier")  # supplier mismatch
    tsv += "\n"                                            # blank line
    tsv += "acme\tC007\t61\t62\n"                         # field count mismatch
    tsv += line("acme", "C008", "64", "65", "conflict-a")
    tsv += line("acme", "C008", "64", "66", "conflict-b")
    tsv += line("acme", "C009", "68", "69", "i")
    tsv += line("acme", "C010", "6a", "6b", "j")
    with open(os.path.join(in_dir, "corrections.tsv"), "w",
              encoding="utf-8", newline="") as handle:
        handle.write(tsv)
    return in_dir


def cli(*args, allow=()):
    proc = subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": SRC},
    )
    if proc.returncode not in (0, -9, *allow):
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(args)} -> {proc.returncode}")
    return proc


def show(title):
    print("\n=== %s ===" % title)


def main():
    workdir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="import-demo-")
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir)
    in_dir = build_files(workdir)
    config = os.path.join(in_dir, "config.json")
    tsv = os.path.join(in_dir, "corrections.tsv")
    store = os.path.join(workdir, "store")
    state = os.path.join(workdir, "state")
    print("working directory:", workdir)

    show("1. precheck (store is never opened)")
    pre = json.loads(cli("staging", "import", "precheck", config, tsv,
                         os.path.join(workdir, "pre-state"), "--root", in_dir).stdout)
    print("counts:", json.dumps(pre["counts"]))
    print("reasons:", json.dumps(pre["quarantine_reasons"]))
    print("planned batches:", [b["row_count"] for b in pre["batches"]])

    show("2. execution: acceptable rows batched, rest isolated")
    run = json.loads(cli("staging", "import", "run", config, tsv, store, state,
                         "--root", in_dir).stdout)
    print("committed batches:", run["staging"]["confirmed_batches"],
          "rows:", run["staging"]["confirmed_rows"])
    scan = json.loads(cli("staging", "scan", store).stdout)
    print("store keys:", [e["key_hex"] or "(empty)" for e in scan["entries"]])
    print("conflict ids:", run["conflict_correction_ids"], "-> none of their keys landed")

    show("3. SIGKILL at batch boundary, then resume")
    store2, state2 = os.path.join(workdir, "s2"), os.path.join(workdir, "st2")
    killed = cli("staging", "import", "run", config, tsv, store2, state2,
                 "--root", in_dir, "--kill-after-batch", "1")
    print("killed process return code:", killed.returncode)
    status = json.loads(cli("staging", "import", "status", state2).stdout)
    print("status after kill:", status["status"],
          "confirmed batches:", status["confirmed_batches"])
    resumed = json.loads(cli("staging", "import", "run", config, tsv, store2, state2,
                             "--root", in_dir).stdout)
    scan2 = json.loads(cli("staging", "scan", store2).stdout)
    print("resumed keys:", [e["key_hex"] or "(empty)" for e in scan2["entries"]])
    print("resumed == clean run:", scan2["entries"] == scan["entries"])

    show("4. changed content refuses to splice onto old progress")
    changed = os.path.join(in_dir, "changed.tsv")
    shutil.copyfile(tsv, changed)
    with open(changed, "a", encoding="utf-8") as handle:
        handle.write(line("acme", "C999", "7a", "7b", "new"))
    proc = cli("staging", "import", "run", config, changed, store, state,
               "--root", in_dir, allow=(1,))
    print("resume exit code:", proc.returncode, "error:", json.loads(proc.stdout)["error"])

    show("5. report write failure keeps committed data queryable")
    store3, state3 = os.path.join(workdir, "s3"), os.path.join(workdir, "st3")
    proc = cli("staging", "import", "run", config, tsv, store3, state3,
               "--root", in_dir, "--fail-report-write", allow=(1,))
    print("report failure exit code:", proc.returncode,
          "error:", json.loads(proc.stdout)["error"])
    status3 = json.loads(cli("staging", "import", "status", state3).stdout)
    print("status:", status3["status"], "committed rows:", status3["confirmed_rows"],
          "report present:", status3["reports"]["run.json"]["exists"])
    print("store commit despite missing report:",
          json.loads(cli("staging", "stats", store3).stdout)["commit"])

    show("done")
    print("inspect reports and the quarantine ledger under:", workdir)


if __name__ == "__main__":
    main()
