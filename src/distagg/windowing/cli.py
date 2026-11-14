"""Command line interface for the offline window processor.

Command::

    distagg window run INPUT.jsonl OUTPUT.jsonl --declaration DECL.json \
        [--sqlite-cache-size-kib N] [--batch-size N] \
        [--spill-threshold N] [--max-row-bytes N] [--work-dir DIR]

The output file is created through a private temporary workspace and only
replaced after the whole job succeeds.  Errors are printed as one JSON object
on stdout with a one-line summary on stderr and a non-zero exit code.
"""

from __future__ import annotations

import argparse
import json
import sys

from .declaration import load_declaration
from .engine import WindowConfig, run_window_job
from .errors import WindowError


def _print(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _cmd_run(args) -> int:
    try:
        declaration = load_declaration(args.declaration)
        config = WindowConfig(
            sqlite_cache_size_kib=args.sqlite_cache_size_kib,
            batch_size=args.batch_size,
            spill_threshold=args.spill_threshold,
            max_row_bytes=args.max_row_bytes,
        )
        stats = run_window_job(
            args.input,
            args.output,
            declaration,
            config,
            work_dir=args.work_dir,
        )
    except WindowError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except OSError as exc:
        payload = {"error": "window_io_error", "message": str(exc)}
        _print(payload)
        print(f"error: window_io_error: {exc}", file=sys.stderr)
        return 1
    _print({
        "rows_read": stats.rows_read,
        "rows_written": stats.rows_written,
        "groups": stats.groups,
        "windows": stats.windows,
        "output_path": stats.output_path,
    })
    return 0


def _add_run_command(window_sub) -> None:
    run = window_sub.add_parser(
        "run",
        help="compute row-level window metrics and write a new JSONL file",
    )
    run.add_argument("input", help="input JSONL stream (UTF-8, one object per line)")
    run.add_argument("output", help="output JSONL path (must differ from input)")
    run.add_argument(
        "--declaration", required=True,
        help="window declaration JSON (partition_by, order_by, value, "
             "lag_lead_k, windows)",
    )
    run.add_argument(
        "--sqlite-cache-size-kib", type=int, default=8192,
        help="SQLite PRAGMA cache_size in KiB (default: 8192)",
    )
    run.add_argument(
        "--batch-size", type=int, default=1000,
        help="max rows per Python batch for inserts, scans and cache loads "
             "(default: 1000)",
    )
    run.add_argument(
        "--spill-threshold", type=int, default=512,
        help="in-memory entry limit per monotonic deque before SQLite spill "
             "(default: 512)",
    )
    run.add_argument(
        "--max-row-bytes", type=int, default=16 * 1024 * 1024,
        help="hard inclusive byte limit for one physical input line "
             "(default: 16 MiB)",
    )
    run.add_argument(
        "--work-dir", default=None,
        help="directory for the private temporary workspace (default: next "
             "to the output file)",
    )
    run.set_defaults(func=_cmd_run)


def register(sub) -> None:
    """Attach the ``window`` command group to the distagg argument parser."""
    parser = sub.add_parser(
        "window",
        help="offline row-level window processing over UTF-8 JSONL streams",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    window_sub = parser.add_subparsers(dest="window_command", required=True)
    _add_run_command(window_sub)


def main(argv=None) -> int:
    """Standalone entry used by ``python -m distagg.windowing``."""
    parser = argparse.ArgumentParser(prog="distagg window", description=__doc__)
    window_sub = parser.add_subparsers(dest="window_command", required=True)
    _add_run_command(window_sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WindowError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except OSError as exc:
        payload = {"error": "window_io_error", "message": str(exc)}
        _print(payload)
        print(f"error: window_io_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
