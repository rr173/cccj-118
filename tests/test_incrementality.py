"""Local-update proof: a submit touches only the edited creative keys.

The test builds a database with many unrelated creatives and one modified
creative, then compares the access statistics of a transaction on the busy
database with one on a small database.  Reads must not grow with the total
number of creatives; a full JOIN/GROUP BY would read every base row and every
group.
"""

from distagg.api import submit_transaction
from distagg.db import connect
from distagg.engine import apply_transaction
from helpers import E, T


def _with_stats(conn, txn):
    outcome = apply_transaction(conn, txn)
    return outcome.receipt


def _build(creatives):
    conn = connect(":memory:")
    edits = []
    for i in range(creatives):
        edits.append(E(f"p{i % 7}", f"c{i}", i % 11))
        edits.append(T(f"c{i}", f"t{i % 5}"))
        edits.append(T(f"c{i}", "shared"))
    # one "hot" creative that every transaction will touch
    edits.append(E("hotpub", "hot", 42))
    edits.append(T("hot", "shared"))
    apply_transaction(conn, {"seq": 1, "edits": edits})
    return conn


def test_single_edit_reads_only_its_creative():
    small = _build(20)
    large = _build(4000)
    txn = {
        "seq": 2,
        "edits": [
            E("hotpub", "hot", 42, 1),
            T("hot", "shared", 1),
            E("hotpub", "hot", 100, 1),
        ],
    }
    small_stats = _with_stats(small, txn)["stats"]["access"]["reads"]
    large_stats = _with_stats(large, txn)["stats"]["access"]["reads"]
    # Identical row counts: one base_e range over creative "hot", one base_t
    # range over creative "hot", support key probes and two low/high probes.
    assert small_stats["rows"] == large_stats["rows"]
    assert large_stats["rows"] <= 30
    assert large_stats["calls"] == small_stats["calls"]
    for op in ("base_e_by_creative", "base_t_by_creative"):
        assert op in large_stats["by_statement"]
    # Base-e lookup reports only the old rows of the touched creative (one).
    assert large_stats["by_statement"]["base_e_by_creative"]["rows"] == 1
    # No full aggregation statements appear at all.
    assert "group_lookup" in large_stats["by_statement"]
    assert large_stats["by_statement"]["group_lookup"]["rows"] == 1
    # Only the edited groups changed.
    changed = _with_stats(large, {"seq": 3, "edits": [E("hotpub", "hot", 42, -1)]})
    assert len(changed["changed_groups"]) == 1
    assert changed["changed_groups"][0]["tag"] == "shared"
    assert changed["touched_creatives"] == ["hot"]


def test_submit_reads_do_not_grow_with_database_size(tmp_path):
    """Same transaction against a 30-creative and a 3000-creative database."""
    results = {}
    for size in (30, 3000):
        db = str(tmp_path / f"size-{size}.db")
        edits = []
        for i in range(size):
            edits.append(E(f"p{i % 13}", f"c{i}", (i * 7) % 101))
            edits.append(T(f"c{i}", f"tag{i % 9}"))
        submit_transaction(db, {"seq": 1, "edits": edits})
        conn = connect(db, timeout=60)
        try:
            receipt = apply_transaction(
                conn,
                {
                    "seq": 2,
                    "edits": [
                        E("p1", "c1", 7, 1),
                        T("c1", "tag1", 1),
                        E("p1", "c1", 500, 1),
                    ],
                },
            ).receipt
        finally:
            conn.close()
        results[size] = receipt["stats"]["access"]["reads"]
    assert results[30]["rows"] == results[3000]["rows"]
    assert results[30]["calls"] == results[3000]["calls"]
    # and the whole transaction is a handful of point reads
    assert results[3000]["rows"] < 25
    # the number of index probes is independent of the 3000 creatives
    assert results[3000]["by_statement"]["base_e_by_creative"]["rows"] == 1


def test_support_removal_probe_does_not_scan_group(tmp_path):
    """After many creatives support the same group, removing one cost support
    is still two low/high index probes plus point reads."""
    db = str(tmp_path / "a.db")
    edits = []
    for i in range(300):
        edits.append(E("p", f"c{i}", i))
        edits.append(T(f"c{i}", "t"))
    submit_transaction(db, {"seq": 1, "edits": edits})
    conn = connect(db, timeout=60)
    try:
        receipt = apply_transaction(
            conn, {"seq": 2, "edits": [E("p", "c300", 300), T("c300", "t")]}
        ).receipt
        # add one more creative; group already has 300 creatives
        stats = receipt["stats"]["access"]["reads"]["by_statement"]
        assert stats["gcs_low"]["rows"] == 1
        assert stats["gcs_high"]["rows"] == 1
        assert stats["group_creative_key"]["rows"] == 0  # new creative in this group
        assert stats["base_e_by_creative"]["rows"] == 0  # new creative has no rows yet
        assert receipt["stats"]["access"]["reads"]["rows"] < 15
        receipt2 = apply_transaction(
            conn, {"seq": 3, "edits": [E("p", "c0", 0, -1)]}
        ).receipt
        stats2 = receipt2["stats"]["access"]["reads"]["by_statement"]
        # removing the current minimum cost still only probes the index
        assert stats2["gcs_low"]["rows"] == 1
        assert stats2["gcs_high"]["rows"] == 1
        assert receipt2["changed_groups"][0]["after"]["low"] == 1
        assert receipt2["changed_groups"][0]["after"]["creatives"] == 300
    finally:
        conn.close()


def test_plans_show_index_searches(tmp_path):
    from distagg.api import query_plans

    db = str(tmp_path / "a.db")
    submit_transaction(db, {"seq": 1, "edits": [E("p", "c", 1), T("c", "t")]})
    plans = query_plans(db)
    assert any("idx_base_e_creative" in d for d in plans["creative_lookup"]["detail"])
    assert any("idx_gcs_low" in d for d in plans["support_low"]["detail"])
    assert any("idx_gcs_low" in d for d in plans["support_high"]["detail"])
    # group lookup uses the unique index, not a table scan
    detail = " ".join(plans["group_lookup"]["detail"])
    assert "ux_agg_groups_pub_tag" in detail or "SEARCH" in detail
    assert not any("SCAN agg_groups" in d for d in plans["group_lookup"]["detail"])
