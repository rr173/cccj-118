"""Integration tests for the TSV importer against a real staging store.

Covers the acceptance scenarios that need an on-disk
:class:`~distagg.staging.StagingStore`: precheck/run classification parity,
batched landing of good rows amid bad ones, conflict ids never reaching the
store, interruption/resume with identical final keys and no double
counting, fingerprint refusal on changed content, and the distinction
between an unfinished report and already committed data batches.
"""

import base64
import json
import os
import shutil
import tempfile
import unittest

from distagg.staging import (
    ImportFingerprintError,
    ImportReportError,
    ImportStateError,
    StagingStore,
    classify_file,
    load_config,
    precheck,
    read_status,
    run_import,
)
from distagg.staging.importer import ImportState

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "imports")
CONFIG_PATH = os.path.join(DATA, "supplier_config.json")
MIXED = os.path.join(DATA, "corrections_mixed.tsv")
GOOD = os.path.join(DATA, "corrections_good.tsv")
BAD_UTF8 = os.path.join(DATA, "bad_utf8.tsv")


def write_tsv(directory: str, name: str, text: str) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def store_items(store_dir: str):
    store = StagingStore.open(store_dir, read_only=True)
    try:
        with store.scan() as iterator:
            items = {key.hex(): value.hex() for key, value in iterator}
        commit = store.commit_number
    finally:
        store.close()
    return items, commit


class ImportIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="import-integ-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = load_config(CONFIG_PATH)
        self.store_dir = os.path.join(self.dir, "store")
        self.state_dir = os.path.join(self.dir, "state")

    # -- precheck vs run parity ----------------------------------------------
    def test_precheck_and_run_agree_on_classification(self):
        pre_state = os.path.join(self.dir, "pre-state")
        pre = precheck(self.config, MIXED, pre_state, root=DATA)
        # After precheck alone, neither the run store nor a batch ledger exists.
        self.assertFalse(os.path.exists(self.store_dir))
        self.assertFalse(os.path.exists(os.path.join(pre_state, "batches.jsonl")))
        run = run_import(self.config, MIXED, self.store_dir, self.state_dir, root=DATA)
        self.assertEqual(pre["counts"], run["counts"])
        self.assertEqual(pre["quarantine_reasons"], run["quarantine_reasons"])
        self.assertEqual(
            [(row["line_no"], row["status"], row.get("reason")) for row in pre["rows"]],
            [(row["line_no"], row["status"], row.get("reason")) for row in run["rows"]],
        )
        # The precheck state directory contains only a LOCK and its report.
        self.assertEqual(sorted(os.listdir(pre_state)), ["LOCK", "reports"])
        self.assertTrue(os.path.exists(os.path.join(pre_state, "reports",
                                                    "precheck.json")))

    def test_precheck_writes_no_batches_and_never_opens_store(self):
        pre = precheck(self.config, GOOD, self.state_dir, root=DATA)
        self.assertEqual(pre["batch_commitments"], [])
        self.assertIsNone(pre["staging"])
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "batches.jsonl")))

    # -- mixed good/bad/duplicate rows ---------------------------------------
    def test_good_rows_land_in_batches_bad_rows_keep_raw_bytes(self):
        report = run_import(self.config, MIXED, self.store_dir, self.state_dir, root=DATA)
        # 6 importable rows at batch_rows=3 -> exactly two store commits.
        self.assertEqual(report["staging"]["confirmed_batches"], 2)
        self.assertEqual(report["staging"]["confirmed_rows"], 6)
        commitments = report["batch_commitments"]
        self.assertEqual([c["row_count"] for c in commitments], [3, 3])
        self.assertEqual(commitments[0]["first_line"], 2)
        self.assertEqual(commitments[-1]["last_line"], 18)

        items, commit = store_items(self.store_dir)
        # Keys from lines 2,3,4 (6162, empty, 6b) and 16,17,18 (66,68,6a).
        self.assertEqual(set(items), {"", "6162", "6b", "66", "68", "6a"})
        self.assertEqual(commit, 2)

        # Quarantine file retains the full original bytes of isolated rows.
        qpath = os.path.join(self.state_dir, "quarantine.jsonl")
        with open(MIXED, "rb") as handle:
            disk = handle.read().split(b"\n")
        with open(qpath, encoding="utf-8") as qhandle:
            records = [json.loads(line) for line in qhandle]
        self.assertEqual(len(records), 9)  # 7 quarantined + 2 conflict
        for record in records:
            raw = base64.b64decode(record["raw_line_b64"])
            self.assertEqual(raw, disk[record["line_no"] - 1])

    def test_conflict_id_rows_never_reach_store(self):
        report = run_import(self.config, MIXED, self.store_dir, self.state_dir, root=DATA)
        conflict_ids = report["conflict_correction_ids"]
        self.assertEqual(conflict_ids, ["C010"])
        items, _ = store_items(self.store_dir)
        # C010 would have written key 64; it must be absent entirely.
        self.assertNotIn("64", items)
        conflict_rows = [r for r in report["rows"] if r.get("correction_id") == "C010"]
        self.assertEqual(len(conflict_rows), 2)
        self.assertTrue(all(r["status"] == "conflict" for r in conflict_rows))

    def test_duplicates_are_not_written_but_do_not_block_other_rows(self):
        report = run_import(self.config, MIXED, self.store_dir, self.state_dir, root=DATA)
        # C001 appears twice and C002 twice (exact duplicates); only first
        # occurrences were candidates, and they did land.
        items, _ = store_items(self.store_dir)
        self.assertIn("6162", items)
        self.assertEqual(report["counts"]["duplicate"], 2)
        # Duplicate line numbers are not part of any committed batch.
        committed_lines = {
            line for c in report["batch_commitments"] for line in c["physical_lines"]
        }
        self.assertNotIn(5, committed_lines)
        self.assertNotIn(13, committed_lines)

    def test_all_quarantined_file_commits_nothing(self):
        parsed = classify_file(BAD_UTF8, self.config)
        # bad_utf8.tsv: two good rows survive, one invalid-utf8 row isolated.
        self.assertEqual(parsed.counts["importable"], 2)
        self.assertEqual(parsed.counts["quarantined"], 1)
        # Build a wholly-bad file instead: every data row is structurally bad.
        header = "supplier\tcorrection_id\tkey_hex\tvalue_hex\tnote\n"
        path = write_tsv(self.dir, "allbad.tsv", header + "only\tthree\n\n")
        report = run_import(self.config, path, self.store_dir, self.state_dir, root=self.dir)
        self.assertEqual(report["staging"]["confirmed_batches"], 0)
        # The store directory was still initialised but holds no commits.
        self.assertTrue(os.path.isdir(self.store_dir))
        _, commit = store_items(self.store_dir)
        self.assertEqual(commit, 0)

    # -- idempotent rerun -----------------------------------------------------
    def test_rerun_same_file_does_not_double_write(self):
        first = run_import(self.config, GOOD, self.store_dir, self.state_dir, root=DATA)
        items_before, commit_before = store_items(self.store_dir)
        second = run_import(self.config, GOOD, self.store_dir, self.state_dir, root=DATA)
        items_after, commit_after = store_items(self.store_dir)
        self.assertEqual(items_before, items_after)
        self.assertEqual(commit_before, commit_after)
        self.assertEqual(
            first["staging"]["confirmed_rows"], second["staging"]["confirmed_rows"]
        )
        # The batch ledger was not appended to.
        with ImportState(self.state_dir) as state:
            ledger = state.read_batches()
        self.assertEqual(len(ledger), 3)
        self.assertEqual(second["staging"]["last_commit"], first["staging"]["last_commit"])

    # -- interruption and resume ---------------------------------------------
    def test_resume_via_pause_hook_and_final_equality(self):
        interrupted_dir = os.path.join(self.dir, "interrupted")
        clean_dir = os.path.join(self.dir, "clean")
        os.makedirs(interrupted_dir)
        interrupted_store = os.path.join(interrupted_dir, "store")
        interrupted_state = os.path.join(interrupted_dir, "state")

        seen = []

        def pause(point, info):
            seen.append(info["batch_index"])
            if info["batch_index"] == 2:
                raise _PauseInterrupt()

        with self.assertRaises(_PauseInterrupt):
            run_import(self.config, GOOD, interrupted_store, interrupted_state,
                       root=DATA, pause=pause)
        # Two batches were confirmed before the interruption.
        status = read_status(interrupted_state)
        self.assertEqual(status["confirmed_batches"], 2)
        self.assertEqual(status["confirmed_rows"], 6)
        partial_items, partial_commit = store_items(interrupted_store)
        self.assertEqual(partial_commit, 2)

        # Resume; the remaining batch lands without rewriting confirmed rows.
        resumed = run_import(self.config, GOOD, interrupted_store, interrupted_state, root=DATA)
        self.assertEqual(resumed["staging"]["confirmed_batches"], 3)
        self.assertEqual(resumed["staging"]["confirmed_rows"], 8)
        resumed_items, resumed_commit = store_items(interrupted_store)
        self.assertEqual(resumed_commit, 3)

        # A clean one-shot run on a fresh store ends with identical key/values.
        clean_report = run_import(
            self.config, GOOD,
            os.path.join(clean_dir, "store"), os.path.join(clean_dir, "state"), root=DATA,
        )
        clean_items, clean_commit = store_items(os.path.join(clean_dir, "store"))
        self.assertEqual(resumed_items, clean_items)
        self.assertEqual(resumed_commit, clean_commit)
        self.assertEqual(clean_report["staging"]["confirmed_rows"], 8)

    def test_resume_reconciles_a_batch_whose_ledger_record_is_missing(self):
        # Model the torn window where a batch reached the store (WAL fsynced)
        # but the importer died before appending its batches.jsonl record.
        # We drive two batches, keep only the first ledger line, and let the
        # resume run detect that batch 2 is already fully present.
        state = self.state_dir
        with self.assertRaises(_PauseInterrupt):
            run_import(self.config, GOOD, self.store_dir, state, root=DATA,
                       pause=_raise_after(2))
        ledger = os.path.join(state, "batches.jsonl")
        with open(ledger, "rb") as handle:
            first_line = handle.readline()
        with open(ledger, "wb") as handle:
            handle.write(first_line)  # batch 2's record is now missing
        report = run_import(self.config, GOOD, self.store_dir, state, root=DATA)
        reconciled = [c for c in report["batch_commitments"] if c.get("reconciled")]
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["batch_index"], 2)
        items, commit = store_items(self.store_dir)
        self.assertEqual(len(items), 8)
        self.assertEqual(commit, 3)  # only batch 3 was newly written

    def test_resume_ambiguous_when_batch_is_partially_present(self):
        from distagg.staging import ImportResumeError

        state = self.state_dir
        with self.assertRaises(_PauseInterrupt):
            run_import(self.config, GOOD, self.store_dir, state, root=DATA,
                       pause=_raise_after(2))
        # Remove one key that batch 2 wrote, then drop its ledger record:
        # the candidate batch is now only partially present -> ambiguous.
        ledger = os.path.join(state, "batches.jsonl")
        with open(ledger, "rb") as handle:
            first_line = handle.readline()
        with open(ledger, "wb") as handle:
            handle.write(first_line)
        store = StagingStore.open(self.store_dir)
        try:
            # The fixture key_hex "6b04" decodes to the raw bytes k\\x04.
            store.delete(bytes.fromhex("6b04"))
        finally:
            store.close()
        checker = StagingStore.open(self.store_dir, read_only=True)
        try:
            self.assertIsNone(checker.get(b"6b04"))  # deletion is durable
        finally:
            checker.close()
        with self.assertRaises(ImportResumeError):
            run_import(self.config, GOOD, self.store_dir, state, root=DATA)

    # -- changed content refusal ---------------------------------------------
    def test_changed_content_refuses_resume_but_fresh_identity_runs(self):
        header = "supplier\tcorrection_id\tkey_hex\tvalue_hex\tnote\n"
        original = header + "acme\tA1\t61\t62\tone\n"
        path = write_tsv(self.dir, "changing.tsv", original)
        run_import(self.config, path, self.store_dir, self.state_dir, root=self.dir)
        # Same path, modified bytes -> the fingerprint must reject a resume.
        write_tsv(self.dir, "changing.tsv",
                  original + "acme\tZ999\t7a\t7b\tnew\n")
        with self.assertRaises(ImportFingerprintError):
            run_import(self.config, path, self.store_dir, self.state_dir, root=self.dir)
        # The previously committed data is untouched.
        items_before, commit_before = store_items(self.store_dir)
        # A new identity in a new state directory starts cleanly.
        fresh = run_import(
            self.config, path,
            os.path.join(self.dir, "store2"), os.path.join(self.dir, "state2"),
            root=self.dir, import_id="imp-fresh-0001",
        )
        self.assertEqual(fresh["status"], "complete")
        self.assertEqual(store_items(self.store_dir), (items_before, commit_before))

    def test_config_change_refuses_resume(self):
        run_import(self.config, GOOD, self.store_dir, self.state_dir, root=DATA)
        other = load_config(CONFIG_PATH)
        from distagg.staging import parse_config

        changed_cfg = parse_config(dict(other.raw, batch_rows=2))
        with self.assertRaises(ImportFingerprintError):
            run_import(changed_cfg, GOOD, self.store_dir, self.state_dir, root=DATA)

    def test_state_dir_reused_by_other_identity_is_refused(self):
        run_import(self.config, GOOD, self.store_dir, self.state_dir, root=DATA)
        with self.assertRaises(ImportFingerprintError):
            run_import(
                self.config, GOOD, self.store_dir, self.state_dir,
                root=DATA, import_id="imp-someone-else",
            )

    # -- report failure vs committed data ------------------------------------
    def test_report_write_failure_does_not_report_success(self):
        with self.assertRaises(ImportReportError):
            run_import(self.config, GOOD, self.store_dir, self.state_dir,
                       root=DATA, fail_report_write=True)
        # Data batches are committed and queryable.
        items, commit = store_items(self.store_dir)
        self.assertEqual(commit, 3)
        self.assertEqual(len(items), 8)
        status = read_status(self.state_dir)
        self.assertEqual(status["status"], "data_committed_report_missing")
        self.assertFalse(status["reports"]["run.json"]["exists"])
        # Re-running normally completes the report without extra writes.
        report = run_import(self.config, GOOD, self.store_dir, self.state_dir, root=DATA)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(store_items(self.store_dir)[1], 3)
        self.assertTrue(
            os.path.exists(os.path.join(self.state_dir, "reports", "run.json"))
        )

    def test_status_distinguishes_interrupted_and_complete(self):
        with self.assertRaises(_PauseInterrupt):
            run_import(self.config, GOOD, self.store_dir, self.state_dir,
                       root=DATA, pause=_raise_after(1))
        status = read_status(self.state_dir)
        self.assertEqual(status["status"], "interrupted_batches_confirmed")
        self.assertEqual(status["last_physical_line"], 4)  # 3 rows + header
        run_import(self.config, GOOD, self.store_dir, self.state_dir, root=DATA)
        self.assertEqual(read_status(self.state_dir)["status"], "complete")

    def test_status_on_unknown_state_dir_is_an_error(self):
        with self.assertRaises(ImportStateError):
            read_status(os.path.join(self.dir, "never-existed"))

    # -- truncated UTF-8 file end to end --------------------------------------
    def test_bad_utf8_rows_are_isolated_with_field_location(self):
        report = run_import(
            self.config, BAD_UTF8,
            os.path.join(self.dir, "s2"), os.path.join(self.dir, "st2"), root=DATA,
        )
        bad = [r for r in report["rows"] if r.get("reason") == "invalid_utf8"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["line_no"], 3)
        self.assertEqual(bad[0]["field_indexes"], [4])
        # The two well-formed rows still land.
        items, _ = store_items(os.path.join(self.dir, "s2"))
        self.assertEqual(set(items), {"61", "63"})

    def test_batched_writes_are_atomic_store_batches(self):
        report = run_import(self.config, GOOD, self.store_dir, self.state_dir, root=DATA)
        # Each importer batch maps to exactly one staging commit.
        self.assertEqual(
            [c["commit"] for c in report["batch_commitments"]], [1, 2, 3]
        )


class _PauseInterrupt(Exception):
    """Raised by a pause callback to simulate an interrupted process."""


def _raise_after(batch_index: int):
    def pause(point, info):
        if info["batch_index"] == batch_index:
            raise _PauseInterrupt()

    return pause


if __name__ == "__main__":
    unittest.main()
