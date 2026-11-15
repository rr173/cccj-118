"""Command line interface for controlled-vocabulary annotation.

Commands::

    distagg lexicon compile DICTIONARY.jsonl LIBRARY.sqlite [options]
    distagg lexicon check   LIBRARY.sqlite [--limit N]
    distagg lexicon scan    LIBRARY.sqlite INPUT.jsonl OUTPUT.jsonl [--mode all|resolved] [options]

``compile`` builds the read-only matching library (never overwriting an
existing target), ``check`` prints terms, aliases, automaton counts and
the matching conventions, and ``scan`` annotates a remark JSONL file,
publishing the result atomically.  All output is JSON on stdout; errors
are JSON on stdout plus a one-line summary on stderr, exit code 1.
"""

from __future__ import annotations

import argparse
import json
import sys

from .builder import CompileConfig, compile_dictionary
from .errors import LexiconError
from .report import library_report
from .scanner import MODES, ScanConfig, scan_remarks


def _print(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _guard(fn):
    """Turn lexicon failures into JSON + exit code 1 (no raw traceback)."""

    def wrapped(args) -> int:
        try:
            return fn(args)
        except LexiconError as exc:
            _print(exc.to_dict())
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            payload = {"error": "lexicon_io_error", "message": str(exc)}
            _print(payload)
            print(f"error: lexicon_io_error: {exc}", file=sys.stderr)
            return 1

    return wrapped


def _cmd_compile(args) -> int:
    config = CompileConfig(
        sqlite_cache_size_kib=args.sqlite_cache_size_kib,
        max_record_bytes=args.max_record_bytes,
        batch_size=args.batch_size,
        transition_cache_entries=args.transition_cache_entries,
        state_cache_entries=args.state_cache_entries,
        work_dir=args.work_dir,
    )
    stats = compile_dictionary(args.dictionary, args.library, config)
    _print(stats.to_dict())
    return 0


def _cmd_check(args) -> int:
    report = library_report(
        args.library, limit=args.limit,
        sqlite_cache_size_kib=args.sqlite_cache_size_kib)
    _print(report)
    return 0


def _cmd_scan(args) -> int:
    config = ScanConfig(
        sqlite_cache_size_kib=args.sqlite_cache_size_kib,
        state_cache_entries=args.state_cache_entries,
        transition_cache_entries=args.transition_cache_entries,
        output_cache_entries=args.output_cache_entries,
        max_remark_bytes=args.max_remark_bytes,
        max_candidates_per_record=args.max_candidates_per_record,
        work_dir=args.work_dir,
    )
    stats = scan_remarks(args.library, args.input, args.output,
                         mode=args.mode, config=config)
    _print(stats.to_dict())
    return 0


def _add_cache_args(parser, *, compile_: bool) -> None:
    parser.add_argument(
        "--sqlite-cache-size-kib", type=int, default=8192,
        help="SQLite PRAGMA cache_size in KiB (default: 8192)")
    parser.add_argument(
        "--state-cache-entries", type=int,
        default=65536 if compile_ else 4096,
        help="LRU capacity for per-state info (default: %(default)s)")
    parser.add_argument(
        "--transition-cache-entries", type=int,
        default=65536 if compile_ else 8192,
        help="LRU capacity for (state, char) transitions "
             "(default: %(default)s)")


def _register_commands(lexicon_sub) -> None:
    p = lexicon_sub.add_parser(
        "compile", help="compile a dictionary JSONL into a read-only library")
    p.add_argument("dictionary", help="dictionary JSONL (term_id, priority, aliases)")
    p.add_argument("library", help="target library path (must not exist)")
    p.add_argument(
        "--max-record-bytes", type=int, default=1024 * 1024,
        help="hard inclusive byte limit for one dictionary line "
             "(default: 1 MiB)")
    p.add_argument(
        "--batch-size", type=int, default=8192,
        help="max rows per Python insert/update batch (default: 8192)")
    _add_cache_args(p, compile_=True)
    p.add_argument(
        "--work-dir", default=None,
        help="directory for the private temporary workspace (default: "
             "next to the library)")
    p.set_defaults(func=_guard(_cmd_compile))

    p = lexicon_sub.add_parser(
        "check", help="print terms, aliases, automaton counts and conventions")
    p.add_argument("library", help="compiled library path")
    p.add_argument(
        "--limit", type=int, default=None,
        help="show at most N terms (counts always cover the whole library)")
    p.add_argument(
        "--sqlite-cache-size-kib", type=int, default=8192,
        help="SQLite PRAGMA cache_size in KiB (default: 8192)")
    p.set_defaults(func=_guard(_cmd_check))

    p = lexicon_sub.add_parser(
        "scan", help="annotate a remark JSONL file against the library")
    p.add_argument("library", help="compiled library path")
    p.add_argument("input", help="remark JSONL (UTF-8, one object per line)")
    p.add_argument("output", help="output JSONL path (must differ from input)")
    p.add_argument(
        "--mode", choices=MODES, default="resolved",
        help="annotation mode: all candidates or the resolved "
             "non-overlapping subset (default: resolved)")
    _add_cache_args(p, compile_=False)
    p.add_argument(
        "--output-cache-entries", type=int, default=2048,
        help="LRU capacity for per-state output rows (default: 2048)")
    p.add_argument(
        "--max-remark-bytes", type=int, default=4 * 1024 * 1024,
        help="hard inclusive byte limit for one remark line "
             "(default: 4 MiB)")
    p.add_argument(
        "--max-candidates-per-record", type=int, default=1_000_000,
        help="hard limit on raw candidates per remark; exceeding it "
             "fails the run (default: 1000000)")
    p.add_argument(
        "--work-dir", default=None,
        help="directory for the private temporary workspace (default: "
             "next to the output)")
    p.set_defaults(func=_guard(_cmd_scan))


def register(sub) -> None:
    """Attach the ``lexicon`` command group to the distagg argument parser."""
    parser = sub.add_parser(
        "lexicon",
        help="controlled-vocabulary annotation of supplier remark text",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    lexicon_sub = parser.add_subparsers(dest="lexicon_command", required=True)
    _register_commands(lexicon_sub)


def main(argv=None) -> int:
    """Standalone entry used by ``python -m distagg.lexicon``."""
    parser = argparse.ArgumentParser(prog="distagg lexicon", description=__doc__)
    lexicon_sub = parser.add_subparsers(dest="lexicon_command", required=True)
    _register_commands(lexicon_sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LexiconError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        payload = {"error": "lexicon_io_error", "message": str(exc)}
        _print(payload)
        print(f"error: lexicon_io_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
