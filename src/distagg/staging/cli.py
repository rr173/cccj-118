"""Command line interface for the staging store (``distagg staging ...``).

Keys and values cross the command line as hexadecimal strings (``""`` is the
empty byte string); they are decoded before use and compared as unsigned
bytes, never as integers or text.  All output is JSON on stdout; errors are
JSON on stdout plus a one-line message on stderr, exit code 1.

Commands::

    distagg staging put     DIR KEY_HEX VALUE_HEX
    distagg staging delete  DIR KEY_HEX
    distagg staging batch   DIR OPS.json
    distagg staging get     DIR KEY_HEX [--stats]
    distagg staging scan    DIR [--lower HEX] [--upper HEX] [--limit N]
    distagg staging flush   DIR
    distagg staging compact DIR
    distagg staging stats   DIR

``OPS.json`` is a list like ``[{"op": "put", "key": "00", "value": "ff"},
{"op": "delete", "key": "aa"}]`` (hex fields).  The mutating commands accept
the pause-point flags (``--pause-points``, ``--pause-signal-file``,
``--pause-wait-file``, ``--pause-delay``, ``--kill-at``) so tests can stop or
SIGKILL a real process around WAL syncs and manifest switches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .errors import InvalidOperation, StagingError
from .store import PAUSE_POINTS, StagingStore


def _print(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _decode_hex(text: str, what: str) -> bytes:
    if not isinstance(text, str) or len(text) % 2 != 0:
        raise InvalidOperation(f"{what}: hex string must have an even length, got {text!r}")
    if text and any(ch not in "0123456789abcdefABCDEF" for ch in text):
        raise InvalidOperation(f"{what}: not hexadecimal: {text!r}")
    return bytes.fromhex(text)


def _add_pause_args(parser) -> None:
    parser.add_argument("--pause-json", help="append pause events to this JSONL file")
    parser.add_argument("--pause-signal-file", help="write the reached pause point to this file")
    parser.add_argument("--pause-wait-file", help="block at pause points until this file is removed")
    parser.add_argument(
        "--pause-delay", type=float, default=None,
        help="sleep this many seconds at each honoured pause point",
    )
    parser.add_argument(
        "--pause-points", default=",".join(PAUSE_POINTS),
        help="comma separated pause points to honour ('all' for every point)",
    )
    parser.add_argument(
        "--kill-at", choices=PAUSE_POINTS, default=None,
        help="hard-kill this process with SIGKILL at the given pause point",
    )


def _make_pause(args):
    if args.pause_points.strip() == "all":
        active = set(PAUSE_POINTS)
    else:
        active = {point.strip() for point in args.pause_points.split(",") if point.strip()}
        unknown = active - set(PAUSE_POINTS)
        if unknown:
            raise SystemExit(f"unknown pause point(s): {', '.join(sorted(unknown))}")

    def pause(point: str, info: dict) -> None:
        if point not in active:
            return
        record = dict(info)
        record["pid"] = os.getpid()
        record["point"] = point
        if args.pause_json:
            with open(args.pause_json, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
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
            import time

            while os.path.exists(args.pause_wait_file):
                time.sleep(0.02)
        if args.kill_at == point:
            # Deliberate hard kill of the real process, no cleanup.
            os.kill(os.getpid(), 9)

    return pause


def _open(args, read_only: bool) -> StagingStore:
    kwargs = {}
    if getattr(args, "memtable_budget", None):
        kwargs["memtable_budget"] = args.memtable_budget
    if getattr(args, "block_size", None):
        kwargs["block_size"] = args.block_size
    pause = _make_pause(args) if hasattr(args, "pause_points") and not read_only else None
    return StagingStore.open(
        args.dir, read_only=read_only, pause=pause, **kwargs
    )


def _run(fn, read_only: bool = False) -> int:
    def command(args) -> int:
        try:
            store = _open(args, read_only)
        except StagingError as exc:
            _print(exc.to_dict())
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            return 1
        try:
            result = fn(store, args)
        except StagingError as exc:
            _print(exc.to_dict())
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            return 1
        finally:
            store.close()
        _print(result)
        return 0

    return command


def _cmd_put(store, args):
    commit = store.put(_decode_hex(args.key, "key"), _decode_hex(args.value, "value"))
    return {"commit": commit}


def _cmd_delete(store, args):
    commit = store.delete(_decode_hex(args.key, "key"))
    return {"commit": commit}


def _cmd_batch(store, args):
    with open(args.ops_file, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list):
        raise InvalidOperation("batch file must be a JSON list of operations")
    ops = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise InvalidOperation(f"op #{index}: not an object")
        op = entry.get("op")
        if op == "put":
            ops.append(("put", _decode_hex(entry.get("key", ""), "key"),
                        _decode_hex(entry.get("value", ""), "value")))
        elif op == "delete":
            ops.append(("delete", _decode_hex(entry.get("key", ""), "key")))
        else:
            raise InvalidOperation(f"op #{index}: unknown op {op!r}")
    return {"commit": store.write_batch(ops), "ops": len(ops)}


def _cmd_get(store, args):
    key = _decode_hex(args.key, "key")
    if args.stats:
        value, stats = store.get_with_stats(key)
    else:
        value, stats = store.get(key), None
    result = {"found": value is not None, "value_hex": value.hex() if value is not None else None}
    if stats is not None:
        result["lookup"] = stats
    return result


def _cmd_scan(store, args):
    lower = _decode_hex(args.lower, "lower") if args.lower is not None else None
    upper = _decode_hex(args.upper, "upper") if args.upper is not None else None
    entries = []
    with store.scan(lower=lower, upper=upper) as iterator:
        for key, value in iterator:
            entries.append({"key_hex": key.hex(), "value_hex": value.hex()})
            if args.limit is not None and len(entries) >= args.limit:
                return {"entries": entries, "complete": False,
                        "iterator": _iterator_stats(iterator)}
        return {"entries": entries, "complete": True, "iterator": _iterator_stats(iterator)}


def _iterator_stats(iterator) -> dict:
    return {
        "records_scanned": iterator.records_scanned,
        "keys_emitted": iterator.keys_emitted,
        "peak_buffered_bytes": iterator.peak_buffered_bytes,
    }


def _cmd_flush(store, args):
    return {"flushed": store.flush()}


def _cmd_compact(store, args):
    return store.compact()


def _cmd_stats(store, args):
    return store.stats()


def register(sub) -> None:
    """Attach the ``staging`` command group to the distagg argument parser."""
    parser = sub.add_parser(
        "staging",
        help="disk staging store (small LSM engine, independent of the aggregation db)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    staging_sub = parser.add_subparsers(dest="staging_command", required=True)

    def add_store_args(p):
        p.add_argument("dir")
        p.add_argument("--memtable-budget", type=int, default=None,
                       help="memtable byte budget before an automatic flush")
        p.add_argument("--block-size", type=int, default=None,
                       help="target segment block size in bytes")

    p = staging_sub.add_parser("put", help="set one key; keys/values are hex")
    add_store_args(p)
    p.add_argument("key")
    p.add_argument("value")
    _add_pause_args(p)
    p.set_defaults(func=_run(_cmd_put))

    p = staging_sub.add_parser("delete", help="delete one key (hex)")
    add_store_args(p)
    p.add_argument("key")
    _add_pause_args(p)
    p.set_defaults(func=_run(_cmd_delete))

    p = staging_sub.add_parser("batch", help="apply a JSON batch file atomically")
    add_store_args(p)
    p.add_argument("ops_file")
    _add_pause_args(p)
    p.set_defaults(func=_run(_cmd_batch))

    p = staging_sub.add_parser("get", help="read one key (hex)")
    p.add_argument("dir")
    p.add_argument("key")
    p.add_argument("--stats", action="store_true", help="include lookup access statistics")
    p.set_defaults(func=_run(_cmd_get, read_only=True))

    p = staging_sub.add_parser("scan", help="iterate [lower, upper) in key order (hex)")
    p.add_argument("dir")
    p.add_argument("--lower", default=None, help="inclusive lower bound, hex (default: none)")
    p.add_argument("--upper", default=None, help="exclusive upper bound, hex (default: none)")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=_run(_cmd_scan, read_only=True))

    p = staging_sub.add_parser("flush", help="flush the memtable to a new segment")
    add_store_args(p)
    _add_pause_args(p)
    p.set_defaults(func=_run(_cmd_flush))

    p = staging_sub.add_parser("compact", help="merge all on-disk segments, reclaiming versions")
    add_store_args(p)
    _add_pause_args(p)
    p.set_defaults(func=_run(_cmd_compact))

    p = staging_sub.add_parser("stats", help="print store sizes, versions and file inventory")
    p.add_argument("dir")
    p.set_defaults(func=_run(_cmd_stats, read_only=True))
