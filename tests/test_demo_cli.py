"""End-to-end CLI: demo replay, summary/base/receipt/check/plans."""

import json
import os
import subprocess
import sys

from distagg.demo import SCENARIO
from helpers import cli_env, E, T, write_txn


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True,
        text=True,
        env=cli_env(),
    )


def test_cli_full_demo_flow(tmp_path):
    db = str(tmp_path / "demo.db")
    proc = cli("demo", db)
    assert proc.returncode == 0, proc.stderr

    summary = json.loads(cli("summary", db).stdout)
    assert {
        "publisher": "pub-a",
        "tag": "sale",
        "pairs": 2,
        "priced": 2,
        "total": 20,
        "low": 10,
        "high": 10,
        "creatives": 1,
    } in summary

    base = json.loads(cli("base", db).stdout)
    assert base["E"] and base["T"]

    receipt = json.loads(cli("receipt", db, "1").stdout)
    assert receipt["seq"] == 1
    assert receipt["status"] == "committed"

    receipts = json.loads(cli("receipts", db).stdout)
    assert [r["seq"] for r in receipts] == list(range(1, len(SCENARIO) + 1))

    check = cli("check", db)
    assert check.returncode == 0
    assert json.loads(check.stdout)["ok"] is True

    plans = json.loads(cli("plans", db).stdout)
    assert "support_low" in plans

    missing = cli("receipt", db, "999")
    assert missing.returncode == 1
    assert json.loads(missing.stdout)["error"] == "not_found"


def test_cli_rejects_and_replays(tmp_path):
    db = str(tmp_path / "a.db")
    txn = write_txn(str(tmp_path / "1.json"), 1, [E("p", "c", 1), T("c", "t")])
    ok = cli("submit", db, txn)
    assert ok.returncode == 0
    body = json.loads(ok.stdout)
    assert body["replayed"] is False
    again = cli("submit", db, txn)
    assert again.returncode == 0
    assert json.loads(again.stdout)["replayed"] is True

    bad = str(tmp_path / "bad.json")
    with open(bad, "w") as handle:
        json.dump({"seq": 2, "edits": [{"relation": "E", "row": {}, "weight": 1}]}, handle)
    result = cli("submit", db, bad)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "invalid_transaction"
    # Failed attempt did not consume seq 2.
    good = write_txn(str(tmp_path / "2.json"), 2, [T("c", "t", 1)])
    assert cli("submit", db, good).returncode == 0


def test_packaged_demo_files_replay(tmp_path):
    db = str(tmp_path / "replay.db")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = sorted(
        os.path.join(root, "demo", "demo_transactions", name)
        for name in os.listdir(os.path.join(root, "demo", "demo_transactions"))
    )
    for path in files:
        proc = cli("submit", db, path)
        assert proc.returncode == 0, proc.stderr
    assert cli("check", db).returncode == 0
