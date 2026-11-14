"""Small packaged demo: a handful of transactions on a fresh database.

Run as ``python -m distagg demo /tmp/distagg-demo.db`` (or
``distagg demo /tmp/distagg-demo.db``) and every step prints the receipt's
effect and the resulting ``publisher x tag`` table.

The scenario exercises:

* a delta-only base row (two identical E rows and three identical T rows
  give six pairs; one of each then gives two),
* null costs and duplicate tags,
* two-sided changes in one transaction,
* a tag migration expressed as "remove old, add new" in one transaction,
* a cost correction expressed as "remove old cost row, add new cost row",
* cancelling edits inside one transaction,
* withdrawal down to a null-cost-only group,
* the final withdrawal that makes the group disappear.
"""

from __future__ import annotations

import json

from .api import get_receipt, submit_transaction, query_summary


def _E(publisher: str, creative: str, cost, weight: int = 1) -> dict:
    return {
        "relation": "E",
        "row": {"publisher": publisher, "creative": creative, "cost": cost},
        "weight": weight,
    }


def _T(creative: str, tag: str, weight: int = 1) -> dict:
    return {"relation": "T", "row": {"creative": creative, "tag": tag}, "weight": weight}


SCENARIO = [
    (
        1,
        "two copies of an exposure and three copies of a tag -> six pairs",
        [
            _E("pub-a", "cr-1", 10, 2),
            _T("cr-1", "sale", 3),
        ],
    ),
    (
        2,
        "withdraw one exposure copy and one tag copy -> two pairs",
        [
            _E("pub-a", "cr-1", 10, -1),
            _T("cr-1", "sale", -1),
        ],
    ),
    (
        3,
        "tag migration for cr-2 in one transaction: remove old, add new",
        [
            _E("pub-a", "cr-2", 10),
            _T("cr-2", "sale"),
            _T("cr-2", "clearance"),
            _T("cr-2", "sale", -1),
        ],
    ),
    (
        4,
        "cost correction for cr-3: remove the old cost row, add the new one",
        [
            _E("pub-b", "cr-3", 10),
            _E("pub-b", "cr-3", 10, -1),
            _E("pub-b", "cr-3", 25),
            _T("cr-3", "sale"),
            _T("cr-3", "sale"),  # duplicate tag relation, counted twice
        ],
    ),
    (
        5,
        "a null-cost exposure and a different creative at a different cost",
        [
            _E("pub-b", "cr-4", None, 4),
            _T("cr-4", "clearance", 2),
            _E("pub-b", "cr-5", 25),
            _T("cr-5", "sale"),
        ],
    ),
    (
        6,
        "edits that cancel inside one transaction are a no-op",
        [
            _E("pub-a", "cr-1", 10, 1),
            _E("pub-a", "cr-1", 10, -1),
            _T("cr-1", "sale", 1),
            _T("cr-1", "sale", -1),
        ],
    ),
    (
        7,
        "a group with two cost supports: withdrawal of the only exposure at cost 25",
        [
            _E("pub-b", "cr-3", 25, -1),
            _E("pub-b", "cr-5", 25, -1),
            _T("cr-3", "sale", -1),
            _E("pub-b", "cr-6", 25),
            _T("cr-6", "sale"),
        ],
    ),
    (
        8,
        "withdraw every priced pair -> group still has pairs, priced=0, total/low/high null",
        [
            _E("pub-b", "cr-6", 25, -1),
        ],
    ),
    (
        9,
        "withdraw the last null-cost pairs -> the group disappears",
        [
            _E("pub-b", "cr-4", None, -2),
            _T("cr-4", "clearance", -1),
        ],
    ),
]


def run_demo(db_path: str) -> None:
    import os

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
    print(f"database: {db_path}")
    for seq, note, edits in SCENARIO:
        result = submit_transaction(db_path, {"seq": seq, "edits": edits})
        receipt = result.receipt
        print(f"\nseq {seq}: {note}")
        print(
            "  touched creatives:",
            ", ".join(receipt["touched_creatives"]) or "(none)",
        )
        for g in receipt["changed_groups"]:
            print(
                "  group {} / {}: {} -> {} (pairs delta {:+d}, creatives delta {:+d})".format(
                    g["publisher"],
                    g["tag"],
                    "absent" if g["before"] is None else g["before"],
                    "absent" if g["after"] is None else g["after"],
                    g["pairs_delta"],
                    g["creatives_delta"],
                )
            )
        print("  summary now:")
        for row in query_summary(db_path):
            print("   ", json.dumps(row, ensure_ascii=False, sort_keys=True))
    print("\nreceipts:")
    for seq, _, _ in SCENARIO:
        r = get_receipt(db_path, seq)
        print(f"  seq {seq}: fingerprint {r['fingerprint'][:19]}...")


if __name__ == "__main__":  # pragma: no cover
    import sys

    run_demo(sys.argv[1] if len(sys.argv) > 1 else "/tmp/distagg-demo.db")
