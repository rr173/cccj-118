"""Command line interface for the offline workbook recalculation entry point.

Commands::

    distagg workbook recalc WORKBOOK.xlsx RECIPE.json OUTPUT.jsonl \
        [--mode strict|annotate] [--cell-budget N] [--max-formula-cells N]
    distagg workbook check  WORKBOOK.xlsx RECIPE.json \
        [--cell-budget N] [--max-formula-cells N]

``recalc`` writes one JSON object per selected data row.  ``check`` writes a
JSON report listing the recipe's selections, the workbook's sheets and how
many selected and reached cells are formulas, constants or blank.  Both exit
0 on success and 1 on a job-level failure; the failure object is JSON on
stdout with a one-line summary on stderr.  The output path must differ from
both inputs.
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import (
    MODE_ANNOTATE,
    MODE_STRICT,
    RecalcConfig,
    check_workbook,
    run_recalc_job,
)
from .errors import WorkbookError


def _print(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _config(args) -> RecalcConfig:
    return RecalcConfig(
        mode=getattr(args, "mode", MODE_STRICT),
        cell_budget=args.cell_budget,
        max_formula_cells=args.max_formula_cells,
    )


def _cmd_recalc(args) -> int:
    try:
        stats = run_recalc_job(
            args.workbook, args.recipe, args.output, _config(args)
        )
    except WorkbookError as exc:
        payload = exc.to_dict()
        payload["complete"] = False
        _print(payload)
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except OSError as exc:
        payload = {"error": "workbook_io_error", "message": str(exc),
                   "complete": False}
        _print(payload)
        print(f"error: workbook_io_error: {exc}", file=sys.stderr)
        return 1
    _print(stats.as_dict())
    if args.mode == MODE_ANNOTATE and stats.error_fields:
        print(
            f"annotate: delivered {stats.rows_written} rows with "
            f"{stats.error_fields} error field(s)",
            file=sys.stderr,
        )
    return 0


def _cmd_check(args) -> int:
    try:
        stats = check_workbook(args.workbook, args.recipe, _config(args))
    except WorkbookError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except OSError as exc:
        _print({"error": "workbook_io_error", "message": str(exc)})
        print(f"error: workbook_io_error: {exc}", file=sys.stderr)
        return 1
    _print(stats.as_dict())
    return 0


def _common(parser) -> None:
    parser.add_argument("workbook", help="XLSX workbook to read")
    parser.add_argument("recipe", help="recipe JSON (sheets, rows, fields)")
    parser.add_argument(
        "--cell-budget", type=int, default=None,
        help="maximum charged cell accesses (references + range visits); "
             "exceeding it fails the job",
    )
    parser.add_argument(
        "--max-formula-cells", type=int, default=None,
        help="maximum number of distinct formula cells the job may parse",
    )


def _add_recalc(workbook_sub) -> None:
    recalc = workbook_sub.add_parser(
        "recalc",
        help="recompute the recipe's cells and write JSONL handover rows",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common(recalc)
    recalc.add_argument("output", help="JSONL output path (must differ from inputs)")
    recalc.add_argument(
        "--mode", choices=(MODE_STRICT, MODE_ANNOTATE), default=MODE_STRICT,
        help="strict stops at the first error field and produces no file; "
             "annotate delivers every row with typed errors (default: strict)",
    )
    recalc.set_defaults(func=_cmd_recalc)


def _add_check(workbook_sub) -> None:
    check = workbook_sub.add_parser(
        "check",
        help="report ranges, sheets and actually touched formula/constant counts",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common(check)
    check.set_defaults(func=_cmd_check)


def register(sub) -> None:
    """Attach the ``workbook`` command group to the distagg argument parser."""
    parser = sub.add_parser(
        "workbook",
        help="offline XLSX recalculation and verifiable row handover",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    workbook_sub = parser.add_subparsers(dest="workbook_command", required=True)
    _add_recalc(workbook_sub)
    _add_check(workbook_sub)


def main(argv=None) -> int:
    """Standalone entry used by ``python -m distagg.workbook``."""
    parser = argparse.ArgumentParser(prog="distagg workbook", description=__doc__)
    workbook_sub = parser.add_subparsers(dest="workbook_command", required=True)
    _add_recalc(workbook_sub)
    _add_check(workbook_sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WorkbookError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except OSError as exc:
        _print({"error": "workbook_io_error", "message": str(exc)})
        print(f"error: workbook_io_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
