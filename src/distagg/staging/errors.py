"""Exception types used by the distagg staging store.

All failures raised by the staging store are subclasses of :class:`StagingError`.
The ``code`` attribute is a stable machine-readable string, also used in the
JSON error objects printed by the command line interface.
"""


class StagingError(Exception):
    code = "staging_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class CorruptionError(StagingError):
    """A file that is part of the store is malformed.

    Raised when the write-ahead log contains a complete but invalid frame
    (bad magic, bad header CRC, bad payload CRC), when a segment file fails
    its magic/CRC checks, when the manifest or CURRENT file is damaged, or
    when files referenced by the manifest are missing.  The store refuses to
    open (or the read fails) rather than silently rebuilding an empty store.
    """

    code = "corruption"

    def __init__(self, message: str, path: str = None, offset: int = None):
        super().__init__(message)
        self.path = path
        self.offset = offset

    def to_dict(self) -> dict:
        d = super().to_dict()
        if self.path is not None:
            d["path"] = self.path
        if self.offset is not None:
            d["offset"] = self.offset
        return d


class StoreLockedError(StagingError):
    """Another process (or this one) already holds the store's write lock."""

    code = "store_locked"


class ReadOnlyError(StagingError):
    """A mutating operation was attempted on a read-only handle."""

    code = "read_only"


class ClosedError(StagingError):
    """A closed store, snapshot or iterator was used."""

    code = "closed"


class InvalidOperation(StagingError):
    """An operation argument is malformed (wrong types, bad hex, bad bounds)."""

    code = "invalid_operation"
