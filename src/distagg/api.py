"""Public API: submit, query and diagnostics.

Submit lifecycle
----------------
``submit_transaction`` / ``submit_file``

1. decode + validate the transaction strictly (invalid input is rejected
   before the database is touched at all);
2. open the database (creating/initialising the schema if needed);
3. start ``BEGIN IMMEDIATE`` (one writer at a time, WAL, busy timeout);
4. if ``seq`` is already committed: return the stored receipt (same meaning)
   or reject with ``seq_conflict`` (different effective meaning);
5. otherwise require ``seq`` to be the next expected sequence number,
   apply base + aggregate changes incrementally, and commit the receipt
   together with the data.

Every error raised after the transaction starts rolls back, so no base row,
aggregate row or receipt is left behind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from . import db as dbm
from .engine import apply_transaction
from .errors import InvalidTransaction

# A pause callback receives (point, info).  Supported points:
#   "before_commit"  - after all writes are buffered, before COMMIT
#   "after_commit"   - after COMMIT, before the receipt is returned
PauseCallback = Callable[[str, dict], None]


@dataclass
class SubmitResult:
    receipt: dict
    replayed: bool

    def to_dict(self) -> dict:
        return {"replayed": self.replayed, "receipt": self.receipt}


def _decode_strict(text: str, source: str):
    def reject_constant(name):
        raise InvalidTransaction(f"{source}: non-finite number {name!r} is not valid JSON")

    def reject_duplicate_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise InvalidTransaction(f"{source}: duplicate object key {key!r}")
            obj[key] = value
        return obj

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise InvalidTransaction(f"{source}: invalid JSON: {exc}") from exc


def submit_transaction(
    db_path: str,
    txn,
    *,
    timeout: float = 60.0,
    pause: Optional[PauseCallback] = None,
    pause_info: Optional[dict] = None,
) -> SubmitResult:
    """Apply one already-decoded transaction object (dict) to ``db_path``."""
    conn = dbm.connect(db_path, timeout=timeout)
    try:
        outcome = apply_transaction(
            conn, txn, pause=pause, pause_info=pause_info
        )
        return SubmitResult(outcome.receipt, outcome.replayed)
    finally:
        conn.close()


def submit_file(
    db_path: str,
    txn_path: str,
    *,
    timeout: float = 60.0,
    pause: Optional[PauseCallback] = None,
    pause_info: Optional[dict] = None,
) -> SubmitResult:
    """Read one transaction JSON file and submit it.

    The file is fully validated before the database is opened.
    """
    try:
        with open(txn_path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise InvalidTransaction(f"cannot read transaction file {txn_path!r}: {exc}") from exc
    txn = _decode_strict(text, txn_path)
    return submit_transaction(
        db_path,
        txn,
        timeout=timeout,
        pause=pause,
        pause_info=pause_info,
    )


def _decode_value(is_null: int, text):
    return None if is_null else int(text)


def query_summary(db_path: str, *, publisher: Optional[str] = None,
                  tag: Optional[str] = None) -> list:
    """Current ``publisher x tag`` summary rows as native Python values."""
    conn = dbm.connect(db_path)
    try:
        sql = (
            "SELECT publisher, tag, pairs, priced, total, low_key, high_key, creatives "
            "FROM agg_groups"
        )
        clauses = []
        params = []
        if publisher is not None:
            clauses.append("publisher = ?")
            params.append(publisher)
        if tag is not None:
            clauses.append("tag = ?")
            params.append(tag)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY publisher, tag"
        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "publisher": publisher_,
                "tag": tag_,
                "pairs": int(pairs),
                "priced": int(priced),
                "total": int(total) if total is not None else None,
                "low": int(low_key) if low_key is not None else None,
                "high": int(high_key) if high_key is not None else None,
                "creatives": int(creatives),
            }
            for (
                publisher_,
                tag_,
                pairs,
                priced,
                total,
                low_key,
                high_key,
                creatives,
            ) in rows
        ]
    finally:
        conn.close()


def query_base(db_path: str) -> dict:
    """Base multiplicities (only non-zero rows exist)."""
    conn = dbm.connect(db_path)
    try:
        e_rows = conn.execute(
            "SELECT publisher, creative, is_null, cost_key, weight FROM base_e "
            "ORDER BY publisher, creative, is_null, cost_key"
        ).fetchall()
        t_rows = conn.execute(
            "SELECT creative, tag, weight FROM base_t ORDER BY creative, tag"
        ).fetchall()
        return {
            "E": [
                {
                    "publisher": publisher,
                    "creative": creative,
                    "cost": _decode_value(is_null, cost_key),
                    "weight": int(weight),
                }
                for publisher, creative, is_null, cost_key, weight in e_rows
            ],
            "T": [
                {"creative": creative, "tag": tag, "weight": int(weight)}
                for creative, tag, weight in t_rows
            ],
        }
    finally:
        conn.close()


def get_receipt(db_path: str, seq: int) -> Optional[dict]:
    """Return the stored receipt for ``seq`` or ``None`` if not committed."""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise InvalidTransaction("seq must be an integer >= 1")
    conn = dbm.connect(db_path)
    try:
        row = conn.execute(
            "SELECT receipt_json FROM receipts WHERE seq=?", (str(seq),)
        ).fetchone()
        return json.loads(row[0]) if row is not None else None
    finally:
        conn.close()


def list_receipts(db_path: str) -> list:
    conn = dbm.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, fingerprint, committed_at FROM receipts"
        ).fetchall()
        # seq is canonical decimal text; order in Python so that seq values
        # beyond 64 bits are still listed correctly.
        rows.sort(key=lambda row: int(row[0]))
        return [
            {"seq": int(seq), "fingerprint": fingerprint, "committed_at": committed_at}
            for seq, fingerprint, committed_at in rows
        ]
    finally:
        conn.close()


def next_seq(db_path: str) -> int:
    conn = dbm.connect(db_path)
    try:
        return int(conn.execute("SELECT next_seq FROM meta WHERE id=1").fetchone()[0])
    finally:
        conn.close()


def check_database(db_path: str) -> dict:
    """Diagnostic: recompute the derived table from base rows (O(n)).

    This deliberately uses a naive full recomputation, independent of the
    incremental engine, so it can be used to verify invariants after any
    sequence of transactions.
    """
    conn = dbm.connect(db_path)
    try:
        e_rows = conn.execute(
            "SELECT publisher, creative, is_null, cost_key, weight FROM base_e"
        ).fetchall()
        t_rows = conn.execute(
            "SELECT creative, tag, weight FROM base_t"
        ).fetchall()
        expected = {}
        e_by_c = {}
        for publisher, creative, is_null, cost_key, weight in e_rows:
            e_by_c.setdefault(creative, []).append(
                (publisher, is_null, cost_key, int(weight))
            )
        t_by_c = {}
        for creative, tag, weight in t_rows:
            t_by_c.setdefault(creative, []).append((tag, int(weight)))
        for creative, elist in e_by_c.items():
            tlist = t_by_c.get(creative)
            if not tlist:
                continue
            for publisher, is_null, cost_key, ew in elist:
                for tag, tw in tlist:
                    prod = ew * tw
                    if prod == 0:
                        continue
                    g = expected.setdefault(
                        (publisher, tag),
                        {"pairs": 0, "priced": 0, "total": 0, "support": {}, "creatives": {}},
                    )
                    g["pairs"] += prod
                    if not is_null:
                        g["priced"] += prod
                        g["total"] += int(cost_key) * prod
                    key = (is_null, cost_key)
                    g["support"][key] = g["support"].get(key, 0) + prod
                    g["creatives"][creative] = g["creatives"].get(creative, 0) + prod
        actual = {}
        rows = conn.execute(
            "SELECT publisher, tag, pairs, priced, total, low_key, high_key, creatives, nid "
            "FROM agg_groups"
        ).fetchall()
        problems = []
        for publisher, tag, pairs, priced, total, low_key, high_key, creatives, gid in rows:
            actual[(publisher, tag)] = {
                "pairs": int(pairs),
                "priced": int(priced),
                "total": None if total is None else int(total),
                "low": None if low_key is None else int(low_key),
                "high": None if high_key is None else int(high_key),
                "creatives": int(creatives),
            }
            exp = expected.get((publisher, tag))
            if exp is None:
                problems.append({"group": [publisher, tag], "problem": "group exists but no pairs"})
                continue
            if exp["pairs"] <= 0:
                problems.append({"group": [publisher, tag], "problem": "expected non-positive pairs"})
                continue
            support = conn.execute(
                "SELECT is_null, cost_key, weight FROM group_cost_support WHERE gid=?",
                (gid,),
            ).fetchall()
            gsupport = {(int(n), c): int(w) for n, c, w in support}
            gcrrows = conn.execute(
                "SELECT creative, weight FROM group_creatives WHERE gid=?", (gid,)
            ).fetchall()
            gcreatives = {c: int(w) for c, w in gcrrows}
            if exp["support"] != gsupport:
                problems.append({"group": [publisher, tag], "problem": "cost support mismatch",
                                 "actual": {str(k): v for k, v in gsupport.items()},
                                 "expected": {str(k): v for k, v in exp["support"].items()}})
            if exp["creatives"] != gcreatives:
                problems.append({"group": [publisher, tag], "problem": "creative support mismatch",
                                 "actual": gcreatives, "expected": exp["creatives"]})
            priced = exp["priced"]
            computed_total = exp["total"] if priced else None
            low = min(int(c) for (n, c) in exp["support"] if not n) if priced else None
            high = max(int(c) for (n, c) in exp["support"] if not n) if priced else None
            for field, got, want in (
                ("pairs", actual[(publisher, tag)]["pairs"], exp["pairs"]),
                ("priced", actual[(publisher, tag)]["priced"], priced),
                ("total", actual[(publisher, tag)]["total"], computed_total),
                ("low", actual[(publisher, tag)]["low"], low),
                ("high", actual[(publisher, tag)]["high"], high),
                ("creatives", actual[(publisher, tag)]["creatives"], len(exp["creatives"])),
            ):
                if got != want:
                    problems.append(
                        {"group": [publisher, tag], "problem": f"{field} mismatch",
                         "actual": got, "expected": want}
                    )
        for gkey, expv in expected.items():
            if expv["pairs"] > 0 and gkey not in actual:
                problems.append({"group": list(gkey), "problem": "expected group is missing"})
        next_value = int(conn.execute("SELECT next_seq FROM meta WHERE id=1").fetchone()[0])
        receipt_seqs = [
            int(r[0]) for r in conn.execute("SELECT seq FROM receipts").fetchall()
        ]
        receipt_seqs.sort()
        return {
            "ok": not problems,
            "problems": problems,
            "groups": len(actual),
            "next_seq": next_value,
            "receipts": len(receipt_seqs),
            "seqs_contiguous": sorted(receipt_seqs) == list(range(1, next_value)),
        }
    finally:
        conn.close()


def query_plans(db_path: str, group_id: int = 1) -> dict:
    """EXPLAIN QUERY PLAN output for the hot lookup statements."""
    conn = dbm.connect(db_path)
    try:
        plans = {}
        samples = {
            "creative_lookup": (
                "SELECT publisher, is_null, cost_key, weight FROM base_e WHERE creative=?",
                ("__creative__",),
            ),
            "tag_lookup": (
                "SELECT tag, weight FROM base_t WHERE creative=?",
                ("__creative__",),
            ),
            "group_lookup": (
                "SELECT nid FROM agg_groups WHERE publisher=? AND tag=?",
                ("__publisher__", "__tag__"),
            ),
            "support_low": (
                "SELECT cost_key FROM group_cost_support WHERE gid=? AND is_null=0 "
                "ORDER BY cost_key COLLATE DECIMAL ASC LIMIT 1",
                (group_id,),
            ),
            "support_high": (
                "SELECT cost_key FROM group_cost_support WHERE gid=? AND is_null=0 "
                "ORDER BY cost_key COLLATE DECIMAL DESC LIMIT 1",
                (group_id,),
            ),
            "support_key": (
                "SELECT weight FROM group_cost_support WHERE gid=? AND is_null=? AND cost_key=?",
                (group_id, 0, "0"),
            ),
            "group_creative": (
                "SELECT weight FROM group_creatives WHERE gid=? AND creative=?",
                (group_id, "__creative__"),
            ),
            "base_e_key": (
                "SELECT weight FROM base_e WHERE publisher=? AND creative=? AND is_null=? AND cost_key=?",
                ("__publisher__", "__creative__", 0, "0"),
            ),
            "base_t_key": (
                "SELECT weight FROM base_t WHERE creative=? AND tag=?",
                ("__creative__", "__tag__"),
            ),
            "receipt": (
                "SELECT fingerprint FROM receipts WHERE seq=?",
                ("__seq__",),
            ),
        }
        for name, (sql, params) in samples.items():
            plans[name] = {
                "sql": sql,
                "detail": [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)],
            }
        return plans
    finally:
        conn.close()
