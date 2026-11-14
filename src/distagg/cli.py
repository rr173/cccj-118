"""Command line interface.

Examples
--------
    distagg submit  analytics.db txn-0001.json
    distagg summary analytics.db [--publisher P] [--tag T]
    distagg base    analytics.db
    distagg receipt analytics.db 7
    distagg receipts analytics.db
    distagg check   analytics.db
    distagg plans   analytics.db
    distagg explain analytics.db
    distagg demo    analytics.db

All output is JSON on stdout; diagnostics go to stderr.  Exit code 0 for a
committed or replayed transaction, 1 for a rejected transaction (the JSON on
stdout still describes the error, and stderr has a one-line summary).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import api
from .db import STATEMENTS
from .errors import DistaggError


def _print(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_submit(args) -> int:
    pause_json = None
    if args.pause_json:
        pause_json = args.pause_json
    if args.pause_points.strip() == "all":
        active_points = {"before_commit", "after_commit"}
    else:
        active_points = {
            point.strip() for point in args.pause_points.split(",") if point.strip()
        }
        unknown = active_points - {"before_commit", "after_commit"}
        if unknown:
            parser_error = ", ".join(sorted(unknown))
            raise SystemExit(f"unknown pause point(s): {parser_error}")

    def pause(point: str, info: dict) -> None:
        if point not in active_points:
            return
        if pause_json:
            with open(pause_json, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "point": point,
                            "info": info,
                            "time": __import__("datetime").datetime.now(
                                __import__("datetime").timezone.utc
                            ).isoformat(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
        if args.pause_signal_file:
            with open(args.pause_signal_file, "w", encoding="utf-8") as handle:
                handle.write(f"{point} pid={os.getpid()}\n")
                handle.flush()
                os.fsync(handle.fileno())
        if args.pause_delay is not None:
            import time

            time.sleep(args.pause_delay)
        if args.pause_wait_file:
            # Wait until the watchdog removes the file.  Killing the process
            # while it waits leaves the transaction in exactly this state.
            import time

            while os.path.exists(args.pause_wait_file):
                time.sleep(0.02)
        if args.kill_at == point:
            # Deliberate hard kill of the real process, no cleanup, no commit.
            os.kill(os.getpid(), 9)

    try:
        result = api.submit_file(
            args.db,
            args.txn,
            timeout=args.timeout,
            pause=pause,
            pause_info={"pid": os.getpid(), "txn_file": os.path.abspath(args.txn)},
        )
    except DistaggError as exc:
        _print(exc.to_dict())
        print(f"rejected: {exc.code}: {exc}", file=sys.stderr)
        return 1
    _print(result.to_dict())
    return 0


def cmd_summary(args) -> int:
    _print(api.query_summary(args.db, publisher=args.publisher, tag=args.tag))
    return 0


def cmd_base(args) -> int:
    _print(api.query_base(args.db))
    return 0


def cmd_receipt(args) -> int:
    receipt = api.get_receipt(args.db, args.seq)
    if receipt is None:
        _print({"error": "not_found", "seq": args.seq})
        return 1
    _print(receipt)
    return 0


def cmd_receipts(args) -> int:
    _print(api.list_receipts(args.db))
    return 0


def cmd_check(args) -> int:
    result = api.check_database(args.db)
    _print(result)
    return 0 if result["ok"] else 1


def cmd_plans(args) -> int:
    _print(api.query_plans(args.db))
    return 0


def cmd_explain(args) -> int:
    """Print the index-access model used by submit (also stored in receipts)."""
    entries = []
    for op in sorted(STATEMENTS):
        index, sql, reads = STATEMENTS[op]
        entries.append(
            {
                "operation": op,
                "index_or_table": index,
                "representative_sql": sql,
                "counts_rows_read": reads,
            }
        )
    _print({"index_access": entries})
    return 0


def cmd_demo(args) -> int:
    from .demo import run_demo

    run_demo(args.db)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="distagg", description=__doc__)
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("submit", help="apply one transaction file")
    p.add_argument("db")
    p.add_argument("txn")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--pause-json", help="append pause events to this JSONL file")
    p.add_argument("--pause-signal-file", help="write the pause point to this file")
    p.add_argument("--pause-wait-file", help="wait until this file is removed")
    p.add_argument(
        "--pause-delay",
        type=float,
        default=None,
        help="sleep this many seconds at the first honoured pause point, then continue",
    )
    p.add_argument(
        "--pause-points",
        default="before_commit,after_commit",
        help="comma separated pause points that should be honoured",
    )
    p.add_argument(
        "--kill-at",
        choices=("before_commit", "after_commit"),
        help="hard-kill this process with SIGKILL at the given pause point",
    )
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("summary", help="print the current publisher x tag summary")
    p.add_argument("db")
    p.add_argument("--publisher")
    p.add_argument("--tag")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("base", help="print base multiplicities")
    p.add_argument("db")
    p.set_defaults(func=cmd_base)

    p = sub.add_parser("receipt", help="print one receipt")
    p.add_argument("db")
    p.add_argument("seq", type=int)
    p.set_defaults(func=cmd_receipt)

    p = sub.add_parser("receipts", help="list committed receipts")
    p.add_argument("db")
    p.set_defaults(func=cmd_receipts)

    p = sub.add_parser("check", help="recompute derived state from base (diagnostic)")
    p.add_argument("db")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("plans", help="EXPLAIN QUERY PLAN of hot statements")
    p.add_argument("db")
    p.set_defaults(func=cmd_plans)

    p = sub.add_parser("explain", help="print the documented index access model")
    p.add_argument("db", nargs="?")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("demo", help="run the packaged demo on a fresh database")
    p.add_argument("db")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DistaggError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
