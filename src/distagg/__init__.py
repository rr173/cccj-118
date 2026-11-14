"""distagg - incremental publisher x tag cost aggregation over SQLite.

Public API
----------
``submit_transaction`` / ``submit_file``
    Apply one transaction file (or decoded object) and return the receipt.
``query_summary``
    Read the current derived ``publisher x tag`` table.
``query_base``
    Read base multiplicities.
``get_receipt``
    Read a stored receipt by seq.
``check_database``
    O(n) diagnostic that recomputes derived state from base tables.
"""

from .api import (
    SubmitResult,
    check_database,
    get_receipt,
    query_base,
    query_summary,
    submit_file,
    submit_transaction,
)
from .errors import (
    DatabaseError,
    DistaggError,
    InvalidTransaction,
    NegativeBaseWeight,
    SeqConflict,
    SequenceGap,
)

__all__ = [
    "SubmitResult",
    "submit_transaction",
    "submit_file",
    "query_summary",
    "query_base",
    "get_receipt",
    "check_database",
    "DistaggError",
    "InvalidTransaction",
    "NegativeBaseWeight",
    "SequenceGap",
    "SeqConflict",
    "DatabaseError",
]

__version__ = "0.1.0"
