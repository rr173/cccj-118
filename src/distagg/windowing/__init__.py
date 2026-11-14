"""Offline row-level window processing over JSONL transaction streams.

The entry point is :func:`run_window_job`: it consumes a UTF-8 JSONL stream
plus a window declaration and writes a new JSONL stream with the same row
count and source order, each row carrying a ``computed`` object (ranking,
lag/lead references and per-window metrics).

SQLite is used only as a disk-backed cache, index and sorter.  Window member
selection and every aggregate are computed by code in this package; no SQL
``OVER`` / window extension and no DataFrame window function is used.
"""

from .declaration import WindowDeclaration, WindowSpec, load_declaration
from .engine import WindowConfig, WindowJobStats, run_window_job
from .errors import WindowError, WindowDeclarationError, WindowInputError

__all__ = [
    "WindowConfig",
    "WindowDeclaration",
    "WindowDeclarationError",
    "WindowError",
    "WindowInputError",
    "WindowJobStats",
    "WindowSpec",
    "load_declaration",
    "run_window_job",
]
