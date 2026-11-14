"""Independent, deliberately naive interpreter used only for verification.

It shares *no* implementation code with ``distagg``: no incremental update
equations, no SQL indexes, no support tables.  It is an in-process model of

* validation semantics (we reuse the product's validator solely to keep the
  notion of "valid transaction" identical; the aggregation is independent),
* base multiplicities,
* a full re-aggregation of the six metrics from scratch after each accepted
  transaction, exactly as a fresh SQL ``JOIN ... GROUP BY`` would compute.

The product test suite compares the product's incremental state against this
model on random, small transactions.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple


class ReferenceModel:
    def __init__(self) -> None:
        # E: (publisher, creative, cost) -> weight ; T: (creative, tag) -> weight
        self.E: Dict[Tuple[str, str, Optional[int]], int] = {}
        self.T: Dict[Tuple[str, str], int] = {}
        self.next_seq = 1
        self.receipts: Dict[int, dict] = {}

    # -- validation ------------------------------------------------------
    @staticmethod
    def fold(edits: List[dict]):
        from distagg.validate import validate_transaction

        return validate_transaction({"seq": 1, "edits": edits})[1]

    def would_be_negative(self, edits: List[dict]):
        net = self.fold(edits)
        for edit in net:
            if edit.relation == "E":
                cur = self.E.get(edit.key, 0)
            else:
                cur = self.T.get(edit.key, 0)
            if cur + edit.delta < 0:
                return (edit.relation, edit.key, cur, edit.delta)
        return None

    def apply(self, edits: List[dict]) -> dict:
        net = self.fold(edits)
        for edit in net:
            if edit.relation == "E":
                new = self.E.get(edit.key, 0) + edit.delta
                if new:
                    self.E[edit.key] = new
                else:
                    self.E.pop(edit.key, None)
            else:
                new = self.T.get(edit.key, 0) + edit.delta
                if new:
                    self.T[edit.key] = new
                else:
                    self.T.pop(edit.key, None)
        return {e.relation + repr(e.key): e.delta for e in net}

    # -- naive full computation -----------------------------------------
    def groups(self) -> Dict[Tuple[str, str], dict]:
        out: Dict[Tuple[str, str], dict] = {}
        for (publisher, creative, cost), ew in self.E.items():
            for (c2, tag), tw in self.T.items():
                if c2 != creative:
                    continue
                prod = ew * tw
                if prod == 0:
                    continue
                g = out.setdefault(
                    (publisher, tag),
                    {"pairs": 0, "priced": 0, "total": None, "low": None,
                     "high": None, "creatives": set(), "costs": {}},
                )
                g["pairs"] += prod
                g["creatives"].add(creative)
                if cost is not None:
                    g["priced"] += prod
                    g["total"] = (g["total"] or 0) + cost * prod
                    g["costs"][cost] = g["costs"].get(cost, 0) + prod
        for key, g in out.items():
            if g["priced"] == 0:
                g["total"] = None
                g["low"] = None
                g["high"] = None
            else:
                supports = [c for c, wgt in g["costs"].items() if wgt > 0]
                g["low"] = min(supports)
                g["high"] = max(supports)
            g["creatives"] = len(g["creatives"])
            del g["costs"]
        return {k: v for k, v in out.items() if v["pairs"] > 0}

    def summary(self) -> List[dict]:
        rows = []
        for (publisher, tag), g in sorted(self.groups().items()):
            rows.append(
                {
                    "publisher": publisher,
                    "tag": tag,
                    "pairs": g["pairs"],
                    "priced": g["priced"],
                    "total": g["total"],
                    "low": g["low"],
                    "high": g["high"],
                    "creatives": g["creatives"],
                }
            )
        return rows

    def base(self) -> dict:
        return {
            "E": [
                {
                    "publisher": k[0],
                    "creative": k[1],
                    "cost": k[2],
                    "weight": v,
                }
                for k, v in sorted(
                    self.E.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] is not None, kv[0][2] or 0)
                )
            ],
            "T": [
                {"creative": k[0], "tag": k[1], "weight": v}
                for k, v in sorted(self.T.items())
            ],
        }
