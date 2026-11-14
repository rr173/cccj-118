"""Database connection, schema, numeric encoding and low-level helpers.

Numeric policy (important for exactness)
----------------------------------------
SQLite INTEGER is signed 64-bit and *silently converts to REAL on overflow*.
Weights, pair counts and cost sums can exceed 64 bits (inputs are arbitrary
precision JSON integers and pair weights are products), so every count, weight
and money value is stored as canonical decimal **TEXT** and all arithmetic is
performed with Python ``int`` before writing.  Nothing ever goes through a
float.  ``cost_key`` is ``str(int(cost))``; a NULL cost is represented by
``is_null = 1`` and ``cost_key = ''`` (NULLs compare distinct in a SQLite
primary key, so a separate flag is used so that a NULL-cost row still behaves
as one exact multiset cell).

The ``DECIMAL`` collation below orders decimal strings numerically.  It is
registered on every connection made via :func:`connect`; ``idx_gcs_low`` uses
it so that the minimum/maximum cost support of a group is found with one
index probe (no scan of the group's exposures).  Any other SQL client that
sorts ``cost_key COLLATE DECIMAL`` must also register a collation with this
name (all entry points of this package do).

Integer ``seq`` is stored as canonical decimal TEXT, so it stays exact for
values beyond SQLite's signed 64-bit INTEGER; ordering is done in Python.
"""

from __future__ import annotations

import os
import sqlite3

from .errors import DatabaseError

