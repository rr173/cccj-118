"""Command line interface for the local TSV correction importer.

Commands (under ``distagg staging import ...``)::

    distagg staging import precheck CONFIG TSV STATE_DIR [options]
    distagg staging import run       CONFIG TSV STORE_DIR STATE_DIR [options]
    distagg staging import status    STATE_DIR

The importer only adds this command group; the existing ``staging``
subcommands and the aggregation CLI are unchanged.  All output is JSON on
stdout; errors are JSON on stdout plus a one-line summary on stderr and
exit code 1.  Precheck never opens the staging store for writing.
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import StagingError
from .importer import (
    DEFAULT_MAX_SAMPLES,
    load_config,
    precheck,
    read_status,
    run_import,
)


def _print(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _guard(fn):
    """Turn importer failures into JSON + exit code 1 (no raw traceback)."""

    def wrapped(args) -> int:
        try:
            return fn(args)
        except StagingError as exc:
            _print(exc.to_dict())
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            return 1

    return wrapped


def _common_input_args(parser, *, store_optional: bool) -> None:
    parser.add_argument("config", help="import configuration JSON")
    parser.add_argument("tsv", help="input UTF-8 TSV file")
    if not store_optional:
        parser.add_argument("store_dir", help="staging store directory to write into")
    parser.add_argument("state_dir", help="importer progress/report directory")
    parser.add_argument(
        "--root", default=None,
        help="directory the TSV path must stay inside (default: current directory)",
    )
    parser.add_argument(
        "--allow-symlinks", action="store_true",
        help="accept a symlinked input after explicit validation (refused by default)",
    )
    parser.add_argument(
        "--import-id", default=None,
        help="explicit import identity (default: stable id derived from the relative path)",
    )
    parser.add_argument(
        "--max-samples", type=int, default=DEFAULT_MAX_SAMPLES,
        help=f"bounded number of sample locations per class (default {DEFAULT_MAX_SAMPLES})",
    )


def _cmd_precheck(args) -> int:
    config = load_config(args.config)
    report = precheck(
        config, args.tsv, args.state_dir,
        root=args.root, allow_symlinks=args.allow_symlinks,
        import_id=args.import_id, max_samples=args.max_samples,
    )
    _print(report)
    return 0


def _cmd_run(args) -> int:
    config = load_config(args.config)
    report = run_import(
        config, args.tsv, args.store_dir, args.state_dir,
        root=args.root, allow_symlinks=args.allow_symlinks,
        import_id=args.import_id, max_samples=args.max_samples,
        kill_after_batch=args.kill_after_batch,
        fail_report_write=args.fail_report_write,
    )
    _print(report)
    return 0


def _cmd_status(args) -> int:
    _print(read_status(args.state_dir))
    return 0


def _register_commands(import_sub) -> None:
    """Attach precheck/run/status to an existing ``import`` subparser."""
    p = import_sub.add_parser("precheck", help="classify a TSV file without writing the store")
    _common_input_args(p, store_optional=True)
    p.set_defaults(func=_guard(_cmd_precheck))

    p = import_sub.add_parser("run", help="import acceptable rows, batching into the store")
    _common_input_args(p, store_optional=False)
    p.add_argument(
        "--kill-after-batch", type=int, default=None,
        help="SIGKILL this process after the Nth batch's progress is durable (test hook)",
    )
    p.add_argument(
        "--fail-report-write", action="store_true",
        help="simulate a report write failure after the data batches are committed",
    )
    p.set_defaults(func=_guard(_cmd_run))

    p = import_sub.add_parser("status", help="inspect progress, batches and reports of an import")
    p.add_argument("state_dir")
    p.set_defaults(func=_guard(_cmd_status))


def register(sub) -> None:
    """Attach the ``staging import`` command group."""
    parser = sub.add_parser(
        "import",
        help="batch-import supplier correction TSV files into the staging store",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    import_sub = parser.add_subparsers(dest="import_command", required=True)
    _register_commands(import_sub)


def main(argv=None) -> int:
    """Standalone entry used by ``python -m distagg.staging.import_cli``."""
    parser = argparse.ArgumentParser(prog="distagg staging import", description=__doc__)
    import_sub = parser.add_subparsers(dest="import_command", required=True)
    _register_commands(import_sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except StagingError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
