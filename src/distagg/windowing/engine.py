"""Disk-backed engine implementing the offline window job.

Pipeline (each pass is streaming; no pass ever materializes a whole group,
tie group or frame in Python memory):

1. **ingest**       parse/validate JSONL, batch-insert every row into the
                    scratch SQLite database with its 1-based source row id;
2. **ordering**      scan the ``(part_key, ts, source_row)`` index, assign the
                    in-group position and tie-group boundaries per peer;
3. **computation**   for every group, stream rows in position order and drive
                    monotonic interval aggregators (one per monotonic piece
                    sequence, at most two per frame) plus index seeks for
                    RANGE endpoints and lag/lead references;
4. **delivery**      read computed payloads back ordered by source row and
                    atomically replace the destination file.

SQLite provides storage, indexes and ordering only.  Frame membership,
exclusions and every aggregate are computed in this module with exact Python
integers; no SQL ``OVER`` clause or window extension is used anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .aggregator import IntervalAggregator, RowCache
from .codec import encode_partition
from .declaration import WindowDeclaration, WindowSpec
from .errors import WindowError
from .jsonl_io import INT64_MAX, INT64_MIN, iter_jsonl, validate_row

_SCHEMA = """
CREATE TABLE records (
    source_row INTEGER PRIMARY KEY,
    part_key   BLOB NOT NULL,
    ts         INTEGER NOT NULL,
    pos        INTEGER,
    peer_start INTEGER,
    peer_end   INTEGER,
    value_text TEXT,
    raw_json   TEXT NOT NULL
);
CREATE TABLE deque_store (
    deque_id   INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    pos        INTEGER NOT NULL,
    value_text TEXT
);
CREATE TABLE results (
    source_row INTEGER PRIMARY KEY,
    payload    TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class WindowConfig:
    """Resource knobs for a window job.

    Attributes
    ----------
    sqlite_cache_size_kib:
        Value of SQLite's ``PRAGMA cache_size`` (positive KiB).  Bounds the
        database page cache held inside the SQLite connection.
    batch_size:
        Maximum number of rows held in one Python batch (inserts, position
        updates, result writes, row-cache loads).
    spill_threshold:
        Maximum number of entries a monotonic min/max deque keeps in memory
        before spilling into the shared ``deque_store`` table.
    max_row_bytes:
        Hard inclusive size limit for one physical input line.
    """

    sqlite_cache_size_kib: int = 8192
    batch_size: int = 1000
    spill_threshold: int = 512
    max_row_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "sqlite_cache_size_kib",
            "batch_size",
            "spill_threshold",
            "max_row_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise WindowError(
                    f"{name} must be a positive integer, got {value!r}"
                )


@dataclass(frozen=True)
class WindowJobStats:
    rows_read: int
    rows_written: int
    groups: int
    windows: int
    output_path: str


def _same_path(left: str, right: str) -> bool:
    if os.path.realpath(left) == os.path.realpath(right):
        return True
    if os.path.exists(left) and os.path.exists(right):
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _open_scratch(path: str, cache_size_kib: int) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA cache_size = -{int(cache_size_kib)}")
    db.execute("PRAGMA temp_store = MEMORY")
    db.execute("PRAGMA journal_mode = MEMORY")
    db.execute("PRAGMA synchronous = OFF")
    db.execute("PRAGMA locking_mode = EXCLUSIVE")
    db.executescript(_SCHEMA)
    return db


class _FrameState:
    """Aggregators and bookkeeping for one declared frame within a group.

    Two monotonic streams are always allocated:

    * ``left``  receives pieces entirely before the excluded row/peer;
    * ``right`` receives pieces entirely after the excluded row/peer.

    A frame without exclusion simply sends every interval to ``left``.
    Splitting the two sides is what keeps every stream's endpoints
    non-decreasing (a single stream would see the left piece move backwards
    across a same-timestamp tie group).
    """

    def __init__(self, spec: WindowSpec, db, deque_id: int, cache: RowCache,
                 spill_threshold: int):
        self.spec = spec
        self.left = IntervalAggregator(db, deque_id, cache, spill_threshold)
        self.right = IntervalAggregator(
            db, deque_id + 2, cache, spill_threshold
        )

    def reset(self) -> None:
        self.left.reset()
        self.right.reset()


def _merge_metrics(pieces: Sequence[Tuple[int, int, Optional[int],
                                          Optional[int], Optional[int]]],
                   count_all: int) -> dict:
    count_value = 0
    total = 0
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    for _all, values, piece_sum, piece_min, piece_max in pieces:
        count_value += values
        total += piece_sum
        if piece_min is not None:
            minimum = piece_min if minimum is None else min(minimum, piece_min)
            maximum = piece_max if maximum is None else max(maximum, piece_max)
    return {
        "count_all": count_all,
        "count_value": count_value,
        "sum": total if count_value else None,
        "min": minimum,
        "max": maximum,
    }


