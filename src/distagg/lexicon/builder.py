"""Compile a controlled-vocabulary dictionary into a read-only SQLite library.

The library stores a self-contained Aho–Corasick automaton — states,
transitions, failure links and output links — plus the audited term/alias
tables.  Nothing is held in Python objects across the build:

* **ingest** streams the dictionary JSONL one physical line at a time and
  validates every record (duplicates are caught by the ``terms`` primary
  key, not by a Python set);
* **trie construction** streams aliases in sorted ``(folded, term_id,
  alias_index)`` order straight from a SQLite index and keeps only the
  current root-to-leaf path in memory — sorted order means each trie edge
  is created exactly once and never looked up;
* **failure/output links** are computed one depth level at a time
  (``fail(s)`` always lies at a smaller depth), with transition and
  state-info lookups served by fixed-capacity LRU caches over SQLite
  indexes.  Updates of one level are flushed before the next level
  starts, so no cached value can go stale.

Output rows live only on the state where an alias ends; every state
stores one ``out_link`` — the nearest failure ancestor that carries
outputs — so a scan finds inherited matches by walking a short chain
instead of reading copied per-state lists.

The target is never overwritten: the automaton is built in a private
temporary file with ``complete = 0`` and published with ``os.link``
(atomic, fails if the target exists) only after ``complete = 1`` is
committed.  A crashed or failed build leaves no file that can be opened
as a complete library.
"""

from __future__ import annotations

import hashlib
import os
import resource
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Dict, List, Optional, Tuple

from .cache import BoundedCache
from .dictionary import iter_dictionary_records
from .errors import LexiconDictionaryError, LexiconLibraryError

