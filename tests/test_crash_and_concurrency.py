"""Real-process crash and concurrency tests.

These start the actual CLI in a subprocess, wait until it reports that it has
reached a pause point, then SIGKILL it.  Nothing is simulated in-process.
"""

import json
import os
import signal
import subprocess
import sys
import time

from distagg.api import get_receipt, query_base, query_summary, submit_transaction
from helpers import E, T, cli_env, run_cli, write_txn

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _start_submit(db, txn_path, signal_file, wait_file, kill_at, extra=(), point=None):
    cmd = [
        sys.executable,
        "-m",
        "distagg",
        "submit",
        db,
        txn_path,
        "--pause-signal-file",
        signal_file,
        "--pause-wait-file",
        wait_file,
        *extra,
    ]
    if kill_at is not None:
        cmd += ["--kill-at", kill_at]
    if point is not None:
        cmd += ["--pause-points", point]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=cli_env(),
    )


def _wait_for_signal(path, proc, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as handle:
                return handle.read().strip()
        if proc.poll() is not None:
            raise AssertionError(
                f"submit process exited before pausing: {proc.returncode} "
                f"{proc.stdout.read()} {proc.stderr.read()}"
            )
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the pause signal")


def _kill(proc):
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


def test_kill_before_commit_leaves_nothing_and_retry_succeeds(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1), T("c", "t")]})
    before_base = query_base(db)
    before_summary = query_summary(db)
    txn_file = write_txn(str(tmp_path / "2.json"), 2, [E("p", "c", 1, 2), T("c", "t", -1)])
    signal_file = str(tmp_path / "signal.txt")
    wait_file = str(tmp_path / "wait.flag")
    open(wait_file, "w").close()
    proc = _start_submit(db, txn_file, signal_file, wait_file, None)
    try:
        marker = _wait_for_signal(signal_file, proc)
        assert "before_commit" in marker
        _kill(proc)
    finally:
        if proc.poll() is None:
            _kill(proc)
    # No base row, aggregate or receipt may survive a kill before COMMIT.
    from distagg.api import check_database, list_receipts

    assert query_base(db) == before_base
    assert query_summary(db) == before_summary
    assert get_receipt(db, 2) is None
    assert [r["seq"] for r in list_receipts(db)] == [1]
    assert check_database(db)["ok"]
    # The same transaction is accepted afterwards; seq was not consumed.
    result = submit_transaction(db, json.load(open(txn_file)))
    assert result.receipt["seq"] == 2
    assert check_database(db)["ok"]


def test_kill_after_commit_keeps_commit_and_replay_returns_same_receipt(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1), T("c", "t")]})
    txn_file = write_txn(str(tmp_path / "2.json"), 2, [E("p", "c", 1, 2), T("c", "t", 1)])
    signal_file = str(tmp_path / "signal.txt")
    wait_file = str(tmp_path / "wait.flag")
    open(wait_file, "w").close()
    proc = _start_submit(
        db, txn_file, signal_file, wait_file, None, point="after_commit"
    )
    try:
        marker = _wait_for_signal(signal_file, proc)
        assert "after_commit" in marker
        _kill(proc)
    finally:
        if proc.poll() is None:
            _kill(proc)
    # COMMIT happened before the process died: data and receipt are present.
    from distagg.api import check_database, list_receipts

    expected = [
        {"publisher": "p", "tag": "t", "pairs": 6, "priced": 6,
         "total": 6, "low": 1, "high": 1, "creatives": 1}
    ]
    assert query_summary(db) == expected
    receipt = get_receipt(db, 2)
    assert receipt is not None and receipt["status"] == "committed"
    assert [r["seq"] for r in list_receipts(db)] == [1, 2]
    assert check_database(db)["ok"]
    replay = submit_transaction(db, json.load(open(txn_file)))
    assert replay.replayed is True
    assert replay.receipt == receipt
    assert query_summary(db) == expected


def test_cli_kill_at_option_before_commit(tmp_path):
    """The CLI's own --kill-at performs the SIGKILL (no external kill)."""
    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1), T("c", "t")]})
    before = (query_base(db), query_summary(db))
    txn_file = write_txn(str(tmp_path / "2.json"), 2, [E("p", "c", 1, -1), T("c", "t")])
    proc = run_cli(["submit", db, txn_file, "--kill-at", "before_commit"])
    assert proc.returncode != 0  # killed by signal
    assert (query_base(db), query_summary(db)) == before
    assert get_receipt(db, 2) is None


def test_two_writers_same_seq_apply_once(tmp_path):
    """Several real processes submit the same seq at the same time."""
    db = str(tmp_path / "a.db")
    txn_file = write_txn(
        str(tmp_path / "1.json"), 1, [E("p", "c", 7, 3), T("c", "t", 2)]
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "distagg", "submit", db, txn_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cli_env(),
        )
        for _ in range(6)
    ]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        outputs.append((proc.returncode, out, err))
    assert all(code == 0 for code, _, _ in outputs), outputs
    results = [json.loads(out) for _, out, _ in outputs]
    committed = [r for r in results if not r["replayed"]]
    assert len(committed) == 1
    assert len(results) == 6
    # Exactly one application: 3 copies * 2 copies = 6 pairs, not a multiple.
    rows = query_summary(db)
    assert rows == [
        {"publisher": "p", "tag": "t", "pairs": 6, "priced": 6,
         "total": 42, "low": 7, "high": 7, "creatives": 1}
    ]
    from distagg.api import list_receipts

    assert [r["seq"] for r in list_receipts(db)] == [1]


def test_two_writers_different_seq_loser_can_retry(tmp_path):
    db = str(tmp_path / "a.db")
    t1 = write_txn(str(tmp_path / "1.json"), 1, [E("p", "c", 1), T("c", "t")])
    t2 = write_txn(str(tmp_path / "2.json"), 2, [T("c", "t", 1)])
    p1 = subprocess.Popen(
        [sys.executable, "-m", "distagg", "submit", db, t1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=cli_env(),
    )
    p2 = subprocess.Popen(
        [sys.executable, "-m", "distagg", "submit", db, t2],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=cli_env(),
    )
    o1, e1 = p1.communicate(timeout=60)
    o2, e2 = p2.communicate(timeout=60)
    codes = [p1.returncode, p2.returncode]
    assert 0 in codes
    # The one that lost the seq race gets a gap (if it ran first) and can retry.
    if p2.returncode != 0:
        assert json.loads(o2)["error"] == "sequence_gap"
        proc = run_cli(["submit", db, t2])
        assert proc.returncode == 0, proc.stderr
    if p1.returncode != 0:
        assert json.loads(o1)["error"] == "sequence_gap"
        proc = run_cli(["submit", db, t1])
        assert proc.returncode == 0, proc.stderr
    from distagg.api import check_database

    assert check_database(db)["ok"]
