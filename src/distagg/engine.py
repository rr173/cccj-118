"""Incremental transaction engine.

Incremental equations
---------------------
Base relations hold a non-negative integer multiplicity per full row.  A
transaction gives a signed net delta per full row (identical rows are folded
first).  Base rows are updated as ``w' = w + Δ`` and deleted when ``w' == 0``.

Let ``c`` be one creative that has an edit.  Read, through the creative
indexes, *all* base rows of ``c`` before writing anything:

* ``E_o(p,v)``: old weight of (publisher=p, creative=c, cost=v), v ∈ costs ∪ {NULL}
* ``T_o(t)``:   old weight of (creative=c, tag=t)

and form ``E_n`` / ``T_n`` in memory by adding the net deltas.  For every
affected group ``g=(p,t)`` the exact cross contribution of ``c`` is

    pairs(g,c)      = (Σ_v E(p,v)) * T(t)
    priced(g,c)     = (Σ_{v≠NULL} E(p,v)) * T(t)
    total(g,c)      = (Σ_{v≠NULL} v * E(p,v)) * T(t)
    support(g,v,c)  = E(p,v) * T(t)          for every cost v, including NULL

so the applied delta is ``new - old`` of those formulas, evaluated on the same
read of the creative.  This is why both relations changing in one transaction
counts each new cross pair exactly once: there is only one old→new comparison,
not an "update E then recompute against updated T" pass.

Group state is maintained as

    pairs'      = pairs + Σ_c Δpairs(g,c)
    priced'     = priced + Σ_c Δpriced(g,c)
    total'      = total + Σ_c Δtotal(g,c)          (NULL iff priced' == 0)
    support'(v) = support(v) + Σ_c Δsupport(g,v,c) (row deleted at 0)
    ccreative'(c) = ccreative(c) + Σ_c Δpairs(g,c) (row deleted at 0)
    creatives'  = creatives + #creative rows that appeared − #that vanished

``low`` / ``high`` are not recomputed by scanning exposures: they are read
from ``idx_gcs_low``, a partial index over the non-NULL support rows (which
never contain a zero weight).  ``creatives`` is maintained through the
persistent ``group_creatives`` support rows.  When ``pairs'`` reaches 0 the
group row and its remaining support rows are removed, so it disappears from
queries.

Read timing
-----------
Everything happens inside one ``BEGIN IMMEDIATE`` write transaction:

1. read ``meta.next_seq`` and the receipt for ``seq`` (idempotency/conflict);
2. read old base rows for the touched creatives only;
3. validate all final base weights (after folding) — no writes yet;
4. write base rows;
5. for each touched group, read its current aggregate row and the specific
   support/creative rows being changed, then write the deltas and probe the
   low/high index;
6. insert the receipt and advance ``meta.next_seq``.

A failure anywhere rolls the whole transaction back, so no base row, no
aggregate state and no receipt survives.  The receipt is committed together
with the data.  A re-submission of an already committed seq returns the
stored receipt without re-applying anything.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from . import db as dbm
from .errors import (
    DatabaseError,
    NegativeBaseWeight,
    SeqConflict,
    SequenceGap,
)
from .stats import AccessLog
from .validate import NetEdit, fingerprint, net_edits_json, validate_transaction

PauseCallback = Callable[[str, dict], None]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _group_key_sort(key):
    return key  # publisher, tag strings, BINARY ordering


def _support_key_sort(key):
    is_null, cost_key = key
    return (is_null, 0 if is_null else int(cost_key))


class _Reader:
    """Thin wrapper that logs every read statement and row count."""

    def __init__(self, conn: sqlite3.Connection, log: AccessLog):
        self.conn = conn
        self.log = log

    def all(self, statement: str, sql: str, params: tuple):
        rows = self.conn.execute(sql, params).fetchall()
        self.log.read(statement, len(rows))
        return rows

    def one(self, statement: str, sql: str, params: tuple):
        row = self.conn.execute(sql, params).fetchone()
        self.log.read(statement, 0 if row is None else 1)
        return row


class _Writer:
    def __init__(self, conn: sqlite3.Connection, log: AccessLog):
        self.conn = conn
        self.log = log

    def execute(self, statement: str, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cursor = self.conn.execute(sql, params)
        self.log.write(statement, 1)
        return cursor


@dataclass
class _GroupDelta:
    pairs: int = 0
    priced: int = 0
    total: int = 0
    support: Dict[Tuple[int, str], int] = field(default_factory=dict)
    creative: Dict[str, int] = field(default_factory=dict)

    def add_support(self, key: Tuple[int, str], delta: int) -> None:
        if delta:
            self.support[key] = self.support.get(key, 0) + delta

    def add_creative(self, creative: str, delta: int) -> None:
        if delta:
            self.creative[creative] = self.creative.get(creative, 0) + delta

    def creatives_negative(self) -> bool:
        return any(d < 0 for d in self.creative.values())

    def is_empty(self) -> bool:
        return (
            self.pairs == 0
            and self.priced == 0
            and self.total == 0
            and not self.support
            and not self.creative
        )


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

@dataclass
class ApplyOutcome:
    receipt: dict
    replayed: bool


def apply_transaction(
    conn: sqlite3.Connection,
    txn,
    *,
    pause: Optional[PauseCallback] = None,
    pause_info: Optional[dict] = None,
) -> ApplyOutcome:
    """Validate and apply one transaction on an open connection.

    The caller owns the connection; this function starts and commits a
    ``BEGIN IMMEDIATE`` transaction.  On any exception the transaction is
    rolled back.
    """
    seq, net_edits = validate_transaction(txn)
    fp = fingerprint(net_edits)
    info = dict(pause_info or {})
    info.update(seq=seq, fingerprint=fp)
    log = AccessLog()
    started = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        started = True
        outcome = _apply_locked(conn, seq, net_edits, fp, log)
        if outcome.replayed:
            conn.execute("COMMIT")
            return outcome
        if pause is not None:
            try:
                pause(
                    "before_commit",
                    dict(info, stage="all writes buffered, before COMMIT"),
                )
            except BaseException:
                # The caller may abort (e.g. Ctrl-C) while paused.  Never leave
                # a write transaction open.
                if started:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    started = False
                raise
        conn.execute("COMMIT")
        started = False
        if pause is not None:
            _wait_until_visible(conn, seq)
            pause(
                "after_commit",
                dict(info, stage="COMMIT committed, receipt not yet returned"),
            )
        return outcome
    except Exception:
        if started:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def _apply_locked(
    conn: sqlite3.Connection,
    seq: int,
    net_edits: List[NetEdit],
    fp: str,
    log: AccessLog,
) -> ApplyOutcome:
    reader = _Reader(conn, log)
    writer = _Writer(conn, log)

    seq_text = str(seq)

    # -- 1. idempotency / conflict, based on stored *original* meaning ----
    stored = reader.one(
        "receipt_lookup",
        "SELECT fingerprint, receipt_json FROM receipts WHERE seq=?",
        (seq_text,),
    )
    if stored is not None:
        stored_fp, receipt_json = stored
        if stored_fp != fp:
            raise SeqConflict(seq, stored_fp, fp)
        # Return the exact original receipt.  Nothing else is read or written.
        return ApplyOutcome(json.loads(receipt_json), True)

    next_row = reader.one("meta_next_seq", "SELECT next_seq FROM meta WHERE id=1", ())
    if next_row is None:  # pragma: no cover - schema initialisation guarantees it
        raise DatabaseError("meta row is missing")
    expected = int(next_row[0])
    if seq > expected:
        raise SequenceGap(seq, expected)
    if seq < expected:
        raise DatabaseError(
            f"seq {seq} is below next_seq {expected} but no receipt exists; "
            "the database is inconsistent"
        )

    # -- 2. classify edits by creative and read old base rows -------------
    e_by_creative: Dict[str, List[Tuple[str, int, str, int]]] = {}
    t_by_creative: Dict[str, List[Tuple[str, int]]] = {}
    for edit in net_edits:
        if edit.relation == "E":
            publisher, creative, cost = edit.key
            is_null, cost_key = dbm.encode_cost(cost)
            e_by_creative.setdefault(creative, []).append(
                (publisher, is_null, cost_key, edit.delta)
            )
        else:
            creative, tag = edit.key
            t_by_creative.setdefault(creative, []).append((tag, edit.delta))
    touched_creatives = sorted(set(e_by_creative) | set(t_by_creative))

    old_e: Dict[str, Dict[str, Dict[Tuple[int, str], int]]] = {}
    old_t: Dict[str, Dict[str, int]] = {}
    new_e: Dict[str, Dict[str, Dict[Tuple[int, str], int]]] = {}
    new_t: Dict[str, Dict[str, int]] = {}
    base_changes: List[dict] = []
    base_reads: Dict[str, dict] = {}

    for creative in touched_creatives:
        rows = reader.all(
            "base_e_by_creative",
            "SELECT publisher, is_null, cost_key, weight FROM base_e WHERE creative=?",
            (creative,),
        )
        by_p: Dict[str, Dict[Tuple[int, str], int]] = {}
        for publisher, is_null, cost_key, weight in rows:
            by_p.setdefault(publisher, {})[(is_null, cost_key)] = int(weight)
        old_e[creative] = by_p
        e_rows_read = len(rows)

        trows = reader.all(
            "base_t_by_creative",
            "SELECT tag, weight FROM base_t WHERE creative=?",
            (creative,),
        )
        tagmap = {tag: int(weight) for tag, weight in trows}
        old_t[creative] = tagmap
        base_reads[creative] = {"E_rows": e_rows_read, "T_rows": len(trows)}

        new_by_p = {p: dict(v) for p, v in by_p.items()}
        for publisher, is_null, cost_key, delta in e_by_creative.get(creative, []):
            cur = new_by_p.setdefault(publisher, {}).get((is_null, cost_key), 0)
            final = cur + delta
            if final < 0:
                raise NegativeBaseWeight(
                    "E",
                    {
                        "publisher": publisher,
                        "creative": creative,
                        "cost": dbm.decode_cost(is_null, cost_key),
                    },
                    cur,
                    delta,
                )
            if final == 0:
                new_by_p[publisher].pop((is_null, cost_key), None)
                if not new_by_p[publisher]:
                    del new_by_p[publisher]
            else:
                new_by_p[publisher][(is_null, cost_key)] = final
            base_changes.append(
                {
                    "relation": "E",
                    "row": {
                        "publisher": publisher,
                        "creative": creative,
                        "cost": dbm.decode_cost(is_null, cost_key),
                    },
                    "weight_delta": delta,
                    "old_weight": cur,
                    "new_weight": final,
                }
            )
        new_e[creative] = new_by_p

        new_tagmap = dict(tagmap)
        for tag, delta in t_by_creative.get(creative, []):
            cur = new_tagmap.get(tag, 0)
            final = cur + delta
            if final < 0:
                raise NegativeBaseWeight(
                    "T",
                    {"creative": creative, "tag": tag},
                    cur,
                    delta,
                )
            if final == 0:
                new_tagmap.pop(tag, None)
            else:
                new_tagmap[tag] = final
            base_changes.append(
                {
                    "relation": "T",
                    "row": {"creative": creative, "tag": tag},
                    "weight_delta": delta,
                    "old_weight": cur,
                    "new_weight": final,
                }
            )
        new_t[creative] = new_tagmap

    # -- 3. compute group deltas purely in memory -------------------------
    group_deltas: Dict[Tuple[str, str], _GroupDelta] = {}
    contribution_rows: List[dict] = []

    for creative in touched_creatives:
        e_changed_p = {p for p, _, _, _ in e_by_creative.get(creative, [])}
        t_changed_tags = {t for t, _ in t_by_creative.get(creative, [])}
        old_by_p = old_e[creative]
        new_by_p = new_e[creative]
        old_tags = old_t[creative]
        new_tags = new_t[creative]

        if e_changed_p:
            affected_t = set(old_tags) | set(new_tags)
        else:
            affected_t = set(t_changed_tags)
        if t_changed_tags:
            affected_p = set(old_by_p) | set(new_by_p)
        else:
            affected_p = set(e_changed_p)

        if not affected_p or not affected_t:
            continue

        for publisher in sorted(affected_p):
            o_map = old_by_p.get(publisher, {})
            n_map = new_by_p.get(publisher, {})
            o_pairs = sum(o_map.values())
            n_pairs = sum(n_map.values())
            o_priced = sum(w for (is_null, _), w in o_map.items() if not is_null)
            n_priced = sum(w for (is_null, _), w in n_map.items() if not is_null)
            o_total = sum(
                int(cost_key) * w
                for (is_null, cost_key), w in o_map.items()
                if not is_null
            )
            n_total = sum(
                int(cost_key) * w
                for (is_null, cost_key), w in n_map.items()
                if not is_null
            )
            support_keys = set(o_map) | set(n_map)
            for tag in sorted(affected_t):
                ot = old_tags.get(tag, 0)
                nt = new_tags.get(tag, 0)
                pairs_d = n_pairs * nt - o_pairs * ot
                priced_d = n_priced * nt - o_priced * ot
                total_d = n_total * nt - o_total * ot
                support_d = {}
                for key in support_keys:
                    d = n_map.get(key, 0) * nt - o_map.get(key, 0) * ot
                    if d:
                        support_d[key] = d
                if not (pairs_d or priced_d or total_d or support_d):
                    continue
                gkey = (publisher, tag)
                gd = group_deltas.get(gkey)
                if gd is None:
                    gd = group_deltas[gkey] = _GroupDelta()
                gd.pairs += pairs_d
                gd.priced += priced_d
                gd.total += total_d
                for key, d in support_d.items():
                    gd.add_support(key, d)
                gd.add_creative(creative, pairs_d)
                contribution_rows.append(
                    {
                        "publisher": publisher,
                        "tag": tag,
                        "creative": creative,
                        "pairs_delta": pairs_d,
                        "priced_delta": priced_d,
                        "total_delta": total_d,
                    }
                )

    # -- 4. write base rows ------------------------------------------------
    for change in base_changes:
        row = change["row"]
        new_weight = change["new_weight"]
        if change["relation"] == "E":
            is_null, cost_key = dbm.encode_cost(row["cost"])
            if new_weight == 0:
                writer.execute(
                    "base_e_write",
                    "DELETE FROM base_e WHERE publisher=? AND creative=? AND is_null=? AND cost_key=?",
                    (row["publisher"], row["creative"], is_null, cost_key),
                )
            else:
                writer.execute(
                    "base_e_write",
                    "INSERT INTO base_e (publisher, creative, is_null, cost_key, weight) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(publisher, creative, is_null, cost_key) "
                    "DO UPDATE SET weight=excluded.weight",
                    (
                        row["publisher"],
                        row["creative"],
                        is_null,
                        cost_key,
                        str(new_weight),
                    ),
                )
        else:
            if new_weight == 0:
                writer.execute(
                    "base_t_write",
                    "DELETE FROM base_t WHERE creative=? AND tag=?",
                    (row["creative"], row["tag"]),
                )
            else:
                writer.execute(
                    "base_t_write",
                    "INSERT INTO base_t (creative, tag, weight) VALUES (?, ?, ?) "
                    "ON CONFLICT(creative, tag) DO UPDATE SET weight=excluded.weight",
                    (row["creative"], row["tag"], str(new_weight)),
                )

    # -- 5. apply group deltas -------------------------------------------
    changed_groups: List[dict] = []
    groups_created: List[dict] = []
    groups_removed: List[dict] = []
    groups_updated: List[dict] = []

    for gkey in sorted(group_deltas, key=_group_key_sort):
        gd = group_deltas[gkey]
        if gd.is_empty():
            continue
        publisher, tag = gkey
        _apply_group_delta(
            reader,
            writer,
            publisher,
            tag,
            gd,
            changed_groups,
            groups_created,
            groups_removed,
            groups_updated,
        )

    # -- 6. receipt and sequence -----------------------------------------
    stats = _build_stats(
        touched_creatives,
        base_reads,
        contribution_rows,
        changed_groups,
        groups_created,
        groups_removed,
        groups_updated,
        log,
        base_changes,
    )
    receipt = {
        "seq": seq,
        "status": "committed",
        "fingerprint": fp,
        "committed_at": _iso_now(),
        "net_edits": [
            {"relation": e.relation, "key": list(e.key), "delta": e.delta}
            for e in net_edits
        ],
        "touched_creatives": touched_creatives,
        "base_changes": base_changes,
        "contributions": contribution_rows,
        "changed_groups": changed_groups,
        "groups_created": groups_created,
        "groups_removed": groups_removed,
        "groups_updated": groups_updated,
        "stats": stats,
    }
    receipt_json = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    writer.execute(
        "receipt_write",
        "INSERT INTO receipts (seq, fingerprint, net_edits_json, receipt_json, committed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (seq_text, fp, net_edits_json(net_edits), receipt_json, receipt["committed_at"]),
    )
    writer.execute(
        "meta_write",
        "UPDATE meta SET next_seq=? WHERE id=1",
        (str(seq + 1),),
    )
    return ApplyOutcome(json.loads(receipt_json), False)


def _apply_group_delta(
    reader: _Reader,
    writer: _Writer,
    publisher: str,
    tag: str,
    gd: _GroupDelta,
    changed_groups: List[dict],
    groups_created: List[dict],
    groups_removed: List[dict],
    groups_updated: List[dict],
) -> None:
    group_row = reader.one(
        "group_lookup",
        "SELECT nid, pairs, priced, total, low_key, high_key, creatives "
        "FROM agg_groups WHERE publisher=? AND tag=?",
        (publisher, tag),
    )

    if group_row is None:
        # Brand-new group: all old values are zero/empty.
        if gd.pairs < 0 or gd.priced < 0 or gd.creatives_negative():
            # Cannot happen with consistent state: a group that does not
            # exist cannot lose pairs.  Fail loudly instead of corrupting.
            raise DatabaseError(
                f"new group {publisher!r}/{tag!r} has a non-positive delta "
                f"(pairs {gd.pairs}, priced {gd.priced})"
            )
        if gd.priced > gd.pairs:  # pragma: no cover - invariant
            raise DatabaseError("inconsistent new-group priced delta")
        support_items = sorted(gd.support.items(), key=lambda kv: _support_key_sort(kv[0]))
        for key, d in support_items:
            if d <= 0:  # pragma: no cover - invariant
                raise DatabaseError("new-group support delta is not positive")
        nonnull = [int(cost_key) for (is_null, cost_key), _ in support_items if not is_null]
        low_key = str(min(nonnull)) if gd.priced > 0 else None
        high_key = str(max(nonnull)) if gd.priced > 0 else None
        if gd.priced > 0 and not nonnull:  # pragma: no cover - invariant
            raise DatabaseError("new group has priced pairs but no non-null support")
        creative_items = sorted(gd.creative.items())
        if any(d <= 0 for _, d in creative_items):  # pragma: no cover - invariant
            raise DatabaseError("new-group creative delta is not positive")
        total = str(gd.total) if gd.priced > 0 else None
        cur = writer.execute(
            "group_write",
            "INSERT INTO agg_groups "
            "(publisher, tag, pairs, priced, total, low_key, high_key, creatives) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                publisher,
                tag,
                str(gd.pairs),
                str(gd.priced),
                total,
                low_key,
                high_key,
                str(len(creative_items)),
            ),
        )
        gid = cur.lastrowid
        for (is_null, cost_key), d in support_items:
            writer.execute(
                "support_write",
                "INSERT INTO group_cost_support (gid, is_null, cost_key, weight) "
                "VALUES (?, ?, ?, ?)",
                (gid, is_null, cost_key, str(d)),
            )
        for creative, d in creative_items:
            writer.execute(
                "group_creative_write",
                "INSERT INTO group_creatives (gid, creative, weight) VALUES (?, ?, ?)",
                (gid, creative, str(d)),
            )
        after = {
            "pairs": gd.pairs,
            "priced": gd.priced,
            "total": gd.total if gd.priced > 0 else None,
            "low": int(low_key) if low_key is not None else None,
            "high": int(high_key) if high_key is not None else None,
            "creatives": len(creative_items),
        }
        changed_groups.append(
            {
                "publisher": publisher,
                "tag": tag,
                "pairs_delta": gd.pairs,
                "priced_delta": gd.priced,
                "total_delta": gd.total,
                "creatives_delta": len(creative_items),
                "support_changes": len(support_items),
                "before": None,
                "after": after,
                "action": "created",
            }
        )
        groups_created.append({"publisher": publisher, "tag": tag})
        return

    gid, old_pairs_s, old_priced_s, old_total_s, old_low_s, old_high_s, old_creatives_s = group_row
    old_pairs = int(old_pairs_s)
    old_priced = int(old_priced_s)
    old_total = int(old_total_s) if old_total_s is not None else None
    old_low = int(old_low_s) if old_low_s is not None else None
    old_high = int(old_high_s) if old_high_s is not None else None
    old_creatives = int(old_creatives_s)
    before = {
        "pairs": old_pairs,
        "priced": old_priced,
        "total": old_total,
        "low": old_low,
        "high": old_high,
        "creatives": old_creatives,
    }

    # support rows: point updates only
    support_remaining = 0
    support_items = sorted(gd.support.items(), key=lambda kv: _support_key_sort(kv[0]))
    for (is_null, cost_key), d in support_items:
        row = reader.one(
            "support_key",
            "SELECT weight FROM group_cost_support WHERE gid=? AND is_null=? AND cost_key=?",
            (gid, is_null, cost_key),
        )
        old_s = int(row[0]) if row is not None else 0
        new_s = old_s + d
        if new_s < 0:  # pragma: no cover - invariant
            raise DatabaseError(
                f"support weight for group {gid} cost {cost_key!r} would become negative"
            )
        if new_s == 0:
            if row is not None:
                writer.execute(
                    "support_write",
                    "DELETE FROM group_cost_support WHERE gid=? AND is_null=? AND cost_key=?",
                    (gid, is_null, cost_key),
                )
        elif row is None:
            writer.execute(
                "support_write",
                "INSERT INTO group_cost_support (gid, is_null, cost_key, weight) "
                "VALUES (?, ?, ?, ?)",
                (gid, is_null, cost_key, str(new_s)),
            )
        else:
            writer.execute(
                "support_write",
                "UPDATE group_cost_support SET weight=? "
                "WHERE gid=? AND is_null=? AND cost_key=?",
                (str(new_s), gid, is_null, cost_key),
            )
        if new_s > 0:
            support_remaining += 1

    # creative support rows: point updates only
    creatives_delta = 0
    creatives_remaining = 0
    for creative, d in sorted(gd.creative.items()):
        row = reader.one(
            "group_creative_key",
            "SELECT weight FROM group_creatives WHERE gid=? AND creative=?",
            (gid, creative),
        )
        old_c = int(row[0]) if row is not None else 0
        new_c = old_c + d
        if new_c < 0:  # pragma: no cover - invariant
            raise DatabaseError(
                f"creative contribution for group {gid} creative {creative!r} would become negative"
            )
        if new_c == 0:
            if row is not None:
                writer.execute(
                    "group_creative_write",
                    "DELETE FROM group_creatives WHERE gid=? AND creative=?",
                    (gid, creative),
                )
        elif row is None:
            writer.execute(
                "group_creative_write",
                "INSERT INTO group_creatives (gid, creative, weight) VALUES (?, ?, ?)",
                (gid, creative, str(new_c)),
            )
        else:
            writer.execute(
                "group_creative_write",
                "UPDATE group_creatives SET weight=? WHERE gid=? AND creative=?",
                (str(new_c), gid, creative),
            )
        creatives_delta += (1 if new_c > 0 else 0) - (1 if old_c > 0 else 0)
        if new_c > 0:
            creatives_remaining += 1

    new_pairs = old_pairs + gd.pairs
    new_priced = old_priced + gd.priced
    new_creatives = old_creatives + creatives_delta
    if new_pairs < 0 or new_priced < 0 or new_creatives < 0:  # pragma: no cover
        raise DatabaseError("aggregate would become negative")

    if new_pairs == 0:
        if support_remaining or creatives_remaining:  # pragma: no cover - invariant
            raise DatabaseError(
                f"group {publisher!r}/{tag!r} reaches zero pairs but still has "
                f"support rows ({support_remaining} cost, {creatives_remaining} creative)"
            )
        # The group has no remaining pair: remove it entirely.  Support and
        # creative rows were driven to zero above; delete by gid defensively.
        writer.execute(
            "group_purge_support",
            "DELETE FROM group_cost_support WHERE gid=?",
            (gid,),
        )
        writer.execute(
            "group_purge_creatives",
            "DELETE FROM group_creatives WHERE gid=?",
            (gid,),
        )
        writer.execute(
            "group_write",
            "DELETE FROM agg_groups WHERE nid=?",
            (gid,),
        )
        changed_groups.append(
            {
                "publisher": publisher,
                "tag": tag,
                "pairs_delta": gd.pairs,
                "priced_delta": gd.priced,
                "total_delta": gd.total,
                "creatives_delta": creatives_delta,
                "support_changes": len(support_items),
                "before": before,
                "after": None,
                "action": "removed",
            }
        )
        groups_removed.append({"publisher": publisher, "tag": tag})
        return

    if new_priced == 0:
        new_total = None
        new_low = None
        new_high = None
    else:
        new_total = (old_total if old_total is not None else 0) + gd.total
        low_row = reader.one(
            "gcs_low",
            "SELECT cost_key FROM group_cost_support "
            "WHERE gid=? AND is_null=0 "
            "ORDER BY cost_key COLLATE DECIMAL ASC LIMIT 1",
            (gid,),
        )
        high_row = reader.one(
            "gcs_high",
            "SELECT cost_key FROM group_cost_support "
            "WHERE gid=? AND is_null=0 "
            "ORDER BY cost_key COLLATE DECIMAL DESC LIMIT 1",
            (gid,),
        )
        if low_row is None or high_row is None:  # pragma: no cover - invariant
            raise DatabaseError(
                f"group {publisher!r}/{tag!r} has priced pairs but no non-null support"
            )
        new_low = int(low_row[0])
        new_high = int(high_row[0])

    writer.execute(
        "group_write",
        "UPDATE agg_groups SET pairs=?, priced=?, total=?, low_key=?, high_key=?, creatives=? "
        "WHERE nid=?",
        (
            str(new_pairs),
            str(new_priced),
            None if new_total is None else str(new_total),
            None if new_low is None else str(new_low),
            None if new_high is None else str(new_high),
            str(new_creatives),
            gid,
        ),
    )
    after = {
        "pairs": new_pairs,
        "priced": new_priced,
        "total": new_total,
        "low": new_low,
        "high": new_high,
        "creatives": new_creatives,
    }
    changed_groups.append(
        {
            "publisher": publisher,
            "tag": tag,
            "pairs_delta": gd.pairs,
            "priced_delta": gd.priced,
            "total_delta": gd.total,
            "creatives_delta": creatives_delta,
            "support_changes": len(support_items),
            "before": before,
            "after": after,
            "action": "updated",
        }
    )
    groups_updated.append({"publisher": publisher, "tag": tag})


def _wait_until_visible(conn: sqlite3.Connection, seq: int) -> None:
    """Block at the post-commit pause until the committed receipt is visible.

    SQLite keeps committed pages in its private page cache, so another
    connection may need a few milliseconds to see the new data.  A test that
    kills us at this point must observe exactly the state the receipt claimed;
    this loop (bounded) guarantees it without changing the transaction.
    """
    file_row = conn.execute("PRAGMA database_list").fetchone()
    path = file_row[2] if file_row is not None else ""
    if not path:  # in-memory or temporary database: nothing to wait for
        return
    probe = sqlite3.connect(path, timeout=5.0)
    try:
        probe.create_collation("DECIMAL", dbm.decimal_collation)
        probe.execute("PRAGMA query_only=1")
        for _ in range(500):
            if probe.execute(
                "SELECT 1 FROM receipts WHERE seq=?", (str(seq),)
            ).fetchone():
                return
            time.sleep(0.01)
    finally:
        probe.close()


def _build_stats(
    touched_creatives: List[str],
    base_reads: Dict[str, dict],
    contribution_rows: List[dict],
    changed_groups: List[dict],
    groups_created: List[dict],
    groups_removed: List[dict],
    groups_updated: List[dict],
    log: AccessLog,
    base_changes: List[dict],
) -> dict:
    signed_pairs = sum(g["pairs_delta"] for g in changed_groups)
    signed_priced = sum(g["priced_delta"] for g in changed_groups)
    signed_total = sum(g["total_delta"] for g in changed_groups)
    return {
        "touched_join_keys": {
            "count": len(touched_creatives),
            "creatives": touched_creatives,
            "base_rows_read_per_creative": base_reads,
            "note": "only these creative join keys were looked up in base_e/base_t",
        },
        "signed_contributions": {
            "rows": len(contribution_rows),
            "pairs_delta": signed_pairs,
            "priced_delta": signed_priced,
            "total_delta": signed_total,
            "by_group": [
                {
                    "publisher": g["publisher"],
                    "tag": g["tag"],
                    "pairs_delta": g["pairs_delta"],
                    "priced_delta": g["priced_delta"],
                    "total_delta": g["total_delta"],
                    "creatives_delta": g["creatives_delta"],
                }
                for g in changed_groups
            ],
        },
        "changed_groups": {
            "count": len(changed_groups),
            "created": len(groups_created),
            "removed": len(groups_removed),
            "updated": len(groups_updated),
        },
        "base_row_changes": len(base_changes),
        "access": log.as_dict(),
    }