def run_window_job(
    input_path: str,
    output_path: str,
    declaration: WindowDeclaration,
    config: Optional[WindowConfig] = None,
    *,
    work_dir: Optional[str] = None,
) -> WindowJobStats:
    """Run one complete offline window job.

    The destination is replaced atomically: all computation and writing land
    in a private temporary workspace first, and ``os.replace`` only runs after
    the full output has been flushed, so a bad last input line or a write
    failure leaves any pre-existing result untouched.
    """
    if config is None:
        config = WindowConfig()
    if _same_path(input_path, output_path):
        raise WindowError(
            f"input and output paths must be different files: {input_path!r}"
        )

    parent = os.path.dirname(os.path.abspath(output_path)) or "."
    scratch_root = work_dir or parent
    os.makedirs(scratch_root, exist_ok=True)
    workspace = tempfile.mkdtemp(prefix="distagg-window-", dir=scratch_root)
    rows_read = 0
    try:
        scratch_db_path = os.path.join(workspace, "scratch.db")
        db = _open_scratch(scratch_db_path, config.sqlite_cache_size_kib)
        try:
            rows_read = _ingest(db, input_path, declaration, config)
            group_count = _assign_positions(db, config)
            db.execute(
                "CREATE INDEX ix_pos ON records(part_key, pos)"
            )
            db.execute(
                "CREATE INDEX ix_deque ON deque_store(deque_id, seq)"
            )
            if rows_read:
                _compute(db, declaration, config, group_count)
            output_tmp = os.path.join(workspace, "output.jsonl")
            _deliver(db, output_tmp, config)
            db.commit()
        finally:
            db.close()
        os.replace(output_tmp, output_path)
    finally:
        # Only the directory this call created is ever removed.
        shutil.rmtree(workspace, ignore_errors=True)

    return WindowJobStats(
        rows_read=rows_read,
        rows_written=rows_read,
        groups=group_count,
        windows=len(declaration.windows),
        output_path=os.path.abspath(output_path),
    )


# -- pass 1: ingest -----------------------------------------------------------

def _ingest(db: sqlite3.Connection, input_path: str,
            declaration: WindowDeclaration, config: WindowConfig) -> int:
    batch: List[tuple] = []
    rows_read = 0
    try:
        handle = open(input_path, "rb")
    except OSError as exc:
        raise WindowError(f"cannot open input {input_path!r}: {exc}") from exc
    with handle:
        for line_number, raw, obj in iter_jsonl(
            handle, path=input_path, max_row_bytes=config.max_row_bytes
        ):
            partition_values, ts, value = validate_row(
                obj,
                line_number=line_number,
                partition_fields=declaration.partition_by,
                order_field=declaration.order_by,
                value_field=declaration.value_field,
                path=input_path,
            )
            rows_read += 1
            batch.append((
                rows_read,
                encode_partition(partition_values),
                ts,
                None if value is None else str(value),
                raw.decode("utf-8"),
            ))
            if len(batch) >= config.batch_size:
                _flush_inserts(db, batch)
                batch.clear()
        _flush_inserts(db, batch)
    db.commit()
    return rows_read


def _flush_inserts(db: sqlite3.Connection, batch: List[tuple]) -> None:
    if not batch:
        return
    db.executemany(
        "INSERT INTO records(source_row, part_key, ts, value_text, raw_json) "
        "VALUES (?, ?, ?, ?, ?)",
        batch,
    )


# -- pass 2: deterministic ordering -------------------------------------------

def _assign_positions(db: sqlite3.Connection, config: WindowConfig) -> int:
    db.execute(
        "CREATE INDEX ix_part ON records(part_key, ts, source_row)"
    )
    cursor = db.execute(
        "SELECT source_row, part_key, ts FROM records "
        "ORDER BY part_key, ts, source_row"
    )
    pos_buffer: List[tuple] = []
    group_count = 0
    current_part = None
    current_ts: Optional[int] = None
    position = 0
    peer_start = 0

    def flush_positions() -> None:
        if pos_buffer:
            db.executemany(
                "UPDATE records SET pos = ? WHERE source_row = ?", pos_buffer
            )
            pos_buffer.clear()

    def close_peer(part_blob: bytes, ts_value: int, start: int, end: int) -> None:
        # One indexed statement per tie group, however large it is: the peer
        # is never materialized as a Python list.
        db.execute(
            "UPDATE records SET peer_start = ?, peer_end = ? "
            "WHERE part_key = ? AND ts = ?",
            (start, end, part_blob, ts_value),
        )

    while True:
        rows = cursor.fetchmany(config.batch_size)
        if not rows:
            break
        for source_row, part_blob, ts in rows:
            if current_part is None or part_blob != current_part:
                if current_part is not None:
                    close_peer(current_part, current_ts, peer_start, position)
                    flush_positions()
                current_part = part_blob
                current_ts = ts
                group_count += 1
                position = 1
                peer_start = 1
            elif ts != current_ts:
                close_peer(current_part, current_ts, peer_start, position)
                current_ts = ts
                position += 1
                peer_start = position
            else:
                position += 1
            pos_buffer.append((position, source_row))
            if len(pos_buffer) >= config.batch_size:
                flush_positions()
    if current_part is not None:
        close_peer(current_part, current_ts, peer_start, position)
        flush_positions()
    db.commit()
    return group_count


