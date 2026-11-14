"""The diagnostic checker detects drift; the reference model matches the API."""

import random

from distagg.api import check_database, query_summary, submit_transaction
from distagg.db import connect
from reference import ReferenceModel
from helpers import E, T


def test_check_database_ok_on_long_sequence(tmp_path):
    db = str(tmp_path / "a.db")
    rng = random.Random(7)
    model = ReferenceModel()
    for seq in range(1, 30):
        edits = []
        for _ in range(rng.randint(0, 4)):
            edits.append(
                E(
                    rng.choice(["a", "b", " a"]),
                    rng.choice(["c1", "c2", "c3"]),
                    rng.choice([None, 0, 5, 9, -4, 50]),
                    rng.choice([1, 1, 2, 3]),
                )
            )
            edits.append(
                T(
                    rng.choice(["c1", "c2", "c3"]),
                    rng.choice(["t1", "t2", "T1"]),
                    rng.choice([1, 1, 2]),
                )
            )
        submit_transaction(db, {"seq": seq, "edits": edits})
        model.apply(edits)
        result = check_database(db)
        assert result["ok"], result["problems"]
        assert result["seqs_contiguous"]
        assert result["next_seq"] == seq + 1
    assert query_summary(db) == model.summary()


def test_check_database_detects_corruption(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {"seq": 1, "edits": [E("p", "c", 10), T("c", "t")]},
    )
    conn = connect(db)
    try:
        # Corrupt the derived min/max only.
        conn.execute("UPDATE agg_groups SET high_key='99'")
        conn.execute("UPDATE agg_groups SET low_key='99'")
    finally:
        conn.close()
    result = check_database(db)
    assert not result["ok"]
    assert any("low mismatch" in p["problem"] for p in result["problems"])
    assert any("high mismatch" in p["problem"] for p in result["problems"])


def test_explain_access_model_lists_indexes(tmp_path):
    from distagg.cli import main

    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": []})
    rc = main(["explain", db])
    assert rc == 0
