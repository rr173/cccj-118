#!/usr/bin/env python3
"""Demonstrate that a submit is local: many unrelated creatives, one edit.

Usage:
    python3 demo/local_update.py /tmp/local.db --unrelated 20000

Builds a database whose base relations contain ``--unrelated`` creatives, each
contributing to its own tags, plus one "hot" creative.  It then submits a
transaction that changes only the hot creative and prints the receipt's access
statistics: which join keys were touched, how many rows each index returned,
the signed group contributions and the resulting groups.  Finally it runs the
independent full recomputation to prove the state is still correct.

The row counts reported by the submit are independent of ``--unrelated``.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from distagg.api import check_database, query_summary, submit_transaction  # noqa: E402
from distagg.db import db_file_size  # noqa: E402


def E(publisher, creative, cost, weight=1):
    return {
        "relation": "E",
        "row": {"publisher": publisher, "creative": creative, "cost": cost},
        "weight": weight,
    }


def T(creative, tag, weight=1):
    return {
        "relation": "T",
        "row": {"creative": creative, "tag": tag},
        "weight": weight,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("--unrelated", type=int, default=20000)
    args = parser.parse_args(argv)

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(args.db + suffix)
        except OSError:
            pass

    # Build in batches so the bulk load itself is not one giant transaction.
    batch = []
    seq = 1
    for i in range(args.unrelated):
        batch.append(E(f"pub-{i % 97}", f"cr-{i}", (i * 37) % 500, 1))
        batch.append(T(f"cr-{i}", f"tag-{i % 41}", 1))
        if len(batch) >= 4000:
            submit_transaction(args.db, {"seq": seq, "edits": batch})
            seq += 1
            batch = []
    batch.extend(
        [
            E("hot-pub", "hot", 100, 2),
            E("hot-pub", "hot", None, 1),
            T("hot", "hot-tag", 3),
            T("hot", "other-tag", 1),
        ]
    )
    submit_transaction(args.db, {"seq": seq, "edits": batch})
    seq += 1
    print(f"database: {args.db}")
    print(f"unrelated creatives: {args.unrelated}")
    print(f"base rows loaded; database size: {db_file_size(args.db):,} bytes")
    print(f"groups: {len(query_summary(args.db)):,}")

    hot_txn = {
        "seq": seq,
        "edits": [
            E("hot-pub", "hot", 100, 1),   # one more exposure copy at cost 100
            T("hot", "hot-tag", -1),       # one tag copy withdrawn
        ],
    }
    print("\ntransaction (only creative 'hot' is mentioned by the edits):")
    for edit in hot_txn["edits"]:
        print("  ", edit)

    receipt = submit_transaction(args.db, hot_txn).receipt
    access = receipt["stats"]["access"]
    print("\ntouched join keys:", receipt["stats"]["touched_join_keys"]["creatives"])
    print("\nserver reads (statements, index used, rows returned):")
    for name, entry in sorted(access["reads"]["by_statement"].items()):
        print(
            f"  {name:22s} {entry['calls']:3d} call(s), "
            f"{entry['rows']:4d} row(s)  [{entry['index']}]"
        )
    print(
        f"  total reads: {access['reads']['calls']} statements, "
        f"{access['reads']['rows']} rows"
    )
    print("\nsigned contributions:")
    for group in receipt["stats"]["signed_contributions"]["by_group"]:
        print("  ", group)
    print("\nchanged groups:")
    for group in receipt["changed_groups"]:
        print(
            "  {} / {}: {} -> {}".format(
                group["publisher"], group["tag"], group["before"], group["after"]
            )
        )

    check = check_database(args.db)
    print("\nindependent full recomputation:", "OK" if check["ok"] else check["problems"])
    print("derived groups:", check["groups"])
    return 0 if check["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
