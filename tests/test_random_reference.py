"""Randomised differential test: incremental engine vs. independent naive model.

The reference model (``tests/reference.py``) performs a fresh full aggregation
after every transaction; it shares no aggregation code with the package.
"""

import random

import pytest

from distagg.api import query_base, query_summary, submit_transaction
from reference import ReferenceModel
from helpers import random_txn


def _normalise_base(base):
    e = sorted(
        (
            r["publisher"],
            r["creative"],
            r["cost"] is not None,
            r["cost"] if r["cost"] is not None else 0,
            r["weight"],
        )
        for r in base["E"]
    )
    t = sorted((r["creative"], r["tag"], r["weight"]) for r in base["T"])
    return e, t


@pytest.mark.parametrize("seed", range(6))
def test_random_transactions_match_reference(tmp_path, seed):
    rng = random.Random(seed)
    db = str(tmp_path / "rand.db")
    model = ReferenceModel()
    creatives = [f"c{i}" for i in range(4)]
    publishers = ["p0", "p1", "P0", " p0"]
    tags = ["t0", "t1", "x"]
    costs = [None, None, 0, 5, 10, -3, 7, 100, 999999999999999999999]
    accepted = 0
    rejected = 0
    for trial in range(26):
        seq = model.next_seq  # a rejected transaction must not consume a seq
        txn = random_txn(rng, seq, creatives, publishers, tags, costs, max_edits=6)
        negative = model.would_be_negative(txn["edits"])
        if negative is not None:
            from distagg.errors import NegativeBaseWeight

            with pytest.raises(NegativeBaseWeight):
                submit_transaction(db, txn)
            rejected += 1
            step = seq
            assert query_summary(db) == model.summary(), f"seed {seed} seq {step}"
            continue
        result = submit_transaction(db, txn)
        model.apply(txn["edits"])
        model.next_seq += 1
        step = seq
        assert query_summary(db) == model.summary(), f"seed {seed} step {step}"
        assert _normalise_base(query_base(db)) == _normalise_base(model.base()), (
            f"seed {seed} step {step}"
        )
        accepted += 1
        # Equivalent re-submission (shuffle + split) replays the receipt.
        edits = txn["edits"]
        if edits:
            shuffled = list(edits)
            rng.shuffle(shuffled)
            split = []
            for edit in shuffled:
                if edit["weight"] > 1 and rng.random() < 0.5:
                    split.append({**edit, "weight": 1})
                    split.append({**edit, "weight": edit["weight"] - 1})
                else:
                    split.append(edit)
            replay = submit_transaction(db, {"seq": step, "edits": split})
            assert replay.replayed, f"seed {seed} step {step}"
            assert replay.receipt == result.receipt
    assert accepted > 5 and rejected > 0


@pytest.mark.parametrize("seed", range(3))
def test_replay_after_rejections_and_gaps(tmp_path, seed):
    """Interleave rejected transactions (wrong seq, conflict, invalid) with
    accepted ones; accepted sequence must still match the reference model."""
    from distagg.errors import InvalidTransaction, SequenceGap

    rng = random.Random(1000 + seed)
    db = str(tmp_path / "rand.db")
    model = ReferenceModel()
    seq = 1
    while seq <= 12:
        txn = random_txn(rng, seq, [f"c{i}" for i in range(3)], ["p", "q"], ["t", "u"], [None, 1, 5, 50], 5)
        if model.would_be_negative(txn["edits"]) is None:
            submit_transaction(db, txn)
            model.apply(txn["edits"])
            assert query_summary(db) == model.summary()
            seq += 1
        else:
            from distagg.errors import NegativeBaseWeight

            with pytest.raises(NegativeBaseWeight):
                submit_transaction(db, txn)
            assert query_summary(db) == model.summary()
    # gap
    with pytest.raises(SequenceGap):
        submit_transaction(db, {"seq": seq + 2, "edits": []})
    # invalid
    with pytest.raises(InvalidTransaction):
        submit_transaction(db, {"seq": seq, "edits": [{"relation": "E", "row": {}, "weight": 1}]})
    assert query_summary(db) == model.summary()