# -- pass 3: window computation -----------------------------------------------

def _compute(db: sqlite3.Connection, declaration: WindowDeclaration,
             config: WindowConfig, group_count: int) -> None:
    specs = list(declaration.windows.values())
    k = declaration.lag_lead_k
    result_batch: List[tuple] = []
    deque_id = 0
    group_cursor = db.execute(
        "SELECT DISTINCT part_key FROM records ORDER BY part_key"
    )
    while True:
        group_page = group_cursor.fetchmany(config.batch_size)
        if not group_page:
            break
        for (part_blob,) in group_page:
            min_ts, max_ts, group_size = db.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM records "
                "WHERE part_key = ?",
                (part_blob,),
            ).fetchone()
            cache = RowCache(db, part_blob, config.batch_size)
            frames: List[_FrameState] = []
            for spec in specs:
                frames.append(
                    _FrameState(spec, db, deque_id, cache, config.spill_threshold)
                )
                deque_id += 4
            dense_rank = 0
            scan = db.execute(
                "SELECT source_row, ts, pos, peer_start, peer_end, raw_json "
                "FROM records WHERE part_key = ? ORDER BY pos",
                (part_blob,),
            )
            while True:
                page = scan.fetchmany(config.batch_size)
                if not page:
                    break
                for source_row, ts, pos, peer_start, peer_end, raw_json in page:
                    if pos == peer_start:
                        dense_rank += 1
                    computed = {
                        "row_number": pos,
                        "rank": peer_start,
                        "dense_rank": dense_rank,
                        "lag": _reference(db, part_blob, pos - k),
                        "lead": _reference(db, part_blob, pos + k),
                        "windows": {},
                    }
                    lowest_left = pos
                    for frame in frames:
                        metrics, frame_left = _frame_metrics(
                            db, frame, part_blob, pos, ts, peer_start, peer_end,
                            group_size, min_ts, max_ts,
                        )
                        computed["windows"][frame.spec.name] = metrics
                        if frame_left is not None and frame_left < lowest_left:
                            lowest_left = frame_left
                    cache.release_before(lowest_left)
                    payload = (
                        '{"source_row": ' + str(source_row)
                        + ', "row": ' + raw_json
                        + ', "computed": '
                        + json.dumps(computed, ensure_ascii=False)
                        + "}"
                    )
                    result_batch.append((source_row, payload))
                    if len(result_batch) >= config.batch_size:
                        _flush_results(db, result_batch)
                        result_batch.clear()
            for frame in frames:
                frame.reset()
    _flush_results(db, result_batch)
    db.commit()


def _reference(db: sqlite3.Connection, part_blob: bytes, pos: int):
    row = db.execute(
        "SELECT source_row, value_text FROM records "
        "WHERE part_key = ? AND pos = ?",
        (part_blob, pos),
    ).fetchone()
    if row is None:
        return None
    source_row, value_text = row
    return {
        "source_row": source_row,
        "value": int(value_text) if value_text is not None else None,
    }


