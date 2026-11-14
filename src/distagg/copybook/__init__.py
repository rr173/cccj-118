"""distagg.copybook — copybook-driven offline decoding of mainframe exports.

This subpackage compiles a restricted COBOL copybook subset into a reusable
record layout and decodes RDW-framed EBCDIC files into structured records
for downstream pipelines.  It is self-contained: it does not reuse the
supplier TSV importer's numbering, quarantine or resume protocols, and it
never touches the aggregation database.

Public API
----------

``compile_copybook(source, selectors=None)``
    Parse and validate copybook text; returns a reusable
    :class:`CompiledLayout`.  ``selectors`` maps each REDEFINES group path
    to ``{"using": field_path, "map": {value: branch_path}}``.
``load_selector_config(path)``
    Read that mapping from a JSON file.
``layout.describe()``
    Layout report (paths, types, spans, occurs/redefines relations) used by
    the ``distagg copybook check`` command.
``decode_record(layout, payload)``
    Decode one record payload (bytes after the RDW) into a nested dict.
``iter_records(layout, fileobj)`` / ``iter_file(layout, path)``
    Stream :class:`DecodedRecord` objects one at a time from an RDW-framed
    source; memory stays proportional to a single record.

See ``docs/COPYBOOK.md`` for the supported subset and the exact semantics.
"""

from .compiler import CompiledLayout, compile_copybook
from .config import load_selector_config
from .decoder import (
    DecodedRecord,
    decode_record,
    format_decimal,
    iter_file,
    iter_records,
)
from .errors import (
    CopybookError,
    CopybookSyntaxError,
    DecodeError,
    LayoutError,
    SelectorConfigError,
    UnsupportedError,
)
from .parser import parse_copybook

__all__ = [
    "compile_copybook",
    "parse_copybook",
    "load_selector_config",
    "CompiledLayout",
    "decode_record",
    "iter_records",
    "iter_file",
    "DecodedRecord",
    "format_decimal",
    "CopybookError",
    "CopybookSyntaxError",
    "UnsupportedError",
    "LayoutError",
    "SelectorConfigError",
    "DecodeError",
]
