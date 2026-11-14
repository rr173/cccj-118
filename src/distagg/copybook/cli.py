"""Command line interface for the copybook decoder (``distagg copybook ...``).

Commands::

    distagg copybook check  COPYBOOK [--select SELECTORS.json]
    distagg copybook decode COPYBOOK DATAFILE [--select SELECTORS.json] [--limit N]

``check`` compiles the copybook and prints the layout report as JSON on
stdout: every field with its full path, kind, PICTURE/USAGE, fixed or
variable span, OCCURS and REDEFINES relations, and its static offset where
one exists — offsets behind variable-length items are reported as
``"offset_kind": "dynamic"``, never disguised as constants.

``decode`` streams the data file record by record and prints one JSON object
per line (``{"record": n, "offset": bytes, "length": bytes, "data": {...}}``).
Groups stay objects, OCCURS items stay arrays, FILLER consumes space without
appearing, and a REDEFINES group contributes only the branch chosen by the
selection config.  On a bad record the already-printed lines stay printed,
the error object (record number, file offset, field path, reason) goes to
stderr and the exit code is 1 — a failed file is never reported as fully
decoded.  Compile/config errors are JSON on stdout plus a one-line summary
on stderr, exit code 1, matching the other distagg commands.
"""

from __future__ import annotations

import argparse
import json
import sys

from .compiler import compile_copybook
from .config import load_selector_config
from .decoder import iter_records
from .errors import CopybookError, DecodeError


def _print(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _guard(fn):
    """Turn copybook failures into JSON + exit code 1 (no raw traceback)."""

    def wrapped(args) -> int:
        try:
            return fn(args)
        except CopybookError as exc:
            _print(exc.to_dict())
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            return 1

    return wrapped


def _load_layout(args):
    try:
        with open(args.copybook, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        raise CopybookError(f"cannot read copybook {args.copybook!r}: {exc}")
    selectors = load_selector_config(args.select) if args.select else None
    return compile_copybook(source, selectors=selectors)


def _cmd_check(args) -> int:
    layout = _load_layout(args)
    _print(layout.describe())
    return 0


def _cmd_decode(args) -> int:
    layout = _load_layout(args)
    written = 0
    try:
        with open(args.datafile, "rb") as handle:
            for record in iter_records(layout, handle):
                if args.limit is not None and written >= args.limit:
                    break
                sys.stdout.write(
                    json.dumps(record.to_dict(), ensure_ascii=False,
                               sort_keys=True)
                    + "\n"
                )
                written += 1
            sys.stdout.flush()
    except DecodeError as exc:
        sys.stdout.flush()
        json.dump(exc.to_dict(), sys.stderr, ensure_ascii=False, indent=2,
                  sort_keys=True)
        sys.stderr.write("\n")
        return 1
    except OSError as exc:
        _print(CopybookError(
            f"cannot read data file {args.datafile!r}: {exc}").to_dict())
        print(f"error: cannot read data file {args.datafile!r}: {exc}",
              file=sys.stderr)
        return 1
    return 0


def _register_commands(copybook_sub) -> None:
    p = copybook_sub.add_parser(
        "check", help="compile the copybook and print the layout report")
    p.add_argument("copybook", help="copybook source file (restricted subset)")
    p.add_argument("--select", default=None,
                   help="JSON branch-selection config for REDEFINES groups")
    p.set_defaults(func=_guard(_cmd_check))

    p = copybook_sub.add_parser(
        "decode", help="decode an RDW-framed mainframe export to JSON lines")
    p.add_argument("copybook", help="copybook source file (restricted subset)")
    p.add_argument("datafile", help="RDW-framed input file")
    p.add_argument("--select", default=None,
                   help="JSON branch-selection config for REDEFINES groups")
    p.add_argument("--limit", type=int, default=None,
                   help="decode at most N records")
    p.set_defaults(func=_guard(_cmd_decode))


def register(sub) -> None:
    """Attach the ``copybook`` command group to the distagg argument parser."""
    parser = sub.add_parser(
        "copybook",
        help="copybook-driven offline decoding of mainframe record exports",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    copybook_sub = parser.add_subparsers(dest="copybook_command", required=True)
    _register_commands(copybook_sub)


def main(argv=None) -> int:
    """Standalone entry used by ``python -m distagg.copybook.cli``."""
    parser = argparse.ArgumentParser(prog="distagg copybook", description=__doc__)
    copybook_sub = parser.add_subparsers(dest="copybook_command", required=True)
    _register_commands(copybook_sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CopybookError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
