"""Access accounting for one committed transaction.

The receipts carry this accounting so that an operator can see *what a
transaction touched*: which creative join keys were looked up, how many index
rows were read, how many base rows were written and which groups changed.

It is deliberately cheap: ``rows`` is the number of rows returned by a query
(``calls`` counts the query itself, even when it returned zero rows).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict

from .db import STATEMENTS


class AccessLog:
    def __init__(self) -> None:
        self._reads: Dict[str, Dict[str, int]] = defaultdict(lambda: {"calls": 0, "rows": 0})
        self._writes: Dict[str, Dict[str, int]] = defaultdict(lambda: {"calls": 0, "rows": 0})

    def read(self, statement: str, rows: int) -> None:
        entry = self._reads[statement]
        entry["calls"] += 1
        entry["rows"] += int(rows)

    def write(self, statement: str, rows: int = 1) -> None:
        entry = self._writes[statement]
        entry["calls"] += 1
        entry["rows"] += int(rows)

    # -- reporting ------------------------------------------------------
    @property
    def read_calls(self) -> int:
        return sum(e["calls"] for e in self._reads.values())

    @property
    def rows_read(self) -> int:
        return sum(e["rows"] for e in self._reads.values())

    @property
    def write_calls(self) -> int:
        return sum(e["calls"] for e in self._writes.values())

    @property
    def rows_written(self) -> int:
        return sum(e["rows"] for e in self._writes.values())

    def reads_dict(self) -> dict:
        by_statement = {}
        for key in sorted(self._reads):
            entry = self._reads[key]
            index, sql, _ = STATEMENTS.get(key, (key, "", False))
            by_statement[key] = {
                "index": index,
                "representative_sql": sql,
                "calls": entry["calls"],
                "rows": entry["rows"],
            }
        return {
            "calls": self.read_calls,
            "rows": self.rows_read,
            "by_statement": by_statement,
        }

    def writes_dict(self) -> dict:
        by_statement = {}
        for key in sorted(self._writes):
            entry = self._writes[key]
            index, _, _ = STATEMENTS.get(key, (key, "", False))
            by_statement[key] = {
                "table_or_index": index,
                "calls": entry["calls"],
                "rows": entry["rows"],
            }
        return {
            "calls": self.write_calls,
            "rows": self.rows_written,
            "by_statement": by_statement,
        }

    def as_dict(self) -> dict:
        return {
            "reads": self.reads_dict(),
            "writes": self.writes_dict(),
            "scope_note": (
                "All reads are point/range lookups on a creative, a "
                "(publisher, tag) group, or a group support key. No "
                "full-table scan or whole-group re-aggregation is performed "
                "by a submit."
            ),
        }