FORMAT_NAME = "distagg-lexicon-library"
FORMAT_VERSION = "1"

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE terms (
    term_id     TEXT PRIMARY KEY,
    priority    INTEGER NOT NULL,
    source_line INTEGER NOT NULL
);
CREATE TABLE aliases (
    term_id     TEXT NOT NULL,
    alias_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    boundary    TEXT NOT NULL,
    folded      TEXT NOT NULL,
    priority    INTEGER NOT NULL,
    PRIMARY KEY (term_id, alias_index)
);
CREATE INDEX ix_aliases_folded ON aliases(folded, term_id, alias_index);
CREATE TABLE states (
    state_id   INTEGER PRIMARY KEY,
    depth      INTEGER NOT NULL,
    parent     INTEGER,
    ch         TEXT,
    has_output INTEGER NOT NULL DEFAULT 0,
    fail       INTEGER NOT NULL DEFAULT 0,
    out_link   INTEGER
);
CREATE INDEX ix_states_depth ON states(depth);
CREATE TABLE transitions (
    state_id INTEGER NOT NULL,
    ch       TEXT NOT NULL,
    target   INTEGER NOT NULL,
    PRIMARY KEY (state_id, ch)
);
CREATE TABLE outputs (
    state_id    INTEGER NOT NULL,
    term_id     TEXT NOT NULL,
    alias_index INTEGER NOT NULL,
    boundary    TEXT NOT NULL,
    priority    INTEGER NOT NULL,
    length      INTEGER NOT NULL,
    PRIMARY KEY (state_id, term_id, alias_index)
);
"""


@dataclass(frozen=True)
class CompileConfig:
    """Resource knobs for compiling a dictionary.

    Attributes
    ----------
    sqlite_cache_size_kib:
        Value of SQLite's ``PRAGMA cache_size`` (positive KiB).  Bounds
        the page cache held inside the SQLite connection.
    max_record_bytes:
        Hard inclusive byte limit for one physical dictionary line.
    batch_size:
        Maximum number of rows held in one Python insert/update batch.
    transition_cache_entries:
        Capacity of the LRU cache for ``(state, char) -> target``
        lookups (positive and negative results) during failure-link
        construction.
    state_cache_entries:
        Capacity of the LRU cache for per-state info lookups during
        failure-link construction.
    work_dir:
        Directory for the private temporary workspace (default: next to
        the target library).
    """

    sqlite_cache_size_kib: int = 8192
    max_record_bytes: int = 1024 * 1024
    batch_size: int = 8192
    transition_cache_entries: int = 65536
    state_cache_entries: int = 65536
    work_dir: Optional[str] = None

    def __post_init__(self) -> None:
        for name in (
            "sqlite_cache_size_kib",
            "max_record_bytes",
            "batch_size",
            "transition_cache_entries",
            "state_cache_entries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LexiconLibraryError(
                    f"{name} must be a positive integer, got {value!r}")


@dataclass(frozen=True)
class CompileStats:
    """Real measurements from one compile run."""

    terms: int
    aliases: int
    folded_chars: int
    states: int
    transitions: int
    outputs: int
    max_depth: int
    fail_walk_steps: int
    transition_probes: int
    cache_stats: Dict[str, dict]
    seconds: Dict[str, float]
    peak_rss_kib: int
    dictionary_sha256: str
    library_path: str

    def to_dict(self) -> dict:
        return {
            "terms": self.terms,
            "aliases": self.aliases,
            "folded_chars": self.folded_chars,
            "states": self.states,
            "transitions": self.transitions,
            "outputs": self.outputs,
            "max_depth": self.max_depth,
            "fail_walk_steps": self.fail_walk_steps,
            "transition_probes": self.transition_probes,
            "caches": self.cache_stats,
            "seconds": self.seconds,
            "peak_rss_kib": self.peak_rss_kib,
            "dictionary_sha256": self.dictionary_sha256,
            "library_path": self.library_path,
        }


class _HashingReader:
    """Wrap a binary handle, hashing every byte ``readline`` returns."""

    def __init__(self, raw: BinaryIO, hasher) -> None:
        self._raw = raw
        self._hasher = hasher

    def readline(self) -> bytes:
        data = self._raw.readline()
        self._hasher.update(data)
        return data


def _same_path(left: str, right: str) -> bool:
    if os.path.realpath(left) == os.path.realpath(right):
        return True
    if os.path.exists(left) and os.path.exists(right):
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _open_build_db(path: str, cache_size_kib: int) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA cache_size = -{int(cache_size_kib)}")
    db.execute("PRAGMA temp_store = MEMORY")
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")
    db.execute("PRAGMA locking_mode = EXCLUSIVE")
    db.executescript(_SCHEMA)
    db.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("format", FORMAT_NAME))
    db.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
               ("format_version", FORMAT_VERSION))
    db.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("complete", "0"))
    return db


def compile_dictionary(
    dictionary_path: str,
    library_path: str,
    config: Optional[CompileConfig] = None,
) -> CompileStats:
    """Compile *dictionary_path* into the read-only library *library_path*.

    The target must not exist yet — a library is never overwritten.  Any
    failure (malformed dictionary, I/O error, existing target) raises a
    :class:`LexiconError` and leaves no openable library behind.
    """
    if config is None:
        config = CompileConfig()
    if _same_path(dictionary_path, library_path):
        raise LexiconLibraryError(
            "dictionary and library paths must be different files",
            path=library_path)
    if os.path.exists(library_path):
        raise LexiconLibraryError(
            "refusing to overwrite existing library", path=library_path)

    parent = os.path.dirname(os.path.abspath(library_path)) or "."
    scratch_root = config.work_dir or parent
    os.makedirs(scratch_root, exist_ok=True)
    workspace = tempfile.mkdtemp(prefix="distagg-lexicon-build-", dir=scratch_root)
    seconds: Dict[str, float] = {}
    try:
        tmp_library = os.path.join(workspace, "library.sqlite")
        db = _open_build_db(tmp_library, config.sqlite_cache_size_kib)
        try:
            started = time.monotonic()
            digest, terms, aliases, folded_chars = _ingest(
                db, dictionary_path, config)
            seconds["ingest"] = time.monotonic() - started

            started = time.monotonic()
            states, transitions, outputs, max_depth = _build_trie(db, config)
            seconds["trie"] = time.monotonic() - started

            started = time.monotonic()
            link_stats, caches = _build_links(db, config, max_depth)
            seconds["failure_links"] = time.monotonic() - started

            started = time.monotonic()
            _finalize(db, {
                "terms": terms,
                "aliases": aliases,
                "folded_chars": folded_chars,
                "states": states,
                "transitions": transitions,
                "outputs": outputs,
                "max_depth": max_depth,
                "dictionary_sha256": digest,
            })
            db.commit()
            seconds["finalize"] = time.monotonic() - started
        finally:
            db.close()
        _publish(tmp_library, library_path)
    finally:
        # Only the directory this call created is ever removed.
        shutil.rmtree(workspace, ignore_errors=True)

    return CompileStats(
        terms=terms,
        aliases=aliases,
        folded_chars=folded_chars,
        states=states,
        transitions=transitions,
        outputs=outputs,
        max_depth=max_depth,
        fail_walk_steps=link_stats["fail_walk_steps"],
        transition_probes=link_stats["transition_probes"],
        cache_stats=caches,
        seconds=seconds,
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        dictionary_sha256=digest,
        library_path=os.path.abspath(library_path),
    )


# -- phase 1: ingest ------------------------------------------------------------

def _ingest(db: sqlite3.Connection, dictionary_path: str,
            config: CompileConfig) -> Tuple[str, int, int, int]:
    hasher = hashlib.sha256()
    terms = 0
    aliases = 0
    folded_chars = 0
    alias_batch: List[tuple] = []
    try:
        handle = open(dictionary_path, "rb")
    except OSError as exc:
        raise LexiconDictionaryError(
            None, f"cannot open dictionary: {exc}", path=dictionary_path) from exc
    with handle:
        reader = _HashingReader(handle, hasher)
        for line_number, term_id, priority, alias_list in iter_dictionary_records(
            reader, path=dictionary_path, max_record_bytes=config.max_record_bytes
        ):
            try:
                db.execute(
                    "INSERT INTO terms(term_id, priority, source_line) "
                    "VALUES (?, ?, ?)",
                    (term_id, priority, line_number),
                )
            except sqlite3.IntegrityError:
                first = db.execute(
                    "SELECT source_line FROM terms WHERE term_id = ?",
                    (term_id,),
                ).fetchone()
                first_line = first[0] if first else "?"
                raise LexiconDictionaryError(
                    line_number,
                    f"duplicate term_id {term_id!r} (first defined on "
                    f"line {first_line})",
                    path=dictionary_path,
                )
            terms += 1
            for index, text, boundary, folded in alias_list:
                alias_batch.append((term_id, index, text, boundary, folded,
                                    priority))
                aliases += 1
                folded_chars += len(folded)
            if len(alias_batch) >= config.batch_size:
                db.executemany(
                    "INSERT INTO aliases(term_id, alias_index, text, boundary, "
                    "folded, priority) VALUES (?, ?, ?, ?, ?, ?)",
                    alias_batch,
                )
                alias_batch.clear()
        if alias_batch:
            db.executemany(
                "INSERT INTO aliases(term_id, alias_index, text, boundary, "
                "folded, priority) VALUES (?, ?, ?, ?, ?, ?)",
                alias_batch,
            )
    db.commit()
    return hasher.hexdigest(), terms, aliases, folded_chars


# -- phase 2: trie ---------------------------------------------------------------

def _common_prefix(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _build_trie(db: sqlite3.Connection,
                config: CompileConfig) -> Tuple[int, int, int, int]:
    """Insert every folded alias into the on-disk trie.

    Aliases arrive in sorted order, so the in-memory footprint is one
    root-to-leaf path: characters shared with the previous alias are
    already on the path, and every character past the common prefix
    provably needs a brand-new state (no lookup, no duplicate edge).
    """
    cursor = db.execute(
        "SELECT folded, term_id, alias_index, boundary, priority "
        "FROM aliases ORDER BY folded, term_id, alias_index"
    )
    states_batch: List[tuple] = [(0, 0, None, None)]  # root
    transitions_batch: List[tuple] = []
    outputs_batch: List[tuple] = []
    path_states: List[int] = [0]
    previous = ""
    next_state = 1
    states = 1
    outputs = 0
    max_depth = 0

    def flush() -> None:
        if states_batch:
            db.executemany(
                "INSERT INTO states(state_id, depth, parent, ch) "
                "VALUES (?, ?, ?, ?)",
                states_batch,
            )
            states_batch.clear()
        if transitions_batch:
            db.executemany(
                "INSERT INTO transitions(state_id, ch, target) "
                "VALUES (?, ?, ?)",
                transitions_batch,
            )
            transitions_batch.clear()
        if outputs_batch:
            db.executemany(
                "INSERT INTO outputs(state_id, term_id, alias_index, "
                "boundary, priority, length) VALUES (?, ?, ?, ?, ?, ?)",
                outputs_batch,
            )
            outputs_batch.clear()

    for folded, term_id, alias_index, boundary, priority in cursor:
        if folded != previous:
            common = _common_prefix(previous, folded)
            del path_states[common + 1:]
            state = path_states[-1]
            for depth in range(common, len(folded)):
                ch = folded[depth]
                new_state = next_state
                next_state += 1
                states_batch.append((new_state, depth + 1, state, ch))
                transitions_batch.append((state, ch, new_state))
                path_states.append(new_state)
                state = new_state
                states += 1
            previous = folded
            if len(folded) > max_depth:
                max_depth = len(folded)
        final_state = path_states[-1]
        outputs_batch.append(
            (final_state, term_id, alias_index, boundary, priority, len(folded))
        )
        outputs += 1
        if len(states_batch) + len(transitions_batch) + len(outputs_batch) \
                >= config.batch_size:
            flush()
    flush()
    db.execute(
        "UPDATE states SET has_output = 1 WHERE state_id IN "
        "(SELECT DISTINCT state_id FROM outputs)"
    )
    db.commit()
    return states, states - 1, outputs, max_depth


# -- phase 3: failure and output links -------------------------------------------

def _build_links(db: sqlite3.Connection, config: CompileConfig,
                 max_depth: int) -> Tuple[Dict[str, int], Dict[str, dict]]:
    """Compute ``fail`` and ``out_link`` for every state, one depth at a time.

    ``fail(s)`` is the target of the first transition labelled with ``s``'s
    incoming character found while walking the failure chain of ``s``'s
    parent — always a state at a strictly smaller depth, so processing
    levels in order and flushing each level before the next keeps every
    read consistent.  ``out_link(s)`` is the nearest failure ancestor that
    carries outputs (or ``NULL``), which lets a scan collect inherited
    matches without copying output lists onto states.
    """
    transition_cache = BoundedCache(config.transition_cache_entries)
    info_cache = BoundedCache(config.state_cache_entries)
    info_cache.put(0, (0, None, 0))  # root: fail=0, out_link=None, no outputs

    def query_transition(state: int, ch: str) -> Optional[int]:
        row = db.execute(
            "SELECT target FROM transitions WHERE state_id = ? AND ch = ?",
            (state, ch),
        ).fetchone()
        return row[0] if row is not None else None

    def query_info(state: int) -> Tuple[int, Optional[int], int]:
        row = db.execute(
            "SELECT fail, out_link, has_output FROM states WHERE state_id = ?",
            (state,),
        ).fetchone()
        if row is None:  # pragma: no cover - states are consistent by build
            raise LexiconLibraryError(f"missing state {state} during build")
        return row[0], row[1], row[2]

    def transition(state: int, ch: str) -> Optional[int]:
        return transition_cache.get_or_load(
            (state, ch), lambda: query_transition(state, ch))

    def state_info(state: int) -> Tuple[int, Optional[int], int]:
        return info_cache.get_or_load(state, lambda: query_info(state))

    stats = {"fail_walk_steps": 0, "transition_probes": 0}
    updates: List[tuple] = []

    def flush_updates() -> None:
        if updates:
            db.executemany(
                "UPDATE states SET fail = ?, out_link = ? WHERE state_id = ?",
                updates,
            )
            updates.clear()

    for depth in range(1, max_depth + 1):
        cursor = db.execute(
            "SELECT state_id, parent, ch, has_output FROM states "
            "WHERE depth = ?",
            (depth,),
        )
        for state_id, parent, ch, has_output in cursor:
            fallback = state_info(parent)[0]
            while True:
                stats["transition_probes"] += 1
                target = transition(fallback, ch)
                if target is not None and target != state_id:
                    fail = target
                    break
                if fallback == 0:
                    fail = 0
                    break
                fallback = state_info(fallback)[0]
                stats["fail_walk_steps"] += 1
            fail_fail, fail_out_link, fail_has_output = state_info(fail)
            out_link = fail if fail_has_output else fail_out_link
            info_cache.put(state_id, (fail, out_link, has_output))
            updates.append((fail, out_link, state_id))
            if len(updates) >= config.batch_size:
                # Safe mid-level: states at this depth are never queried
                # while this depth is being processed.
                flush_updates()
        # Level barrier: the next depth reads only smaller depths, whose
        # fail/out_link values are now durably in the database.
        flush_updates()
        db.commit()
    db.commit()
    caches = {
        "transitions": transition_cache.stats(),
        "state_info": info_cache.stats(),
    }
    return stats, caches


# -- finalize and publish ---------------------------------------------------------

def _finalize(db: sqlite3.Connection, counts: Dict[str, object]) -> None:
    import datetime

    for key, value in counts.items():
        db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)", (key, str(value))
        )
    db.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("created_utc",
         datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )
    db.execute("UPDATE meta SET value = '1' WHERE key = 'complete'")


def _publish(tmp_library: str, library_path: str) -> None:
    """Atomically move the finished library to its target, never overwriting."""
    try:
        os.link(tmp_library, library_path)
    except FileExistsError:
        raise LexiconLibraryError(
            "refusing to overwrite existing library", path=library_path)
    except OSError as exc:
        raise LexiconLibraryError(
            f"cannot publish library: {exc}", path=library_path) from exc
