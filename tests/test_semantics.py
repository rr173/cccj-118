"""Metric semantics, support maintenance and exact arithmetic."""

import pytest

from distagg.api import get_receipt, query_base, query_summary, submit_transaction
from distagg.errors import NegativeBaseWeight, SeqConflict
from helpers import E, T


def groups(db):
    return {(r["publisher"], r["tag"]): r for r in query_summary(db)}


def test_two_sided_same_transaction_counts_new_cross_pairs_once(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db, {"seq": 1, "edits": [E("p", "c", 10, 2), T("c", "t", 3)]}
    )
    assert groups(db)[("p", "t")]["pairs"] == 6
    assert groups(db)[("p", "t")]["total"] == 60
    # Then withdraw one of each in a single transaction: (2-1)*(3-1) = 2.
    submit_transaction(db, {"seq": 2, "edits": [E("p", "c", 10, -1), T("c", "t", -1)]})
    assert groups(db)[("p", "t")]["pairs"] == 2
    assert groups(db)[("p", "t")]["total"] == 20


def test_two_sided_withdrawal_and_retag_in_one_transaction(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {"seq": 1, "edits": [E("p", "c", 10, 2), T("c", "old", 2), T("c", "other")]},
    )
    # migrate tag old->new, change cost 10->12 (remove old cost row, add new)
    # and add one more exposure copy, all in one transaction.
    submit_transaction(
        db,
        {
            "seq": 2,
            "edits": [
                E("p", "c", 10, -2),
                E("p", "c", 12, 3),
                T("c", "old", -2),
                T("c", "new", 2),
            ],
        },
    )
    g = groups(db)
    assert ("p", "old") not in g
    # E(p,c,12,3) crossed with T(c,new,2): 6 pairs, 72 money.
    assert g[("p", "new")]["pairs"] == 6
    assert g[("p", "new")]["total"] == 72
    assert g[("p", "other")]["pairs"] == 3  # 3 exposure copies of cost 12 * 1
    assert g[("p", "other")]["total"] == 36


def test_low_high_after_deleting_current_extreme_supported_by_others(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {
            "seq": 1,
            "edits": [
                E("p", "c1", 10),
                E("p", "c2", 10),
                E("p", "c1", 30),
                E("p", "c3", 50),
                T("c1", "t"),
                T("c2", "t"),
                T("c3", "t"),
            ],
        },
    )
    g = groups(db)[("p", "t")]
    assert (g["low"], g["high"]) == (10, 50)
    # Remove c3 (the only support for 50): high must fall back to 30.
    submit_transaction(db, {"seq": 2, "edits": [E("p", "c3", 50, -1)]})
    g = groups(db)[("p", "t")]
    assert (g["low"], g["high"]) == (10, 30)
    # Remove c1@10: 10 is still supported by c2, low stays 10.
    submit_transaction(db, {"seq": 3, "edits": [E("p", "c1", 10, -1)]})
    g = groups(db)[("p", "t")]
    assert (g["low"], g["high"]) == (10, 30)
    assert g["creatives"] == 2
    # Remove c2@10: now low jumps to 30.
    submit_transaction(db, {"seq": 4, "edits": [E("p", "c2", 10, -1)]})
    g = groups(db)[("p", "t")]
    assert (g["low"], g["high"]) == (30, 30)
    assert g["creatives"] == 1


def test_creatives_drops_only_when_last_support_removed(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {"seq": 1, "edits": [E("p", "c1", 10), E("p", "c1", 20), T("c1", "t")]},
    )
    assert groups(db)[("p", "t")]["creatives"] == 1
    # Remove one cost row of c1: still one creative.
    submit_transaction(db, {"seq": 2, "edits": [E("p", "c1", 20, -1)]})
    g = groups(db)[("p", "t")]
    assert g["creatives"] == 1 and g["pairs"] == 1 and g["high"] == 10
    # Remove the last row: creative count drops.
    submit_transaction(db, {"seq": 3, "edits": [E("p", "c1", 10, -1)]})
    assert groups(db) == {}


def test_null_cost_group_then_withdraw_all(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {"seq": 1, "edits": [E("p", "c", None, 2), T("c", "t", 3)]},
    )
    g = groups(db)[("p", "t")]
    assert g["pairs"] == 6
    assert g["priced"] == 0
    assert g["total"] is None and g["low"] is None and g["high"] is None
    assert g["creatives"] == 1
    # Add a priced exposure: priced appears; then remove it again -> null.
    submit_transaction(db, {"seq": 2, "edits": [E("p", "c", 7, 1)]})
    g = groups(db)[("p", "t")]
    assert (g["pairs"], g["priced"], g["total"], g["low"], g["high"]) == (9, 3, 21, 7, 7)
    submit_transaction(db, {"seq": 3, "edits": [E("p", "c", 7, -1)]})
    g = groups(db)[("p", "t")]
    assert g["priced"] == 0 and g["total"] is None and g["low"] is None and g["high"] is None
    # Remove the last null-cost pair: group disappears from queries.
    submit_transaction(db, {"seq": 4, "edits": [E("p", "c", None, -2)]})
    assert query_summary(db) == []


def test_negative_cost_is_a_reversal_and_participates(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {"seq": 1, "edits": [E("p", "c1", -50), E("p", "c2", 10), T("c1", "t"), T("c2", "t")]},
    )
    g = groups(db)[("p", "t")]
    assert (g["pairs"], g["total"], g["low"], g["high"]) == (2, -40, -50, 10)


def test_totals_exact_beyond_64_bits(tmp_path):
    big = 10**30
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {"seq": 1, "edits": [E("p", "c", big, big), T("c", "t", big)]},
    )
    g = groups(db)[("p", "t")]
    assert g["pairs"] == big * big
    assert g["total"] == big * big * big
    assert g["low"] == big and g["high"] == big
    # Weighted sum no float error: 0.1-like magnitudes are irrelevant, use odd numbers.
    submit_transaction(db, {"seq": 2, "edits": [E("p", "c", big, -big), T("c", "t", -big)]})
    assert query_summary(db) == []


def test_renaming_tag_by_removing_old_and_adding_new(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db, {"seq": 1, "edits": [E("p", "c", 1), T("c", "a"), T("c", "b")]}
    )
    assert set(groups(db)) == {("p", "a"), ("p", "b")}
    submit_transaction(db, {"seq": 2, "edits": [T("c", "a", -1), T("c", "a"), T("c", "b", -1)]})
    g = groups(db)
    assert set(g) == {("p", "a")}
    assert g[("p", "a")]["pairs"] == 1


def test_failed_transaction_leaves_no_partial_state(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1), T("c", "t")]})
    before = (query_base(db), query_summary(db))
    with pytest.raises(NegativeBaseWeight):
        submit_transaction(
            db,
            {
                "seq": 2,
                "edits": [
                    E("p", "c", 1, -1),  # this is valid...
                    T("c", "other", 1),
                    E("p", "c", 1, -5),  # ...this makes base negative
                ],
            },
        )
    assert (query_base(db), query_summary(db)) == before
    assert get_receipt(db, 2) is None


def test_conflict_when_same_seq_has_different_net_meaning(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1)]})
    with pytest.raises(SeqConflict):
        submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 2)]})
    with pytest.raises(SeqConflict):
        submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1), T("c", "t")]})
    # same meaning accepted
    assert submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1)]}).replayed