def _range_endpoints(db: sqlite3.Connection, part_blob: bytes, ts: int,
                     spec: WindowSpec, min_ts: int, max_ts: int
                     ) -> Optional[Tuple[int, int]]:
    raw_lo = ts + spec.start
    raw_hi = ts + spec.end
    # Mathematical emptiness test first, using exact Python integers.
    if raw_hi < min_ts or raw_lo > max_ts:
        return None

    def inside(candidate_ts: int) -> bool:
        return raw_lo <= candidate_ts <= raw_hi

    def first_inside(descending: bool) -> Optional[int]:
        """Return the first stored position satisfying ``inside``.

        The index supplies ordered candidates.  A bound that fits in int64 is
        bound directly so the seek is tight; an unrepresentable bound forces
        a scan from the corresponding storage edge, but the loop still walks
        in timestamp order and stops as soon as the interval is passed.
        """
        order = "DESC, source_row DESC" if descending else "ASC, source_row"
        if descending:
            if raw_hi > INT64_MAX:
                sql = (
                    "SELECT ts, pos FROM records WHERE part_key = ? "
                    f"ORDER BY ts {order}"
                )
                params = (part_blob,)
            else:
                sql = (
                    "SELECT ts, pos FROM records WHERE part_key = ? "
                    f"AND ts <= ? ORDER BY ts {order}"
                )
                params = (part_blob, raw_hi)
        else:
            if raw_lo < INT64_MIN:
                sql = (
                    "SELECT ts, pos FROM records WHERE part_key = ? "
                    f"ORDER BY ts {order}"
                )
                params = (part_blob,)
            else:
                sql = (
                    "SELECT ts, pos FROM records WHERE part_key = ? "
                    f"AND ts >= ? ORDER BY ts {order}"
                )
                params = (part_blob, raw_lo)

        for candidate_ts, candidate_pos in db.execute(sql, params):
            if inside(candidate_ts):
                return candidate_pos
            # Ordered scan: once it has crossed past the interval on its
            # direction's side, no later row can be a member.
            if descending:
                if candidate_ts < raw_lo:
                    return None
                # hi unrepresentable: rows above raw_hi are skipped until the
                # scan drops into the interval; a row already below raw_hi but
                # still outside must not be treated as a member, and the next
                # rows are even smaller, so keep scanning only while >= raw_lo
                # (handled by the check above).
            elif candidate_ts > raw_hi:
                return None
        return None

    low_pos = first_inside(descending=False)
    if low_pos is None:
        return None
    high_pos = first_inside(descending=True)
    if high_pos is None:
        return None
    return low_pos, high_pos


def _rows_endpoints(pos: int, spec: WindowSpec, group_size: int
                    ) -> Optional[Tuple[int, int]]:
    raw_lo = pos + spec.start
    raw_hi = pos + spec.end
    if raw_hi < 1 or raw_lo > group_size:
        return None
    return max(1, raw_lo), min(group_size, raw_hi)


def _frame_metrics(db, frame: _FrameState, part_blob: bytes, pos: int, ts: int,
                   peer_start: int, peer_end: int, group_size: int,
                   min_ts: int, max_ts: int) -> Tuple[dict, Optional[int]]:
    spec = frame.spec
    if spec.mode == "RANGE":
        endpoints = _range_endpoints(db, part_blob, ts, spec, min_ts, max_ts)
    else:
        endpoints = _rows_endpoints(pos, spec, group_size)
    if endpoints is None:
        return (
            {"count_all": 0, "count_value": 0, "sum": None,
             "min": None, "max": None},
            None,
        )
    left, right = endpoints

    empty = {"count_all": 0, "count_value": 0, "sum": None,
             "min": None, "max": None}

    if spec.exclude == "none":
        metrics = _merge_metrics(
            [frame.left.update(left, right)], right - left + 1
        )
        return metrics, left

    if spec.exclude == "current":
        left_piece = (left, min(right, pos - 1))
        right_piece = (max(left, pos + 1), right)
    else:  # group: subtract the entire same-timestamp peer
        left_piece = (left, min(right, peer_start - 1))
        right_piece = (max(left, peer_end + 1), right)

    pieces_metrics = []
    count_all = 0
    frame_low: Optional[int] = None
    if left_piece[0] <= left_piece[1]:
        pieces_metrics.append(frame.left.update(*left_piece))
        count_all += left_piece[1] - left_piece[0] + 1
        frame_low = left_piece[0]
    if right_piece[0] <= right_piece[1]:
        pieces_metrics.append(frame.right.update(*right_piece))
        count_all += right_piece[1] - right_piece[0] + 1
        if frame_low is None:
            frame_low = right_piece[0]
    if not pieces_metrics:
        return empty, left
    return _merge_metrics(pieces_metrics, count_all), frame_low


# -- pass 4: source-order delivery --------------------------------------------

def _deliver(db: sqlite3.Connection, output_tmp: str,
             config: WindowConfig) -> None:
    cursor = db.execute(
        "SELECT payload FROM results ORDER BY source_row"
    )
    with open(output_tmp, "w", encoding="utf-8", newline="\n") as handle:
        while True:
            page = cursor.fetchmany(config.batch_size)
            if not page:
                break
            for (payload,) in page:
                handle.write(payload)
                handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _flush_results(db: sqlite3.Connection, batch: List[tuple]) -> None:
    if not batch:
        return
    db.executemany(
        "INSERT INTO results(source_row, payload) VALUES (?, ?)", batch
    )
