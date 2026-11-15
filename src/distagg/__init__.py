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
from .flow import (
    FlowConfig,
    FlowError,
    FlowFailure,
    FlowGraph,
    FlowRun,
    GraphError,
    Record,
    RunSummary,
    load_graph,
    start_run,
)
from .workbook import (
    RecalcConfig,
    RecalcStats,
    Recipe,
    RecipeError,
    WorkbookBudgetExceeded,
    WorkbookCycle,
    WorkbookError,
    WorkbookFormulaError,
    WorkbookPackageError,
    WorkbookStrictStop,
    WorkbookUnsupported,
    check_workbook,
    load_recipe,
    run_recalc_job,
)
from .windowing import (
    WindowConfig,
    WindowDeclaration,
    WindowDeclarationError,
    WindowError,
    WindowInputError,
    WindowJobStats,
    WindowSpec,
    load_declaration,
    run_window_job,
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
    "WindowConfig",
    "WindowDeclaration",
    "WindowDeclarationError",
    "WindowError",
    "WindowInputError",
    "WindowJobStats",
    "WindowSpec",
    "load_declaration",
    "run_window_job",
    # local asynchronous flow executor
    "FlowConfig",
    "FlowError",
    "FlowFailure",
    "FlowGraph",
    "FlowRun",
    "GraphError",
    "Record",
    "RunSummary",
    "load_graph",
    "start_run",
    # offline XLSX recalculation handover
    "RecalcConfig",
    "RecalcStats",
    "Recipe",
    "RecipeError",
    "WorkbookBudgetExceeded",
    "WorkbookCycle",
    "WorkbookError",
    "WorkbookFormulaError",
    "WorkbookPackageError",
    "WorkbookStrictStop",
    "WorkbookUnsupported",
    "check_workbook",
    "load_recipe",
    "run_recalc_job",
]

__version__ = "0.1.0"
