"""distagg.staging — a small LSM storage engine used as a disk staging store.

This package is independent of the exposure/tag aggregation database: it
stores arbitrary ``bytes`` keys and values (empty strings included) with
put/delete/batch/scan semantics, multi-version snapshots, an on-disk WAL,
sorted segments with a sparse index, and explicit flush/compact.  See
``docs/STAGED_STORE.md`` for the file formats and the fault model.

Quick start
-----------

>>> from distagg.staging import StagingStore
>>> store = StagingStore.open("/tmp/staging")
>>> commit = store.put(b"key", b"value")
>>> store.get(b"key")
b'value'
>>> snap = store.snapshot()
>>> _ = store.put(b"key", b"newer")
>>> snap.get(b"key"), store.get(b"key")
(b'value', b'newer')
>>> snap.close()
>>> store.close()
"""

from .errors import (
    ClosedError,
    CorruptionError,
    InvalidOperation,
    ReadOnlyError,
    StagingError,
    StoreLockedError,
)
from .importer import (
    ImportConfigError,
    ImportFingerprintError,
    ImportHeaderError,
    ImportPathError,
    ImportReportError,
    ImportResumeError,
    ImportStateError,
    ImportConfig,
    classify_file,
    default_import_id,
    load_config,
    parse_config,
    precheck,
    read_status,
    resolve_input_path,
    run_import,
)
from .store import (
    PAUSE_POINTS,
    Batch,
    ScanIterator,
    Snapshot,
    StagingStore,
)

__all__ = [
    "StagingStore",
    "Batch",
    "Snapshot",
    "ScanIterator",
    "PAUSE_POINTS",
    "StagingError",
    "CorruptionError",
    "StoreLockedError",
    "ReadOnlyError",
    "ClosedError",
    "InvalidOperation",
    # TSV correction importer
    "ImportConfig",
    "ImportConfigError",
    "ImportFingerprintError",
    "ImportHeaderError",
    "ImportPathError",
    "ImportReportError",
    "ImportResumeError",
    "ImportStateError",
    "load_config",
    "parse_config",
    "classify_file",
    "resolve_input_path",
    "default_import_id",
    "precheck",
    "run_import",
    "read_status",
]