SCHEMA_VERSION = 1

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        next_seq TEXT NOT NULL,
        schema_version INTEGER NOT NULL
    )
    """,
    "INSERT OR IGNORE INTO meta (id, next_seq, schema_version) VALUES (1, '1', 1)",
    """
    CREATE TABLE IF NOT EXISTS base_e (
        publisher TEXT NOT NULL,
        creative  TEXT NOT NULL,
        is_null   INTEGER NOT NULL CHECK (is_null IN (0, 1)),
        cost_key  TEXT NOT NULL,
        weight    TEXT NOT NULL,
        PRIMARY KEY (publisher, creative, is_null, cost_key)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_base_e_creative
        ON base_e (creative, publisher, is_null, cost_key, weight)
    """,
    """
    CREATE TABLE IF NOT EXISTS base_t (
        creative TEXT NOT NULL,
        tag      TEXT NOT NULL,
        weight   TEXT NOT NULL,
        PRIMARY KEY (creative, tag)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS agg_groups (
        nid        INTEGER PRIMARY KEY,
        publisher  TEXT NOT NULL,
        tag        TEXT NOT NULL,
        pairs      TEXT NOT NULL,
        priced     TEXT NOT NULL,
        total      TEXT,
        low_key    TEXT,
        high_key   TEXT,
        creatives  TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_agg_groups_pub_tag
        ON agg_groups (publisher, tag)
    """,
    """
    CREATE TABLE IF NOT EXISTS group_cost_support (
        gid      INTEGER NOT NULL,
        is_null  INTEGER NOT NULL CHECK (is_null IN (0, 1)),
        cost_key TEXT NOT NULL,
        weight   TEXT NOT NULL,
        PRIMARY KEY (gid, is_null, cost_key)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gcs_low
        ON group_cost_support (gid, cost_key COLLATE DECIMAL)
        WHERE is_null = 0
    """,
    """
    CREATE TABLE IF NOT EXISTS group_creatives (
        gid      INTEGER NOT NULL,
        creative TEXT NOT NULL,
        weight   TEXT NOT NULL,
        PRIMARY KEY (gid, creative)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS receipts (
        seq            TEXT PRIMARY KEY,
        fingerprint    TEXT NOT NULL,
        net_edits_json TEXT NOT NULL,
        receipt_json   TEXT NOT NULL,
        committed_at   TEXT NOT NULL
    ) WITHOUT ROWID
    """,
]

# Operations used for the access log.  Each tuple is
# (index/table detail, representative statement, counts_rows_read)
STATEMENTS = {
    "meta_next_seq": (
        "meta row",
        "SELECT next_seq FROM meta WHERE id=1",
        True,
    ),
    "receipt_lookup": (
        "receipts PRIMARY KEY",
        "SELECT fingerprint, net_edits_json, receipt_json FROM receipts WHERE seq=?",
        True,
    ),
    "receipt_range": (
        "receipts PRIMARY KEY",
        "SELECT seq FROM receipts",
        True,
    ),
    "base_e_key": (
        "base_e PRIMARY KEY",
        "SELECT weight FROM base_e WHERE publisher=? AND creative=? AND is_null=? AND cost_key=?",
        True,
    ),
    "base_t_key": (
        "base_t PRIMARY KEY",
        "SELECT weight FROM base_t WHERE creative=? AND tag=?",
        True,
    ),
    "base_e_by_creative": (
        "idx_base_e_creative",
        "SELECT publisher, is_null, cost_key, weight FROM base_e WHERE creative=?",
        True,
    ),
    "base_t_by_creative": (
        "base_t PRIMARY KEY",
        "SELECT tag, weight FROM base_t WHERE creative=?",
        True,
    ),
    "group_lookup": (
        "ux_agg_groups_pub_tag",
        "SELECT nid, pairs, priced, total, low_key, high_key, creatives "
        "FROM agg_groups WHERE publisher=? AND tag=?",
        True,
    ),
    "group_lookup_gid": (
        "ux_agg_groups_pub_tag",
        "SELECT pairs, priced, total, low_key, high_key, creatives "
        "FROM agg_groups WHERE nid=?",
        True,
    ),
    "support_key": (
        "group_cost_support PRIMARY KEY",
        "SELECT weight FROM group_cost_support WHERE gid=? AND is_null=? AND cost_key=?",
        True,
    ),
    "gcs_low": (
        "idx_gcs_low (partial, cost_key COLLATE DECIMAL ASC)",
        "SELECT cost_key FROM group_cost_support WHERE gid=? AND is_null=0 "
        "ORDER BY cost_key COLLATE DECIMAL ASC LIMIT 1",
        True,
    ),
    "gcs_high": (
        "idx_gcs_low (partial, cost_key COLLATE DECIMAL DESC)",
        "SELECT cost_key FROM group_cost_support WHERE gid=? AND is_null=0 "
        "ORDER BY cost_key COLLATE DECIMAL DESC LIMIT 1",
        True,
    ),
    "group_creative_key": (
        "group_creatives PRIMARY KEY",
        "SELECT weight FROM group_creatives WHERE gid=? AND creative=?",
        True,
    ),
    "explain_gcs_low": (
        "EXPLAIN QUERY PLAN of idx_gcs_low probe",
        "EXPLAIN QUERY PLAN SELECT cost_key FROM group_cost_support "
        "WHERE gid=? AND is_null=0 ORDER BY cost_key COLLATE DECIMAL ASC LIMIT 1",
        False,
    ),
    "explain_base_e_creative": (
        "EXPLAIN QUERY PLAN of idx_base_e_creative probe",
        "EXPLAIN QUERY PLAN SELECT publisher, is_null, cost_key, weight "
        "FROM base_e WHERE creative=?",
        False,
    ),
    "base_e_write": ("base_e PRIMARY KEY", "INSERT/UPDATE/DELETE base_e row", False),
    "base_t_write": ("base_t PRIMARY KEY", "INSERT/UPDATE/DELETE base_t row", False),
    "group_write": (
        "agg_groups (rowid / ux_agg_groups_pub_tag)",
        "INSERT/UPDATE/DELETE agg_groups row",
        False,
    ),
    "support_write": (
        "group_cost_support PRIMARY KEY",
        "INSERT/UPDATE/DELETE group_cost_support row",
        False,
    ),
    "group_creative_write": (
        "group_creatives PRIMARY KEY",
        "INSERT/UPDATE/DELETE group_creatives row",
        False,
    ),
    "receipt_write": ("receipts PRIMARY KEY", "INSERT INTO receipts ...", False),
    "meta_write": ("meta row", "UPDATE meta SET next_seq=? WHERE id=1", False),
}


def decimal_collation(a, b):
    """Compare decimal integer strings numerically, exactly.

    Only the sign of the returned value matters, so Python's arbitrary
    precision integers make this exact for costs of any size.
    """
    try:
        ia = int(a)
        ib = int(b)
    except (TypeError, ValueError):
        return (a > b) - (a < b)
    return (ia > ib) - (ia < ib)


def connect(path: str, timeout: float = 60.0) -> sqlite3.Connection:
    """Open (and if necessary initialise) a distagg database.

    ``timeout`` becomes SQLite's busy timeout.  WAL + a write transaction
    acquired with ``BEGIN IMMEDIATE`` guarantees that at most one commit
    process applies a transaction at a time; a second writer waits for the
    first one to finish and then observes its receipt.
    """
    try:
        conn = sqlite3.connect(
            path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
    except sqlite3.Error as exc:  # pragma: no cover - OS-level failure
        raise DatabaseError(f"cannot open database {path!r}: {exc}") from exc
    conn.create_collation("DECIMAL", decimal_collation)
    conn.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
    _init_schema(conn)
    return conn


def _schema_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    return row is not None


def _init_schema(conn: sqlite3.Connection) -> None:
    """Initialise a fresh database; for an existing one only check version.

    A fresh database is created inside ``BEGIN IMMEDIATE`` (two processes
    racing to create it serialise).  Existing databases are not written to
    here, so opening a query connection never takes the write lock.
    """
    if _schema_present(conn):
        row = conn.execute("SELECT schema_version FROM meta WHERE id=1").fetchone()
        if row is None:  # pragma: no cover - defensive
            raise DatabaseError("meta table exists but the meta row is missing")
        if row[0] != SCHEMA_VERSION:
            raise DatabaseError(
                f"database schema version {row[0]} is not supported "
                f"(expected {SCHEMA_VERSION})"
            )
        return
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("BEGIN IMMEDIATE")
        # Another process may have created it while we waited for the lock.
        if _schema_present(conn):
            conn.execute("ROLLBACK")
            return
        for statement in _DDL:
            conn.execute(statement)
        row = conn.execute("SELECT schema_version FROM meta WHERE id=1").fetchone()
        if row is None:  # pragma: no cover - INSERT OR IGNORE above
            conn.execute(
                "INSERT INTO meta (id, next_seq, schema_version) VALUES (1, '1', ?)",
                (SCHEMA_VERSION,),
            )
        elif row[0] != SCHEMA_VERSION:
            raise DatabaseError(
                f"database schema version {row[0]} is not supported "
                f"(expected {SCHEMA_VERSION})"
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


# ---------------------------------------------------------------------------
# numeric encoding helpers
# ---------------------------------------------------------------------------

def encode_int(value: int) -> str:
    """Canonical decimal text for a Python int (no floats involved)."""
    return str(int(value))


def decode_int(text: str) -> int:
    return int(text)


def encode_cost(cost):
    """Return ``(is_null, cost_key)`` for a validated cost value."""
    if cost is None:
        return 1, ""
    return 0, str(cost)


def decode_cost(is_null: int, cost_key: str):
    return None if is_null else int(cost_key)


def db_file_size(path: str) -> int:
    base = os.path.abspath(path)
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(base + suffix)
        except OSError:
            pass
    return total
