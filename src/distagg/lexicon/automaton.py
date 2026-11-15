"""Read-only library handle and the disk-backed Aho–Corasick automaton.

A :class:`LexiconLibrary` opens a compiled library **read-only**
(``mode=ro`` plus ``PRAGMA query_only``) and verifies the format name,
format version and the ``complete`` marker before any matching happens —
a half-built or foreign file is rejected, never trusted.

An :class:`Automaton` runs the traversal over one library.  Every goto
transition, failure link and output row is fetched through a SQLite index
probe behind a fixed-capacity LRU cache, so the Python footprint is
bounded by the configured cache sizes plus the single remark being
scanned — never by the number of states, aliases or corpus lines.  All
accesses are counted and reported: transition probes, failure fallbacks,
output-chain visits and per-cache hit/miss/peak figures.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .builder import FORMAT_NAME, FORMAT_VERSION
from .cache import BoundedCache
from .errors import LexiconError, LexiconLibraryError

#: One raw automaton hit in *comparison-string* coordinates:
#: (comp_start, comp_end, term_id, alias_index, boundary, priority).
RawCandidate = Tuple[int, int, str, int, str, int]


class LexiconLibrary:
    """A validated, read-only view of a compiled matching library."""

    def __init__(self, path: str, *, sqlite_cache_size_kib: int = 8192):
        self.path = path
        if not os.path.exists(path):
            raise LexiconLibraryError("no such library", path=path)
        if os.path.isdir(path):
            raise LexiconLibraryError("path is a directory, not a library",
                                      path=path)
        uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True)
            db.execute("PRAGMA query_only = ON")
            db.execute(f"PRAGMA cache_size = -{int(sqlite_cache_size_kib)}")
        except sqlite3.Error as exc:
            raise LexiconLibraryError(
                f"cannot open library read-only: {exc}", path=path) from exc
        self._db = db
        try:
            self._meta = self._read_meta()
        except Exception:
            db.close()
            raise

    def _read_meta(self) -> Dict[str, str]:
        try:
            rows = self._db.execute("SELECT key, value FROM meta").fetchall()
        except sqlite3.Error as exc:
            raise LexiconLibraryError(
                f"not a distagg lexicon library ({exc})", path=self.path) from exc
        meta = dict(rows)
        if meta.get("format") != FORMAT_NAME:
            raise LexiconLibraryError(
                f"not a distagg lexicon library (format = "
                f"{meta.get('format')!r})", path=self.path)
        if meta.get("format_version") != FORMAT_VERSION:
            raise LexiconLibraryError(
                f"unsupported format version {meta.get('format_version')!r} "
                f"(expected {FORMAT_VERSION!r})", path=self.path)
        if meta.get("complete") != "1":
            raise LexiconLibraryError(
                "library is incomplete — the build did not finish; "
                "delete it and re-run compile", path=self.path)
        return meta

    # -- introspection ----------------------------------------------------

    @property
    def meta(self) -> Dict[str, str]:
        return dict(self._meta)

    def counts(self) -> Dict[str, int]:
        result = {}
        for key in ("terms", "aliases", "folded_chars", "states",
                    "transitions", "outputs", "max_depth"):
            try:
                result[key] = int(self._meta.get(key, "0"))
            except ValueError:  # pragma: no cover - meta is written by us
                result[key] = 0
        return result

    def iter_terms(self, limit: Optional[int] = None) -> Iterator[dict]:
        """Yield ``{"term_id", "priority", "source_line", "aliases"}``.

        Terms come out in declared ``term_id`` order, aliases in declared
        ``alias_index`` order — never in an unspecified database order.
        """
        sql = "SELECT term_id, priority, source_line FROM terms ORDER BY term_id"
        params: Tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        for term_id, priority, source_line in self._db.execute(sql, params):
            aliases = [
                {
                    "alias_index": alias_index,
                    "text": text,
                    "boundary": boundary,
                    "folded": folded,
                }
                for alias_index, text, boundary, folded in self._db.execute(
                    "SELECT alias_index, text, boundary, folded FROM aliases "
                    "WHERE term_id = ? ORDER BY alias_index",
                    (term_id,),
                )
            ]
            yield {
                "term_id": term_id,
                "priority": priority,
                "source_line": source_line,
                "aliases": aliases,
            }

    # -- automaton row access (used by Automaton) ---------------------------

    def query_transition(self, state: int, ch: str) -> Optional[int]:
        row = self._db.execute(
            "SELECT target FROM transitions WHERE state_id = ? AND ch = ?",
            (state, ch),
        ).fetchone()
        return row[0] if row is not None else None

    def query_state_info(self, state: int) -> Tuple[int, Optional[int]]:
        row = self._db.execute(
            "SELECT fail, out_link FROM states WHERE state_id = ?",
            (state,),
        ).fetchone()
        if row is None:
            raise LexiconLibraryError(
                f"library is corrupt: state {state} is missing",
                path=self.path)
        return row[0], row[1]

    def query_outputs(self, state: int) -> Tuple[Tuple[str, int, str, int, int], ...]:
        rows = self._db.execute(
            "SELECT term_id, alias_index, boundary, priority, length "
            "FROM outputs WHERE state_id = ? ORDER BY term_id, alias_index",
            (state,),
        ).fetchall()
        return tuple(rows)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "LexiconLibrary":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Automaton:
    """Aho–Corasick traversal over one library with bounded caches.

    Counters are real measurements, exposed via :meth:`stats`:

    * ``transition_lookups`` — every ``(state, char)`` goto probe;
    * ``fail_follows`` — every failure-link hop during a scan;
    * ``output_chain_states`` — states visited along output-link chains;
    * per-cache ``hits`` / ``misses`` (one miss = one SQLite probe) and
      ``peak_entries``.
    """

    def __init__(
        self,
        library: LexiconLibrary,
        *,
        state_cache_entries: int = 4096,
        transition_cache_entries: int = 8192,
        output_cache_entries: int = 2048,
    ):
        self._library = library
        self._transitions = BoundedCache(transition_cache_entries)
        self._states = BoundedCache(state_cache_entries)
        self._outputs = BoundedCache(output_cache_entries)
        self._states.put(0, (0, None))  # root: fail=0, no output link
        self.transition_lookups = 0
        self.fail_follows = 0
        self.output_chain_states = 0
        self.candidates_raw = 0
        self.chars_consumed = 0

    def _transition(self, state: int, ch: str) -> Optional[int]:
        self.transition_lookups += 1
        return self._transitions.get_or_load(
            (state, ch), lambda: self._library.query_transition(state, ch))

    def _state_info(self, state: int) -> Tuple[int, Optional[int]]:
        return self._states.get_or_load(
            state, lambda: self._library.query_state_info(state))

    def _state_outputs(self, state: int) -> Tuple[Tuple[str, int, str, int, int], ...]:
        return self._outputs.get_or_load(
            state, lambda: self._library.query_outputs(state))

    def scan(self, comparison: str,
             max_candidates: Optional[int] = None) -> List[RawCandidate]:
        """Run the automaton over one comparison string.

        Returns raw candidates in *comparison-string* coordinates; cut
        points, boundary rules and original-interval mapping are applied
        by the caller.  Only this string's candidates are held in memory,
        and ``max_candidates`` (when given) aborts the scan with a
        :class:`LexiconError` the moment it is exceeded — a pathological
        remark fails loudly instead of growing the list without bound.
        """
        state = 0
        candidates: List[RawCandidate] = []
        for index, ch in enumerate(comparison):
            while True:
                target = self._transition(state, ch)
                if target is not None:
                    state = target
                    break
                if state == 0:
                    break  # root has no transition: stay at root
                state = self._state_info(state)[0]
                self.fail_follows += 1
            self.chars_consumed += 1
            # Emit this state's own outputs, then walk output links.
            chain_state = state
            while chain_state is not None and chain_state != 0:
                self.output_chain_states += 1
                for term_id, alias_index, boundary, priority, length in \
                        self._state_outputs(chain_state):
                    candidates.append((
                        index + 1 - length,
                        index + 1,
                        term_id,
                        alias_index,
                        boundary,
                        priority,
                    ))
                    self.candidates_raw += 1
                if max_candidates is not None \
                        and len(candidates) > max_candidates:
                    raise LexiconError(
                        f"one remark produced more than {max_candidates} "
                        f"raw candidates; refusing to continue")
                chain_state = self._state_info(chain_state)[1]
        return candidates

    def stats(self) -> dict:
        return {
            "chars_consumed": self.chars_consumed,
            "transition_lookups": self.transition_lookups,
            "fail_follows": self.fail_follows,
            "output_chain_states": self.output_chain_states,
            "candidates_raw": self.candidates_raw,
            "caches": {
                "transitions": self._transitions.stats(),
                "state_info": self._states.stats(),
                "outputs": self._outputs.stats(),
            },
        }
