import pytest

from distagg.api import submit_transaction, get_receipt, query_base, query_summary
from distagg.errors import InvalidTransaction, NegativeBaseWeight, SequenceGap, SeqConflict
from helpers import E, T


def test_duplicate_tag_amplifies_contribution(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {"seq": 1, "edits": [E("p", "c", 5, 2), T("c", "t1"), T("c", "t2"), T("c", "t1", 2)]},
    )
    rows = {r["tag"]: r for r in query_summary(db)}
    assert rows["t1"]["pairs"] == 6  # E weight 2 * T weight 3
    assert rows["t2"]["pairs"] == 2
    assert rows["t1"]["total"] == 30
    assert rows["t1"]["creatives"] == 1


def test_exact_string_semantics(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(
        db,
        {
            "seq": 1,
            "edits": [
                E("P", "c", 1),
                E(" P", "c", 2, 2),
                E("p", "C", 3),
                T("c", "Tag"),
                T("C", "tag"),
            ],
        },
    )
    rows = {(r["publisher"], r["tag"]): r for r in query_summary(db)}
    # creative "c" has tags; creative "C" has its own tag. Exact (BINARY)
    # comparison: "P"/" P" and "Tag"/"tag" never merge.
    assert rows[("P", "Tag")]["pairs"] == 1
    assert rows[(" P", "Tag")]["pairs"] == 2
    assert rows[("p", "tag")]["pairs"] == 1
    assert rows[("p", "tag")]["total"] == 3


@pytest.mark.parametrize(
    "txn",
    [
        {"seq": 1},
        {"seq": 1, "edits": [], "extra": 1},
        {"seq": True, "edits": []},
        {"seq": 1.0, "edits": []},
        {"seq": "1", "edits": []},
        {"seq": 0, "edits": []},
        {"seq": -1, "edits": []},
        {"seq": 1, "edits": {}},
        {"seq": 1, "edits": [{"relation": "X", "row": {}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "p", "creative": "c", "cost": 1}, "weight": 0}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "p", "creative": "c", "cost": 1}, "weight": True}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "p", "creative": "c", "cost": 1}, "weight": 1.5}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "", "creative": "c", "cost": 1}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "p", "creative": "", "cost": 1}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "p", "creative": "c", "cost": True}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "p", "creative": "c", "cost": 1.0}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "p", "creative": "c"}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "E", "row": {"publisher": "p", "creative": "c", "cost": 1, "x": 2}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "T", "row": {"creative": "c", "tag": ""}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "T", "row": {"creative": "c", "tag": 3}, "weight": 1}]},
        {"seq": 1, "edits": [{"relation": "T", "row": {"creative": "c", "tag": "t"}, "weight": 1, "note": 2}]},
    ],
)
def test_invalid_transactions_are_rejected_without_trace(tmp_path, txn):
    db = str(tmp_path / "a.db")
    with pytest.raises(InvalidTransaction):
        submit_transaction(db, txn)
    assert query_base(db) == {"E": [], "T": []}
    assert query_summary(db) == []
    assert get_receipt(db, 1) is None


def test_empty_edits_is_a_legal_no_op(tmp_path):
    db = str(tmp_path / "a.db")
    result = submit_transaction(db, {"seq": 1, "edits": []})
    assert result.receipt["status"] == "committed"
    assert result.receipt["touched_creatives"] == []
    assert result.receipt["changed_groups"] == []


def test_temporary_negative_in_one_transaction_is_allowed(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1, 1)]})
    submit_transaction(db, {"seq": 2, "edits": [E("p", "c", 1, -2), E("p", "c", 1, 1)]})
    assert query_base(db)["E"] == []  # net -1 on top of 1 -> exact zero, row removed


def test_final_negative_weight_rejects_whole_transaction(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1, 1), T("c", "t")]})
    before_base = query_base(db)
    before_summary = query_summary(db)
    with pytest.raises(NegativeBaseWeight):
        submit_transaction(
            db,
            {"seq": 2, "edits": [E("p", "c", 1, 1), E("p", "c", 1, -5), T("c", "t", 2)]},
        )
    assert query_base(db) == before_base
    assert query_summary(db) == before_summary
    assert get_receipt(db, 2) is None
    # seq 2 was not consumed: the corrected transaction works.
    submit_transaction(db, {"seq": 2, "edits": [T("c", "t", 1)]})
    assert get_receipt(db, 2)["seq"] == 2


