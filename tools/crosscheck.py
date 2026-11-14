#!/usr/bin/env python3
"""Run random transactions against both distagg and the naive reference model.

This is the standalone version of ``tests/test_random_reference.py``: it prints
every transaction, every metric comparison and stops on the first divergence.

Usage:
    python3 tools/crosscheck.py --seed 3 --transactions 40
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from distagg.api import query_summary, submit_transaction  # noqa: E402
from distagg.errors import NegativeBaseWeight  # noqa: E402
from reference import ReferenceModel  # noqa: E402


def E(publisher, creative, cost, weight=1):
    return {
        "relation": "E",
        "row": {"publisher": publisher, "creative": creative, "cost": cost},
        "weight": weight,
    }


def T(creative, tag, weight=1):
    return {"relation": "T", "row": {"creative": creative, "tag": tag}, "weight": weight}


def random_edits(rng, creatives, publishers, tags, costs):
    edits = []
    for _ in range(rng.randint(0, 6)):
        if rng.random() < 0.55:
            edits.append(
                E(
                    rng.choice(publishers),
                    rng.choice(creatives),
                    rng.choice(costs),
                    rng.choice([1, 1, 1, 2, 3, -1, -1, -2]),
                )
            )
        else:
            edits.append(
                T(
                    rng.choice(creatives),
                    rng.choice(tags),
                    rng.choice([1, 1, 1, 2, -1, -1, -2]),
                )
            )
    return edits


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--transactions", type=int, default=40)
    parser.add_argument("--keep", action="store_true", help="keep the database file")
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)
    path = tempfile.mktemp(prefix="crosscheck-", suffix=".db")
    model = ReferenceModel()
    creatives = ["c1", "c2", "c3", "c4"]
    publishers = ["p", "q", " p"]
    tags = ["t1", "t2", "T1"]
    costs = [None, None, 0, 3, -3, 9, 10**20]
    try:
        for _ in range(args.transactions):
            seq = model.next_seq
            edits = random_edits(rng, creatives, publishers, tags, costs)
            negative = model.would_be_negative(edits)
            if negative is not None:
                try:
                    submit_transaction(path, {"seq": seq, "edits": edits})
                except NegativeBaseWeight:
                    print(f"seq {seq}: correctly rejected (negative base weight)")
                else:
                    raise SystemExit("BUG: product accepted a negative base weight")
                continue
            result = submit_transaction(path, {"seq": seq, "edits": edits})
            model.apply(edits)
            model.next_seq += 1
            product = query_summary(path)
            expected = model.summary()
            if product != expected:
                print("MISMATCH at seq", seq)
                print("edits:", edits)
                print("product:", product)
                print("reference:", expected)
                raise SystemExit(1)
            print(
                f"seq {seq}: {len(edits)} edits, {len(product)} groups, "
                f"{result.receipt['stats']['access']['reads']['rows']} rows read — match"
            )
        print("\nall transactions match the independent reference interpreter")
        print("database:", path if args.keep else "(removed)")
        return 0
    finally:
        if not args.keep:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
