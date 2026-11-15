"""The scan job: annotate a UTF-8 JSONL remark file against one library.

Input is one JSON object per physical line; ``text`` must be a string and
every other member is preserved untouched (business ids are *not*
required to be unique).  Output is one JSON object per input line with
the physical 1-based ``line``, the original ``record`` and the
``matches`` (each with ``term_id``, ``alias_index``, ``start``, ``end``
and the exact original slice).  Empty remarks, all-whitespace remarks
and remarks without hits all produce a line with ``"matches": []`` — no
line is ever dropped.

The whole result is written to a private temporary file first and
published with ``os.replace`` only after the last input line has been
read and the file has been flushed and fsynced: a bad last line or a
real write failure never overwrites a previous result and never leaves a
new file that looks complete.

Memory is bounded by one remark (limited by ``max_remark_bytes``), its
comparison string and character map, and this remark's candidates
(limited by ``max_candidates_per_record`` — exceeding it fails the run
loudly instead of truncating).  The corpus size is never part of the
memory bound.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import BinaryIO, Iterator, List, Optional, Tuple

from .automaton import Automaton, LexiconLibrary
from .errors import LexiconError, LexiconInputError
from .folding import fold_with_mapping
from .resolver import select_all, select_resolved, to_match_json, validate_candidates

MODES = ("all", "resolved")


@dataclass(frozen=True)
class ScanConfig:
    """Resource knobs for one scan run.

    Attributes
    ----------
    sqlite_cache_size_kib:
        SQLite page cache for the read-only library connection (KiB).
    state_cache_entries / transition_cache_entries / output_cache_entries:
        Capacities of the automaton's LRU caches.  These bound the
        Python footprint of the automaton independently of the
        dictionary size.
    max_remark_bytes:
        Hard inclusive byte limit for one physical input line.
    max_candidates_per_record:
        Hard limit on raw candidates emitted for one remark.  Exceeding
        it fails the whole run — hits are never silently truncated.
    batch_lines:
        Number of output lines buffered before one ``write`` call.
    work_dir:
        Directory for the private temporary workspace (default: next to
        the output file).
    """

    sqlite_cache_size_kib: int = 8192
    state_cache_entries: int = 4096
    transition_cache_entries: int = 8192
    output_cache_entries: int = 2048
    max_remark_bytes: int = 4 * 1024 * 1024
    max_candidates_per_record: int = 1_000_000
    batch_lines: int = 256
    work_dir: Optional[str] = None

    def __post_init__(self) -> None:
        for name in (
            "sqlite_cache_size_kib",
            "state_cache_entries",
            "transition_cache_entries",
            "output_cache_entries",
            "max_remark_bytes",
            "max_candidates_per_record",
            "batch_lines",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LexiconError(
                    f"{name} must be a positive integer, got {value!r}")


@dataclass(frozen=True)
class ScanStats:
    """Real measurements from one scan run."""

    mode: str
    lines_read: int
    lines_written: int
    matches: int
    automaton: dict
    seconds: float
    peak_rss_kib: int
    output_path: str

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "lines_read": self.lines_read,
            "lines_written": self.lines_written,
            "matches": self.matches,
            "automaton": self.automaton,
            "seconds": self.seconds,
            "peak_rss_kib": self.peak_rss_kib,
            "output_path": self.output_path,
        }


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str):
        super().__init__(f"duplicate JSON member {key!r}")
        self.key = key


def _reject_duplicates(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKeyError(key)
        seen.add(key)
    return dict(pairs)


def _parse_remark(raw: bytes, line_number: int, path: Optional[str]) -> dict:
    if not raw:
        raise LexiconInputError(
            line_number, "empty line is not a valid JSON object", path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LexiconInputError(
            line_number, f"line is not valid UTF-8: {exc}", path) from exc
    if not text.strip():
        raise LexiconInputError(
            line_number, "blank line is not a valid JSON object", path)
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError as exc:
        raise LexiconInputError(line_number, str(exc), path) from exc
    except json.JSONDecodeError as exc:
        raise LexiconInputError(
            line_number, f"invalid JSON: {exc.msg} at column {exc.colno}",
            path) from exc
    if not isinstance(obj, dict):
        raise LexiconInputError(
            line_number,
            f"top-level JSON value must be an object, got {type(obj).__name__}",
            path)
    if "text" not in obj:
        raise LexiconInputError(
            line_number, "missing required member 'text'", path)
    if not isinstance(obj["text"], str):
        raise LexiconInputError(
            line_number,
            f"'text' must be a string, got {type(obj['text']).__name__}", path)
    return obj


def _iter_remarks(handle: BinaryIO, *, path: Optional[str],
                  max_remark_bytes: int) -> Iterator[Tuple[int, dict]]:
    line_number = 0
    while True:
        raw = handle.readline()
        if raw == b"":
            return
        line_number += 1
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if len(raw) > max_remark_bytes:
            raise LexiconInputError(
                line_number,
                f"line is {len(raw)} bytes, exceeding the single-remark "
                f"limit of {max_remark_bytes} bytes", path)
        yield line_number, _parse_remark(raw, line_number, path)


def scan_text(automaton: Automaton, text: str, mode: str,
              *, max_candidates: Optional[int] = None) -> List[dict]:
    """Annotate one remark text; returns the match list for that text."""
    if mode not in MODES:
        raise LexiconError(
            f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    comparison, mapping = fold_with_mapping(text)
    candidates = automaton.scan(comparison, max_candidates=max_candidates)
    validated = validate_candidates(candidates, mapping, len(comparison), text)
    all_matches = select_all(validated)
    if mode == "all":
        chosen = all_matches
    else:
        chosen = select_resolved(all_matches)
    return to_match_json(chosen, text)


def _same_path(left: str, right: str) -> bool:
    if os.path.realpath(left) == os.path.realpath(right):
        return True
    if os.path.exists(left) and os.path.exists(right):
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def scan_remarks(
    library_path: str,
    input_path: str,
    output_path: str,
    mode: str = "resolved",
    config: Optional[ScanConfig] = None,
) -> ScanStats:
    """Scan one remark file and publish the annotated JSONL atomically."""
    if mode not in MODES:
        raise LexiconError(
            f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    if config is None:
        config = ScanConfig()
    if _same_path(input_path, output_path):
        raise LexiconError(
            f"input and output paths must be different files: {input_path!r}")

    library = LexiconLibrary(
        library_path, sqlite_cache_size_kib=config.sqlite_cache_size_kib)
    automaton = Automaton(
        library,
        state_cache_entries=config.state_cache_entries,
        transition_cache_entries=config.transition_cache_entries,
        output_cache_entries=config.output_cache_entries,
    )

    parent = os.path.dirname(os.path.abspath(output_path)) or "."
    scratch_root = config.work_dir or parent
    os.makedirs(scratch_root, exist_ok=True)
    workspace = tempfile.mkdtemp(prefix="distagg-lexicon-scan-", dir=scratch_root)
    lines_read = 0
    matches_total = 0
    started = time.monotonic()
    try:
        tmp_output = os.path.join(workspace, "output.jsonl")
        try:
            source = open(input_path, "rb")
        except OSError as exc:
            raise LexiconError(
                f"cannot open input {input_path!r}: {exc}") from exc
        with source, open(tmp_output, "w", encoding="utf-8", newline="\n") as out:
            pending: List[str] = []
            for line_number, obj in _iter_remarks(
                source, path=input_path, max_remark_bytes=config.max_remark_bytes
            ):
                lines_read += 1
                try:
                    matches = scan_text(
                        automaton, obj["text"], mode,
                        max_candidates=config.max_candidates_per_record)
                except LexiconError as exc:
                    raise LexiconInputError(
                        line_number, exc.message, input_path) from exc
                matches_total += len(matches)
                pending.append(json.dumps(
                    {"line": line_number, "record": obj, "matches": matches},
                    ensure_ascii=False, sort_keys=True))
                if len(pending) >= config.batch_lines:
                    out.write("\n".join(pending) + "\n")
                    pending.clear()
            if pending:
                out.write("\n".join(pending) + "\n")
                pending.clear()
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_output, output_path)
    finally:
        library.close()
        shutil.rmtree(workspace, ignore_errors=True)

    elapsed = time.monotonic() - started
    return ScanStats(
        mode=mode,
        lines_read=lines_read,
        lines_written=lines_read,
        matches=matches_total,
        automaton=automaton.stats(),
        seconds=elapsed,
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        output_path=os.path.abspath(output_path),
    )
