"""The record envelope that travels along every edge.

A record carries everything needed to trace an output row back to its origin:

``source``
    Name of the source node that read the physical line.
``line``
    Physical 1-based line number inside that source's file.
``expansion_path``
    Empty for an original record.  ``explode`` appends the 0-based index of
    each emitted child; nesting ``explode`` calls keeps appending.
``row``
    The JSON object being processed.  Operators never mutate it in place;
    they build a new object (or, for ``explode``, a shallow copy with the
    selected field replaced by the child value).

The tuple ``(source, line, expansion_path)`` is the *source identity*.  It is
never derived from business keys, and ``merge`` never de-duplicates records:
the same physical line arriving over two paths is delivered twice.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


class Record:
    __slots__ = ("source", "line", "expansion_path", "row")

    def __init__(self, source: str, line: int, expansion_path: Tuple[int, ...],
                 row: Dict[str, Any]):
        self.source = source
        self.line = line
        self.expansion_path = expansion_path
        self.row = row

    def identity(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "line": self.line,
            "expansion_path": list(self.expansion_path),
        }

    def child(self, index: int, row: Dict[str, Any]) -> "Record":
        """A new record for an ``explode`` child (path + index)."""
        return Record(self.source, self.line, self.expansion_path + (index,), row)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"Record(source={self.source!r}, line={self.line}, "
                f"path={self.expansion_path!r})")