def test_cancelling_edits_inside_one_transaction(tmp_path):
    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1), T("c", "t")]})
    receipt = submit_transaction(
        db,
        {
            "seq": 2,
            "edits": [
                E("p", "c", 1, 3),
                E("p", "c", 1, -3),
                T("c", "t", -1),
                T("c", "t", 1),
            ],
        },
    ).receipt
    assert receipt["net_edits"] == []
    assert receipt["changed_groups"] == []
    assert query_summary(db)[0]["pairs"] == 1


def test_seq_gap_rejected_and_retry_after_gap(tmp_path):
    db = str(tmp_path / "a.db")
    with pytest.raises(SequenceGap):
        submit_transaction(db, {"seq": 2, "edits": []})
    assert query_summary(db) == []
    submit_transaction(db, {"seq": 1, "edits": []})
    submit_transaction(db, {"seq": 2, "edits": []})
    with pytest.raises(SequenceGap):
        submit_transaction(db, {"seq": 4, "edits": []})
    submit_transaction(db, {"seq": 3, "edits": []})
    assert get_receipt(db, 3) is not None


def test_equivalent_resend_returns_original_receipt(tmp_path):
    db = str(tmp_path / "a.db")
    first = submit_transaction(
        db,
        {
            "seq": 1,
            "edits": [
                E("p", "c", 5, 2),
                T("c", "t"),
                E("p", "c", 5, 1),
                T("c", "t", 2),
            ],
        },
    ).receipt
    # Same meaning: shuffled edit order, split rows, and a cancel/restore pair.
    second = submit_transaction(
        db,
        {
            "edits": [
                T("c", "t", 1),
                E("p", "c", 5, 1),
                E("p", "c", 5, 2),
                T("c", "t", 2),
                E("p", "c", 5, -1),
                E("p", "c", 5, 1),
            ],
            "seq": 1,
        },
    )
    assert second.replayed is True
    assert second.receipt == first
    assert query_summary(db)[0]["pairs"] == 9


def test_conflicting_resend_is_rejected_even_if_original_was_undone(tmp_path):
    db = str(tmp_path / "a.db")
    original = submit_transaction(
        db, {"seq": 1, "edits": [E("p", "c", 1, 1), T("c", "t", 1)]}
    ).receipt
    submit_transaction(db, {"seq": 2, "edits": [E("p", "c", 1, -1), T("c", "t", -1)]})
    assert query_summary(db) == []  # original changes fully withdrawn
    before = query_base(db)
    with pytest.raises(SeqConflict):
        submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1, 1)]})
    assert query_base(db) == before
    # The original meaning still replays fine.
    assert submit_transaction(
        db, {"seq": 1, "edits": [T("c", "t", 1), E("p", "c", 1, 1)]}
    ).receipt == original


def test_json_file_field_order_does_not_matter(tmp_path):
    db = str(tmp_path / "a.db")
    p1 = tmp_path / "1.json"
    p1.write_text(
        '{"seq":1,"edits":[{"relation":"E","row":{"publisher":"p","creative":"c","cost":1},"weight":1},'
        '{"relation":"T","row":{"creative":"c","tag":"t"},"weight":1}]}'
    )
    p2 = tmp_path / "2.json"
    p2.write_text(
        '{"edits":[{"weight":1,"row":{"tag":"t","creative":"c"},"relation":"T"},'
        '{"weight":1,"row":{"cost":1,"creative":"c","publisher":"p"},"relation":"E"}],"seq":1}'
    )
    from distagg.api import submit_file

    r1 = submit_file(db, str(p1)).receipt
    r2 = submit_file(db, str(p2))
    assert r2.replayed and r2.receipt == r1


def test_strict_json_parser_rejects_non_json_numbers(tmp_path):
    from distagg.api import submit_file
    from distagg.errors import InvalidTransaction

    db = str(tmp_path / "a.db")
    path = tmp_path / "nan.json"
    path.write_text('{"seq": 1, "edits": [], "x": NaN}')
    with pytest.raises(InvalidTransaction):
        submit_file(db, str(path))
