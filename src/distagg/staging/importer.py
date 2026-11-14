"""Local batch importer for supplier correction TSV files.

This module is the file-import / problem-location entry point for the
backfill workflow.  It reads one UTF-8 TSV file per run, classifies every
physical line, writes the acceptable corrections into the *existing*
:class:`~distagg.staging.StagingStore` in configured row batches, and leaves
the unacceptable rows (with their original bytes, file-relative path and
line number) for operations staff.  It never touches the exposure/tag
aggregation database and never extends the staging store's key/value
semantics: a correction is exactly one ``("put", key, value)`` batch op on
arbitrary ``bytes``.

Input contract
--------------
The first physical line is a UTF-8 header naming the columns.  Every later
physical line holds, at least, the columns declared in the JSON config:

* the supplier column,
* the in-file correction-id column (unique per file),
* the byte-key column and the value column, both hexadecimal text.

Hexadecimal fields distinguish three cases that must never be conflated:

==============================  ======================================
raw field text                  meaning
==============================  ======================================
empty string (``""``)           an *empty field*: missing data, isolated
``0x``                          the empty byte string ``b""``
``0x6162`` / ``6162``           the bytes ``b"ab"``
``a``, ``0xzz``                 illegal hexadecimal, isolated
==============================  ======================================

The ``0x`` prefix is the only way to express the empty byte string; an
un-prefixed odd-length or non-hex text is rejected, never guessed at.

Classification (identical in precheck and run modes)
----------------------------------------------------
* ``importable`` — a well-formed, parseable correction;
* ``duplicate`` — a later row whose correction id was seen before and whose
  every column text is identical to the first row's;
* ``conflict`` — rows sharing a correction id whose fields differ.  *All*
  rows of a conflicted id are isolated, so a later value can never silently
  overwrite an earlier one;
* ``quarantined`` — any other malformed row (bad encoding, bad structure,
  empty required field, supplier mismatch, illegal hexadecimal, ...).

Resumability
------------
Each confirmed batch is recorded durably (file fingerprint, physical line
progress, per-class counts, staging commit) before the next batch starts.
Re-running the same file never re-writes a confirmed batch; re-running it
with changed content is refused outright (or must start explicitly under a
new import identity).  A torn run is reconciled against the live staging
store before writing resumes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .errors import StagingError
from .store import StagingStore

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class ImportErrorBase(StagingError):
    """Base class for importer failures; carries a machine-readable code."""


class ImportConfigError(ImportErrorBase):
    """The import configuration JSON is missing fields or has bad values."""

    code = "import_config_error"

    def __init__(self, message: str, issues: Optional[List[str]] = None):
        super().__init__(message)
        self.issues = issues or []

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["issues"] = list(self.issues)
        return d


class ImportPathError(ImportErrorBase):
    """The input path is unsafe (symlink, escape, missing, not a file)."""

    code = "import_path_error"


class ImportHeaderError(ImportErrorBase):
    """The TSV header cannot be used (encoding, duplicate/missing columns)."""

    code = "import_header_error"

    def __init__(self, message: str, issues: Optional[List[str]] = None):
        super().__init__(message)
        self.issues = issues or []

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["issues"] = list(self.issues)
        return d


class ImportFingerprintError(ImportErrorBase):
    """A resume run sees different content/config than the recorded run."""

    code = "import_fingerprint_mismatch"


class ImportStateError(ImportErrorBase):
    """The import state directory is damaged or internally inconsistent."""

    code = "import_state_error"


class ImportResumeError(ImportErrorBase):
    """The store cannot be reconciled with the recorded batch boundary."""

    code = "import_resume_ambiguous"


class ImportReportError(ImportErrorBase):
    """The report could not be written; the data batches may be committed."""

    code = "import_report_write_failed"


# Row-level quarantine reason codes (stable strings used in JSON output).
REASON_BLANK_LINE = "blank_line"
REASON_FIELD_COUNT = "field_count_mismatch"
REASON_INVALID_UTF8 = "invalid_utf8"
REASON_EMPTY_FIELD = "empty_field"
REASON_SUPPLIER_MISMATCH = "supplier_mismatch"
REASON_HEX_ODD_LENGTH = "hex_odd_length"
REASON_HEX_BAD_CHARACTER = "hex_bad_character"
REASON_CONFLICT = "conflict"

# Row statuses.
STATUS_IMPORTABLE = "importable"
STATUS_DUPLICATE = "duplicate"
STATUS_CONFLICT = "conflict"
STATUS_QUARANTINED = "quarantined"

_STATUSES = (STATUS_IMPORTABLE, STATUS_DUPLICATE, STATUS_CONFLICT, STATUS_QUARANTINED)

_HEX_DIGITS = set("0123456789abcdefABCDEF")
_IMPORT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_MAX_RAW_SAMPLE_BYTES = 512
DEFAULT_MAX_SAMPLES = 10
CONFIG_FORMAT = 1
META_FORMAT = 1
REPORT_FORMAT = 1


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportConfig:
    """Validated import configuration.

    ``supplier_expected`` is ``None`` when the supplier column is not value
    checked; otherwise every accepted row must carry exactly that text.
    """

    supplier_column: str
    correction_id_column: str
    key_column: str
    value_column: str
    required_columns: Tuple[str, ...]
    allowed_extra_columns: Tuple[str, ...]
    supplier_expected: Optional[str]
    batch_rows: int
    raw: dict

    @property
    def all_columns(self) -> Tuple[str, ...]:
        return self.required_columns + self.allowed_extra_columns

    def fingerprint(self) -> str:
        payload = json.dumps(self.raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def load_config(path: str) -> ImportConfig:
    """Read and validate an import configuration JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise ImportConfigError(f"config file not found: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportConfigError(f"cannot read config {path}: {exc}")
    return parse_config(raw)


def parse_config(raw: object) -> ImportConfig:
    if not isinstance(raw, dict):
        raise ImportConfigError("import config must be a JSON object")
    issues: List[str] = []

    def require_str(key: str) -> Optional[str]:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            issues.append(f"{key!r} must be a non-empty string")
            return None
        return value

    fmt = raw.get("format", 1)
    if fmt != 1:
        issues.append(f"unsupported format {fmt!r}; expected 1")
    supplier_column = require_str("supplier_column")
    correction_id_column = require_str("correction_id_column")
    key_column = require_str("key_column")
    value_column = require_str("value_column")

    required = raw.get("required_columns")
    if not isinstance(required, list) or not required or not all(
        isinstance(name, str) and name for name in required
    ):
        issues.append("'required_columns' must be a non-empty list of column names")
        required = []

    extras = raw.get("allowed_extra_columns", [])
    if not isinstance(extras, list) or not all(isinstance(name, str) and name for name in extras):
        issues.append("'allowed_extra_columns' must be a list of column names")
        extras = []

    supplier = raw.get("supplier")
    supplier_expected: Optional[str] = None
    if supplier is not None:
        if not isinstance(supplier, dict):
            issues.append("'supplier' must be an object")
        else:
            expected = supplier.get("expected")
            if expected is not None and (not isinstance(expected, str) or not expected):
                issues.append("supplier.expected must be a non-empty string when present")
            elif isinstance(expected, str):
                supplier_expected = expected

    batch_rows = raw.get("batch_rows", 100)
    if not isinstance(batch_rows, int) or isinstance(batch_rows, bool) or batch_rows <= 0:
        issues.append("'batch_rows' must be a positive integer")
        batch_rows = 100

    named = [supplier_column, correction_id_column, key_column, value_column]
    if issues:
        raise ImportConfigError("invalid import configuration", issues)

    # Structural column rules once the basic fields are usable.
    named = [name for name in named if name]
    if len(set(named)) != len(named):
        issues.append("supplier/correction_id/key/value columns must be distinct")
    required_tuple = tuple(required)
    extras_tuple = tuple(extras)
    if len(set(required_tuple)) != len(required_tuple):
        issues.append("'required_columns' contains duplicates")
    if len(set(extras_tuple)) != len(extras_tuple):
        issues.append("'allowed_extra_columns' contains duplicates")
    overlap = set(required_tuple) & set(extras_tuple)
    if overlap:
        issues.append(f"columns listed both as required and extra: {sorted(overlap)}")

    declared = set(required_tuple) | set(extras_tuple)
    for name in named:
        if name not in declared:
            issues.append(
                f"named column {name!r} must appear in required_columns or allowed_extra_columns"
            )
    if issues:
        raise ImportConfigError("invalid import configuration", issues)

    normalised = dict(raw)
    normalised["format"] = 1
    normalised["required_columns"] = list(required_tuple)
    normalised["allowed_extra_columns"] = list(extras_tuple)
    normalised["batch_rows"] = batch_rows
    return ImportConfig(
        supplier_column=supplier_column,
        correction_id_column=correction_id_column,
        key_column=key_column,
        value_column=value_column,
        required_columns=required_tuple,
        allowed_extra_columns=extras_tuple,
        supplier_expected=supplier_expected,
        batch_rows=batch_rows,
        raw=normalised,
    )


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def resolve_input_path(path: str, root: Optional[str], allow_symlinks: bool = False) -> str:
    """Validate an input path and return its absolute, symlink-free form.

    The path must stay inside ``root`` (the current working directory by
    default), every component is checked with ``lstat`` so that symlinked
    directories cannot smuggle a file past the boundary, and the final
    target must be a regular file.  Symlinks are rejected explicitly unless
    ``allow_symlinks`` is set; the input is never modified.
    """
    base = os.path.abspath(root) if root else os.getcwd()
    if not os.path.isdir(base):
        raise ImportPathError(f"root directory does not exist: {base}")
    abs_path = os.path.abspath(path)
    try:
        common = os.path.commonpath([base, abs_path])
    except ValueError:
        common = ""
    if common != base:
        raise ImportPathError(
            f"input path {path!r} escapes the allowed root {base!r} (resolved to {abs_path!r})"
        )
    # Walk every component below the root with lstat: a symlinked directory
    # is caught even when the final target looks innocuous.
    rel = os.path.relpath(abs_path, base)
    cursor = base
    for component in rel.split(os.sep):
        if component in ("", os.curdir):
            continue
        if component == os.pardir:
            raise ImportPathError(f"input path {path!r} escapes the allowed root via '..'")
        cursor = os.path.join(cursor, component)
        try:
            info = os.lstat(cursor)
        except FileNotFoundError:
            raise ImportPathError(f"input path does not exist: {abs_path}")
        if stat.S_ISLNK(info.st_mode):
            if not allow_symlinks:
                raise ImportPathError(
                    f"symlink inputs are refused by default: {cursor} -> "
                    f"{os.readlink(cursor)!r}; pass allow_symlinks to follow explicitly"
                )
            try:
                info = os.stat(cursor)  # follow the final link deliberately
            except OSError as exc:
                raise ImportPathError(f"dangling symlink: {cursor}: {exc}")
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise ImportPathError(f"input is not a regular file or directory: {cursor}")
    if not os.path.isfile(abs_path):
        raise ImportPathError(f"input path is not a regular file: {abs_path}")
    return abs_path


def relative_report_path(abs_path: str, root: Optional[str]) -> str:
    """Path stored in reports: relative to the root when possible."""
    base = os.path.abspath(root) if root else os.getcwd()
    try:
        rel = os.path.relpath(abs_path, base)
        if not rel.startswith(os.pardir):
            return rel
    except ValueError:
        pass
    return abs_path


# ---------------------------------------------------------------------------
# TSV parsing and row classification
# ---------------------------------------------------------------------------


@dataclass
class RowRecord:
    """One physical data row and its classification."""

    line_no: int
    raw_line: bytes
    status: str
    reason: Optional[str] = None
    detail: Optional[str] = None
    field_indexes: Tuple[int, ...] = ()
    correction_id: Optional[str] = None
    fields: Optional[Tuple[str, ...]] = None  # decoded texts, only when valid
    key: Optional[bytes] = None
    value: Optional[bytes] = None

    def to_report_dict(self, include_raw: bool) -> dict:
        out = {"line_no": self.line_no, "status": self.status}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.detail is not None:
            out["detail"] = self.detail
        if self.field_indexes:
            out["field_indexes"] = list(self.field_indexes)
        if self.correction_id is not None:
            out["correction_id"] = self.correction_id
        if self.key is not None:
            out["key_hex"] = self.key.hex()
        if self.value is not None:
            out["value_hex"] = self.value.hex()
        if include_raw:
            truncated = len(self.raw_line) > _MAX_RAW_SAMPLE_BYTES
            out["raw_line_b64"] = base64.b64encode(
                self.raw_line[:_MAX_RAW_SAMPLE_BYTES]
            ).decode("ascii")
            out["raw_truncated"] = truncated
        return out


@dataclass
class ParsedFile:
    header: Tuple[str, ...]
    rows: List[RowRecord]
    fingerprint: str
    size_bytes: int
    counts: Dict[str, int] = field(default_factory=dict)
    quarantine_reasons: Dict[str, int] = field(default_factory=dict)
    conflict_ids: List[str] = field(default_factory=list)
    batches: List[dict] = field(default_factory=list)


def _physical_lines(data: bytes):
    """Yield ``(line_no, raw_line)`` with line numbers starting at 1.

    The line terminator (``\\n`` and one optional preceding ``\\r``) is
    removed, but every other byte — including interior ``\\r`` — is kept
    verbatim in ``raw_line``.  A single trailing newline produces no empty
    final row; an interior empty line yields ``b""`` and is classified as a
    blank line rather than silently skipped.
    """
    line_no = 0
    start = 0
    length = len(data)
    while start <= length:
        nl = data.find(b"\n", start)
        if nl == -1:
            segment = data[start:]
            line_no += 1
            if segment:
                yield line_no, segment
            break
        segment = data[start:nl]
        if segment.endswith(b"\r"):
            segment = segment[:-1]
        line_no += 1
        yield line_no, segment
        start = nl + 1


def _decode_hex_field(text: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Return ``(bytes, None)`` or ``(None, reason)``.

    ``""`` is an empty field (caller decides whether that is allowed);
    ``0x`` marks the empty byte string; everything else must be even-length
    hexadecimal, with or without the ``0x`` prefix.
    """
    if text == "":
        return None, REASON_EMPTY_FIELD
    if text.startswith("0x"):
        hex_text = text[2:]
    else:
        hex_text = text
    if len(hex_text) % 2 != 0:
        return None, REASON_HEX_ODD_LENGTH
    if any(ch not in _HEX_DIGITS for ch in hex_text):
        return None, REASON_HEX_BAD_CHARACTER
    return bytes.fromhex(hex_text), None


def parse_header(raw_header: bytes, config: ImportConfig) -> Tuple[str, ...]:
    """Validate the header line; raises :class:`ImportHeaderError`."""
    issues: List[str] = []
    try:
        header_text = raw_header.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ImportHeaderError(
            "header line is not valid UTF-8", [f"utf-8 decode error: {exc}"]
        )
    names = tuple(header_text.split("\t"))
    seen: Dict[str, int] = {}
    for index, name in enumerate(names):
        if name == "":
            issues.append(f"column #{index} has an empty name")
        if name in seen:
            issues.append(f"duplicate column {name!r} at positions {seen[name]} and {index}")
        else:
            seen[name] = index
    declared = set(config.all_columns)
    for required in config.all_columns:
        if required not in seen:
            issues.append(f"declared column {required!r} is missing from the header")
    for name in names:
        if name and name not in declared:
            issues.append(f"header column {name!r} is not declared in the configuration")
    if issues:
        raise ImportHeaderError("TSV header does not match the import configuration", issues)
    return names


def _classify_data_row(line_no: int, raw: bytes, header: Tuple[str, ...],
                       config: ImportConfig) -> RowRecord:
    if raw == b"":
        return RowRecord(line_no, raw, STATUS_QUARANTINED, reason=REASON_BLANK_LINE,
                         detail="empty physical line")
    parts = raw.split(b"\t")
    if len(parts) != len(header):
        return RowRecord(
            line_no, raw, STATUS_QUARANTINED, reason=REASON_FIELD_COUNT,
            detail=f"expected {len(header)} fields, found {len(parts)}",
        )
    bad_indexes = []
    texts: List[str] = []
    for index, part in enumerate(parts):
        try:
            texts.append(part.decode("utf-8", errors="strict"))
        except UnicodeDecodeError:
            bad_indexes.append(index)
            texts.append(part.decode("utf-8", errors="replace"))
    if bad_indexes:
        return RowRecord(
            line_no, raw, STATUS_QUARANTINED, reason=REASON_INVALID_UTF8,
            detail="field is not valid UTF-8", field_indexes=tuple(bad_indexes),
        )

    fields = tuple(texts)
    index_of = {name: i for i, name in enumerate(header)}

    def col(name: str) -> str:
        return fields[index_of[name]]

    # Empty required fields (covers the empty-field vs empty-bytes distinction).
    empties = [
        index_of[name]
        for name in config.required_columns
        if fields[index_of[name]] == ""
    ]
    if empties:
        return RowRecord(
            line_no, raw, STATUS_QUARANTINED, reason=REASON_EMPTY_FIELD,
            detail="a required column is an empty field", field_indexes=tuple(empties),
            correction_id=_safe_id(fields, index_of, config),
        )

    if config.supplier_expected is not None:
        observed = col(config.supplier_column)
        if observed != config.supplier_expected:
            return RowRecord(
                line_no, raw, STATUS_QUARANTINED, reason=REASON_SUPPLIER_MISMATCH,
                detail=f"expected supplier {config.supplier_expected!r}, found {observed!r}",
                correction_id=_safe_id(fields, index_of, config),
            )

    key, key_reason = _decode_hex_field(col(config.key_column))
    if key_reason is not None:
        return RowRecord(
            line_no, raw, STATUS_QUARANTINED, reason=key_reason,
            detail=f"key column {config.key_column!r} is not usable hexadecimal",
            field_indexes=(index_of[config.key_column],),
            correction_id=_safe_id(fields, index_of, config),
        )
    value, value_reason = _decode_hex_field(col(config.value_column))
    if value_reason is not None:
        return RowRecord(
            line_no, raw, STATUS_QUARANTINED, reason=value_reason,
            detail=f"value column {config.value_column!r} is not usable hexadecimal",
            field_indexes=(index_of[config.value_column],),
            correction_id=_safe_id(fields, index_of, config),
        )

    return RowRecord(
        line_no, raw, STATUS_IMPORTABLE,
        correction_id=col(config.correction_id_column),
        fields=fields, key=key, value=value,
    )


def _safe_id(fields, index_of, config) -> Optional[str]:
    name = config.correction_id_column
    idx = index_of.get(name)
    if idx is None or idx >= len(fields):
        return None
    value = fields[idx]
    return value if value != "" else None


def classify_file(path: str, config: ImportConfig) -> ParsedFile:
    """Parse one TSV file and classify every physical row.

    The file is read once as raw bytes (so no malformed line can damage the
    parsing of the next), the fingerprint covers the exact bytes on disk,
    and classification is deterministic — precheck and run on the same
    bytes always produce the same result.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    fingerprint = hashlib.sha256(data).hexdigest()
    lines = list(_physical_lines(data))
    if not lines:
        raise ImportHeaderError("the input file is empty; the header line is required")
    header = parse_header(lines[0][1], config)

    rows = [
        _classify_data_row(line_no, raw, header, config)
        for line_no, raw in lines[1:]
    ]
    _apply_correction_id_rules(rows, config)
    parsed = ParsedFile(
        header=header, rows=rows, fingerprint=fingerprint, size_bytes=len(data),
    )
    _fill_summary(parsed, config)
    return parsed


def _apply_correction_id_rules(rows: List[RowRecord], config: ImportConfig) -> None:
    """Second pass: duplicate vs conflict on the correction id.

    Only well-formed rows participate.  The first row of an id keeps its
    ``importable`` status; later rows are exact duplicates when every column
    text matches, otherwise the whole id group becomes a conflict and none
    of its rows may reach the store.
    """
    first_by_id: Dict[str, RowRecord] = {}
    conflicted: Dict[str, bool] = {}
    for row in rows:
        if row.status != STATUS_IMPORTABLE or row.correction_id is None:
            continue
        cid = row.correction_id
        if cid not in first_by_id:
            first_by_id[cid] = row
            continue
        if conflicted.get(cid):
            continue
        first = first_by_id[cid]
        if row.fields == first.fields:
            row.status = STATUS_DUPLICATE
            row.reason = None
            row.detail = "identical repeat of line %d" % first.line_no
        else:
            conflicted[cid] = True

    if not conflicted:
        return
    for row in rows:
        if row.status in (STATUS_IMPORTABLE, STATUS_DUPLICATE) and row.correction_id in conflicted:
            row.status = STATUS_CONFLICT
            row.reason = REASON_CONFLICT
            row.detail = "correction id %r appears with differing fields" % row.correction_id


def _fill_summary(parsed: ParsedFile, config: ImportConfig) -> None:
    counts = {status: 0 for status in _STATUSES}
    reasons: Dict[str, int] = {}
    conflict_ids = set()
    importable: List[RowRecord] = []
    for row in parsed.rows:
        counts[row.status] += 1
        if row.status == STATUS_QUARANTINED:
            reasons[row.reason] = reasons.get(row.reason, 0) + 1
        elif row.status == STATUS_CONFLICT:
            reasons[REASON_CONFLICT] = reasons.get(REASON_CONFLICT, 0) + 1
            conflict_ids.add(row.correction_id)
        elif row.status == STATUS_IMPORTABLE:
            importable.append(row)
    parsed.counts = counts
    parsed.quarantine_reasons = dict(sorted(reasons.items()))
    parsed.conflict_ids = sorted(cid for cid in conflict_ids if cid is not None)
    parsed.batches = plan_batches(importable, config.batch_rows)


def plan_batches(importable: List[RowRecord], batch_rows: int) -> List[dict]:
    """Split importable rows (file order) into contiguous configured batches."""
    plans = []
    for start in range(0, len(importable), batch_rows):
        chunk = importable[start:start + batch_rows]
        plans.append({
            "batch_index": len(plans) + 1,
            "row_count": len(chunk),
            "first_line": chunk[0].line_no,
            "last_line": chunk[-1].line_no,
            "physical_lines": [row.line_no for row in chunk],
            "entries": [
                {"line_no": row.line_no, "key_hex": row.key.hex(),
                 "value_hex": row.value.hex()}
                for row in chunk
            ],
        })
    return plans


# ---------------------------------------------------------------------------
# Durable import state
# ---------------------------------------------------------------------------

META_NAME = "meta.json"
BATCHES_NAME = "batches.jsonl"
QUARANTINE_NAME = "quarantine.jsonl"
LOCK_NAME = "LOCK"
REPORTS_DIR = "reports"
PRECHECK_REPORT = "precheck.json"
RUN_REPORT = "run.json"


def _atomic_write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _json_dumps(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


class ImportState:
    """File-backed, flock-protected progress state for one import identity."""

    def __init__(self, state_dir: str):
        self.state_dir = os.path.abspath(state_dir)
        self.lock_fd = None
        self.meta_path = os.path.join(self.state_dir, META_NAME)
        self.batches_path = os.path.join(self.state_dir, BATCHES_NAME)
        self.quarantine_path = os.path.join(self.state_dir, QUARANTINE_NAME)
        self.reports_dir = os.path.join(self.state_dir, REPORTS_DIR)

    def __enter__(self) -> "ImportState":
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)
        self.lock_fd = os.open(os.path.join(self.state_dir, LOCK_NAME),
                               os.O_CREAT | os.O_RDWR, 0o644)
        try:
            import fcntl

            fcntl.flock(self.lock_fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - POSIX only in practice
            pass
        return self

    def __exit__(self, *exc) -> None:
        if self.lock_fd is not None:
            try:
                import fcntl

                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            os.close(self.lock_fd)
            self.lock_fd = None

    def has_meta(self) -> bool:
        return os.path.exists(self.meta_path)

    def read_meta(self) -> dict:
        try:
            with open(self.meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ImportStateError(f"cannot read import state {self.meta_path}: {exc}")
        if not isinstance(meta, dict) or meta.get("kind") != "distagg-import-meta":
            raise ImportStateError(f"{self.meta_path} is not a distagg import meta document")
        return meta

    def write_meta(self, meta: dict) -> None:
        _atomic_write(self.meta_path, _json_dumps(meta))

    def read_batches(self) -> List[dict]:
        if not os.path.exists(self.batches_path):
            return []
        records = []
        try:
            with open(self.batches_path, "r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ImportStateError(
                            f"{self.batches_path}:{line_no} is not valid JSON: {exc}"
                        )
                    records.append(record)
        except OSError as exc:
            raise ImportStateError(f"cannot read {self.batches_path}: {exc}")
        indexes = [record.get("batch_index") for record in records]
        if indexes != list(range(1, len(indexes) + 1)):
            raise ImportStateError(
                f"{self.batches_path} batch indexes are not a contiguous 1..n sequence"
            )
        return records

    def append_batch(self, record: dict) -> None:
        with open(self.batches_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def write_quarantine(self, parsed: ParsedFile) -> None:
        """Persist the full original bytes of every isolated row."""
        lines = []
        for row in parsed.rows:
            if row.status not in (STATUS_QUARANTINED, STATUS_CONFLICT):
                continue
            lines.append(json.dumps({
                "line_no": row.line_no,
                "status": row.status,
                "reason": row.reason,
                "detail": row.detail,
                "field_indexes": list(row.field_indexes),
                "correction_id": row.correction_id,
                "raw_line_b64": base64.b64encode(row.raw_line).decode("ascii"),
            }, ensure_ascii=False, sort_keys=True))
        _atomic_write(self.quarantine_path,
                      ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))


def default_import_id(relative_path: str) -> str:
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]
    return "imp-" + digest


def validate_import_id(import_id: str) -> str:
    if not _IMPORT_ID_RE.match(import_id):
        raise ImportConfigError(
            f"import id {import_id!r} must match {_IMPORT_ID_RE.pattern}")
    return import_id


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _bounded_samples(parsed: ParsedFile, max_samples: int) -> dict:
    buckets = {
        "quarantined": [],
        "conflicts": [],
        "duplicates": [],
        "importable": [],
    }
    for row in parsed.rows:
        if row.status == STATUS_QUARANTINED and len(buckets["quarantined"]) < max_samples:
            buckets["quarantined"].append(row.to_report_dict(include_raw=True))
        elif row.status == STATUS_CONFLICT and len(buckets["conflicts"]) < max_samples:
            buckets["conflicts"].append(row.to_report_dict(include_raw=True))
        elif row.status == STATUS_DUPLICATE and len(buckets["duplicates"]) < max_samples:
            buckets["duplicates"].append(row.to_report_dict(include_raw=False))
        elif row.status == STATUS_IMPORTABLE and len(buckets["importable"]) < max_samples:
            buckets["importable"].append(row.to_report_dict(include_raw=False))
    return buckets


def build_report(mode: str, import_id: str, rel_path: str, parsed: ParsedFile,
                 config: ImportConfig, max_samples: int,
                 staging_info: Optional[dict], batch_records: List[dict],
                 status: str) -> dict:
    return {
        "kind": "distagg-import-report",
        "format": REPORT_FORMAT,
        "mode": mode,
        "status": status,
        "import_id": import_id,
        "input": {
            "relative_path": rel_path,
            "fingerprint_sha256": parsed.fingerprint,
            "size_bytes": parsed.size_bytes,
            "total_data_rows": len(parsed.rows),
        },
        "config": config.raw,
        "config_fingerprint_sha256": config.fingerprint(),
        "counts": {
            "total_rows": len(parsed.rows),
            "importable": parsed.counts.get(STATUS_IMPORTABLE, 0),
            "duplicate": parsed.counts.get(STATUS_DUPLICATE, 0),
            "conflict": parsed.counts.get(STATUS_CONFLICT, 0),
            "quarantined": parsed.counts.get(STATUS_QUARANTINED, 0),
        },
        "quarantine_reasons": parsed.quarantine_reasons,
        "conflict_correction_ids": parsed.conflict_ids,
        "sample_limit": max_samples,
        "samples": _bounded_samples(parsed, max_samples),
        "batches": [
            {
                "batch_index": plan["batch_index"],
                "row_count": plan["row_count"],
                "first_line": plan["first_line"],
                "last_line": plan["last_line"],
            }
            for plan in parsed.batches
        ],
        "batch_commitments": batch_records,
        "staging": staging_info,
        "rows": [row.to_report_dict(include_raw=False) for row in parsed.rows],
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def precheck(config: ImportConfig, input_path: str, state_dir: str, *,
             root: Optional[str] = None, allow_symlinks: bool = False,
             import_id: Optional[str] = None,
             max_samples: int = DEFAULT_MAX_SAMPLES) -> dict:
    """Classify the file and write a precheck report; the store is untouched."""
    if max_samples <= 0:
        raise ImportConfigError("max_samples must be a positive integer")
    abs_path = resolve_input_path(input_path, root, allow_symlinks)
    rel_path = relative_report_path(abs_path, root)
    if import_id is None:
        import_id = default_import_id(rel_path)
    validate_import_id(import_id)
    parsed = classify_file(abs_path, config)
    report = build_report(
        "precheck", import_id, rel_path, parsed, config, max_samples,
        staging_info=None, batch_records=[], status="complete",
    )
    with ImportState(state_dir):
        _atomic_write(os.path.join(state_dir, REPORTS_DIR, PRECHECK_REPORT),
                      _json_dumps(report))
    return report


def run_import(config: ImportConfig, input_path: str, store_dir: str, state_dir: str, *,
               root: Optional[str] = None, allow_symlinks: bool = False,
               import_id: Optional[str] = None,
               max_samples: int = DEFAULT_MAX_SAMPLES,
               kill_after_batch: Optional[int] = None,
               fail_report_write: bool = False,
               pause=None) -> dict:
    """Execute the import, batching acceptable rows into the staging store.

    Returns the completed report.  Progress is durable per batch; the same
    file can be re-run to resume.  ``kill_after_batch`` hard-kills the
    process after the named batch's progress is durable (test hook), and
    ``fail_report_write`` simulates a report failure after the data batches
    are committed.
    """
    if max_samples <= 0:
        raise ImportConfigError("max_samples must be a positive integer")
    if kill_after_batch is not None and kill_after_batch <= 0:
        raise ImportConfigError("kill_after_batch must be a positive integer")
    abs_path = resolve_input_path(input_path, root, allow_symlinks)
    rel_path = relative_report_path(abs_path, root)
    if import_id is None:
        import_id = default_import_id(rel_path)
    validate_import_id(import_id)
    parsed = classify_file(abs_path, config)

    with ImportState(state_dir) as state:
        meta = _begin_or_resume(state, import_id, rel_path, abs_path, parsed, config, store_dir)
        state.write_quarantine(parsed)
        batch_records = state.read_batches()

        store = StagingStore.open(store_dir)
        try:
            batch_records = _reconcile_and_write(
                store, state, meta, parsed, config, batch_records,
                kill_after_batch=kill_after_batch, pause=pause,
            )
            staging_info = {
                "dir": os.path.abspath(store_dir),
                "confirmed_batches": len(batch_records),
                "confirmed_rows": sum(record["row_count"] for record in batch_records),
                "last_commit": store.commit_number,
            }
        finally:
            store.close()

        meta["status"] = "complete"
        meta["staging"] = staging_info
        state.write_meta(meta)

        report = build_report(
            "run", import_id, rel_path, parsed, config, max_samples,
            staging_info=staging_info, batch_records=batch_records, status="complete",
        )
        report_path = os.path.join(state.reports_dir, RUN_REPORT)
        if fail_report_write:
            raise ImportReportError(
                "simulated report write failure: data batches up to "
                f"{staging_info['confirmed_batches']} are committed in {store_dir}; "
                f"consult {state.state_dir} for the durable batch boundary"
            )
        try:
            _atomic_write(report_path, _json_dumps(report))
        except OSError as exc:
            raise ImportReportError(
                f"could not write report {report_path}: {exc}; data batches up to "
                f"{staging_info['confirmed_batches']} are committed in {store_dir}"
            ) from exc
        return report


def _begin_or_resume(state: ImportState, import_id: str, rel_path: str,
                     abs_path: str, parsed: ParsedFile, config: ImportConfig,
                     store_dir: str) -> dict:
    fingerprint = parsed.fingerprint
    config_fp = config.fingerprint()
    if not state.has_meta():
        meta = {
            "kind": "distagg-import-meta",
            "format": META_FORMAT,
            "import_id": import_id,
            "status": "running",
            "input": {
                "relative_path": rel_path,
                "abs_path": abs_path,
                "fingerprint_sha256": fingerprint,
                "size_bytes": parsed.size_bytes,
            },
            "config_fingerprint_sha256": config_fp,
            "config": config.raw,
            "staging_dir": os.path.abspath(store_dir),
            "counts": parsed.counts,
            "quarantine_reasons": parsed.quarantine_reasons,
            "conflict_correction_ids": parsed.conflict_ids,
            "total_data_rows": len(parsed.rows),
            "last_physical_line": parsed.rows[-1].line_no if parsed.rows else 1,
            "last_batch_index": 0,
            "staging": None,
        }
        state.write_meta(meta)
        return meta

    meta = state.read_meta()
    if meta.get("import_id") != import_id:
        raise ImportFingerprintError(
            f"state directory {state.state_dir} already belongs to import "
            f"{meta.get('import_id')!r}; start a new import identity in a new directory"
        )
    recorded_fp = meta.get("input", {}).get("fingerprint_sha256")
    if recorded_fp != fingerprint:
        raise ImportFingerprintError(
            "the input file content changed since the recorded run: fingerprint "
            f"{recorded_fp} -> {fingerprint}. Refusing to splice new content onto old "
            "progress; re-run with a fresh state directory/new import identity"
        )
    if meta.get("config_fingerprint_sha256") != config_fp:
        raise ImportFingerprintError(
            "the import configuration changed since the recorded run; refusing to resume"
        )
    if meta.get("staging_dir") != os.path.abspath(store_dir):
        raise ImportFingerprintError(
            "resume run targets a different staging store than the recorded run"
        )
    if meta.get("counts") != parsed.counts:
        raise ImportStateError("row classification no longer matches the recorded counts")
    return meta


def _reconcile_and_write(store: StagingStore, state: ImportState, meta: dict,
                         parsed: ParsedFile, config: ImportConfig,
                         batch_records: List[dict], *, kill_after_batch, pause) -> List[dict]:
    plans = parsed.batches
    confirmed = len(batch_records)
    if confirmed > len(plans):
        raise ImportStateError(
            f"recorded {confirmed} batches but only {len(plans)} are planned for this file"
        )
    # Reconcile a possible batch whose WAL frame landed but whose progress
    # record did not (a hard crash inside that window).
    if confirmed < len(plans):
        candidate = plans[confirmed]
        landed = [
            store.get(bytes.fromhex(entry["key_hex"])) == bytes.fromhex(entry["value_hex"])
            for entry in candidate["entries"]
        ]
        if all(landed):
            record = _batch_record(candidate, store.commit_number, reconciled=True)
            state.append_batch(record)
            batch_records.append(record)
            confirmed += 1
        elif any(landed):
            raise ImportResumeError(
                f"batch {candidate['batch_index']} is partially present in the staging "
                "store; cannot tell where the committed boundary is. Inspect the store "
                "and resume manually"
            )
        # else: nothing landed; normal writing resumes below.

    for plan in plans[confirmed:]:
        ops = [
            ("put", bytes.fromhex(entry["key_hex"]), bytes.fromhex(entry["value_hex"]))
            for entry in plan["entries"]
        ]
        commit = store.write_batch(ops)
        record = _batch_record(plan, commit, reconciled=False)
        state.append_batch(record)
        batch_records.append(record)
        meta["last_batch_index"] = record["batch_index"]
        meta["last_physical_line"] = plan["last_line"]
        meta["status"] = "running"
        state.write_meta(meta)
        if pause is not None:
            pause("import_batch_confirmed", {"batch_index": record["batch_index"],
                                             "commit": commit})
        if kill_after_batch is not None and record["batch_index"] == kill_after_batch:
            os.kill(os.getpid(), 9)
    return batch_records


def _batch_record(plan: dict, commit: int, reconciled: bool) -> dict:
    return {
        "batch_index": plan["batch_index"],
        "row_count": plan["row_count"],
        "first_line": plan["first_line"],
        "last_line": plan["last_line"],
        "physical_lines": plan["physical_lines"],
        "commit": commit,
        "reconciled": reconciled,
        "entries": plan["entries"],
    }


def read_status(state_dir: str) -> dict:
    """Inspect an import state directory without opening the staging store."""
    state = ImportState(state_dir)
    if not os.path.isdir(state.state_dir):
        raise ImportStateError(f"no import state at {state.state_dir}")
    meta = state.read_meta() if state.has_meta() else None
    batch_records = state.read_batches() if os.path.exists(state.batches_path) else []
    reports = {}
    for name in (PRECHECK_REPORT, RUN_REPORT):
        path = os.path.join(state.reports_dir, name)
        reports[name] = {"exists": os.path.exists(path),
                         "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0}
    quarantine = {
        "exists": os.path.exists(state.quarantine_path),
        "size_bytes": os.path.getsize(state.quarantine_path)
        if os.path.exists(state.quarantine_path) else 0,
    }
    if meta is None:
        return {
            "kind": "distagg-import-status",
            "state_dir": state.state_dir,
            "status": "precheck_only",
            "reports": reports,
            "quarantine": quarantine,
        }
    data_complete = meta.get("status") == "complete"
    report_complete = reports[RUN_REPORT]["exists"]
    if data_complete and report_complete:
        overall = "complete"
    elif data_complete and not report_complete:
        overall = "data_committed_report_missing"
    elif batch_records:
        overall = "interrupted_batches_confirmed"
    else:
        overall = "started_no_batch_confirmed"
    return {
        "kind": "distagg-import-status",
        "state_dir": state.state_dir,
        "status": overall,
        "meta_status": meta.get("status"),
        "import_id": meta.get("import_id"),
        "input": meta.get("input"),
        "counts": meta.get("counts"),
        "confirmed_batches": len(batch_records),
        "confirmed_rows": sum(record.get("row_count", 0) for record in batch_records),
        "last_batch_index": meta.get("last_batch_index"),
        "last_physical_line": meta.get("last_physical_line"),
        "staging": meta.get("staging"),
        "reports": reports,
        "quarantine": quarantine,
        "batch_records": batch_records,
    }
