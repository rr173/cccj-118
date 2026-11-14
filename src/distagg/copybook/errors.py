"""Exception types used by the distagg copybook decoder.

All failures raised by the copybook entry point are subclasses of
:class:`CopybookError`.  The ``code`` attribute is a stable machine-readable
string, mirroring the convention used by the aggregation engine and the
staging store.

Compile-time failures (copybook syntax, unsupported constructs, layout
semantics, selector configuration) always name the copybook line and, when
known, the field being declared — unsupported constructs are reported, never
silently ignored.  Decode-time failures carry the 1-based record number, the
file byte offset of the record's RDW, the field path and a human-readable
reason.
"""


class CopybookError(Exception):
    code = "copybook_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class CopybookSyntaxError(CopybookError):
    """The copybook text cannot be tokenised or parsed as a declaration."""

    code = "copybook_syntax"

    def __init__(self, message: str, line: int = None, field: str = None):
        self.line = line
        self.field = field
        prefix = []
        if line is not None:
            prefix.append(f"line {line}")
        if field:
            prefix.append(f"field {field}")
        if prefix:
            message = f"{', '.join(prefix)}: {message}"
        super().__init__(message)

    def to_dict(self) -> dict:
        d = super().to_dict()
        if self.line is not None:
            d["line"] = self.line
        if self.field is not None:
            d["field"] = self.field
        return d


class UnsupportedError(CopybookSyntaxError):
    """A construct outside the supported subset was used.

    Raised for things like level 66/77/88 items, VALUE clauses, edited PICTURE
    strings, signed DISPLAY numerics, OCCURS ... KEY, REDEFINES chains and so
    on.  The copybook line and field are always reported.
    """

    code = "copybook_unsupported"


class LayoutError(CopybookError):
    """The declarations parse but do not form a valid record layout."""

    code = "layout_error"

    def __init__(self, message: str, line: int = None, field: str = None):
        self.line = line
        self.field = field
        prefix = []
        if line is not None:
            prefix.append(f"line {line}")
        if field:
            prefix.append(f"field {field}")
        if prefix:
            message = f"{', '.join(prefix)}: {message}"
        super().__init__(message)

    def to_dict(self) -> dict:
        d = super().to_dict()
        if self.line is not None:
            d["line"] = self.line
        if self.field is not None:
            d["field"] = self.field
        return d


class SelectorConfigError(CopybookError):
    """The branch-selection configuration is missing, unknown or malformed."""

    code = "selector_config"


class DecodeError(CopybookError):
    """One record of the input file could not be decoded.

    Attributes
    ----------
    record_no
        1-based number of the failing record (its RDW counts as part of it).
    offset
        File byte offset of the first byte of the record's RDW.
    field_path
        COBOL-style path of the field being decoded when the failure was
        detected, e.g. ``ORDER-RECORD.DETAILS[2].UNIT-PRICE`` (occurrence
        subscripts are 1-based, matching COBOL).  ``None`` when the failure
        is not tied to a field (bad RDW, trailing bytes).
    reason
        Human-readable cause.
    """

    code = "decode_error"

    def __init__(self, reason: str, record_no: int = None, offset: int = None,
                 field_path: str = None):
        self.reason = reason
        self.record_no = record_no
        self.offset = offset
        self.field_path = field_path
        parts = []
        if record_no is not None:
            parts.append(f"record {record_no}")
        if offset is not None:
            parts.append(f"file offset {offset}")
        if field_path:
            parts.append(f"field {field_path}")
        message = f"{', '.join(parts)}: {reason}" if parts else reason
        super().__init__(message)

    def with_record(self, record_no: int, offset: int) -> "DecodeError":
        """Return a copy of this error annotated with the record context."""
        return DecodeError(
            self.reason,
            record_no=record_no,
            offset=offset,
            field_path=self.field_path,
        )

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["reason"] = self.reason
        if self.record_no is not None:
            d["record_no"] = self.record_no
        if self.offset is not None:
            d["offset"] = self.offset
        if self.field_path is not None:
            d["field_path"] = self.field_path
        return d
