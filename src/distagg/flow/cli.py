"""Command line entry points for the flow executor.

::

    distagg flow check GRAPH.json
    distagg flow run   GRAPH.json [--results DIR] [--capacity N] \
        [--max-inflight N] [--max-line-bytes N] [--max-record-bytes N] \
        [--result-capacity N]

``check`` only validates the graph (it never opens a source or creates an
output).  ``run`` opens a fresh result directory, runs the graph to completion
and prints a run summary as one JSON object on stdout.

Exit status:

* 0 — success: every sink received every record it should and closed cleanly;
* 1 — failure (bad graph, input/operator/sink error) or cancellation;
* 2 — command line usage error (argparse).

Failures print ``"status": "failed"`` (or ``"cancelled"``) in the summary and
never a success summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from typing import Optional

from .config import FlowConfig
from .engine import start_run
from .errors import FlowError, GraphError
from .graph import load_graph


def _print(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _positive_int(text: str) -> int:
    try:
        value = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be positive: {text!r}")
    return value


def _cmd_check(args) -> int:
    try:
        graph = load_graph(args.graph)
    except GraphError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    _print({
        "valid": True,
        "graph": graph.name,
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "nodes": [
            {"name": node.name, "type": node.kind} for node in graph.nodes
        ],
        "topological_order": list(graph.order),
    })
    return 0


async def _run(args) -> int:
    try:
        graph = load_graph(args.graph)
        config = FlowConfig(
            args.results,
            default_capacity=args.capacity,
            max_inflight_per_node=args.max_inflight,
            max_line_bytes=args.max_line_bytes,
            max_record_bytes=args.max_record_bytes,
            result_capacity=args.result_capacity,
            max_nodes=args.max_nodes,
            max_edges=args.max_edges,
        )
        run = start_run(graph, config, base_dir=os.getcwd())
    except GraphError as exc:
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except ValueError as exc:
        _print({"error": "flow_config_error", "message": str(exc)})
        print(f"error: flow_config_error: {exc}", file=sys.stderr)
        return 1

    loop = asyncio.get_running_loop()
    # Ctrl-C / SIGTERM request the same cooperative stop as run.cancel(): all
    # nodes close their files, the summary says "cancelled" and the exit code
    # is non-zero.
    for signal_name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, run.cancel)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass
    task = loop.create_task(run.run_to_completion())
    try:
        summary = await task
    except asyncio.CancelledError:  # pragma: no cover - external cancel
        run.cancel()
        summary = await asyncio.shield(run.wait())
    except FlowError as exc:  # pragma: no cover - summary carries the error
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    _print(summary.to_dict())
    if summary.ok:
        return 0
    print(f"error: flow {summary.status}: {summary.error}", file=sys.stderr)
    return 1


def _cmd_run(args) -> int:
    try:
        return asyncio.run(_run(args))
    except FlowError as exc:  # pragma: no cover - defensive
        _print(exc.to_dict())
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except OSError as exc:  # pragma: no cover - defensive
        _print({"error": "flow_io_error", "message": str(exc)})
        print(f"error: flow_io_error: {exc}", file=sys.stderr)
        return 1


def _add_common_limits(parser) -> None:
    parser.add_argument(
        "--capacity", type=_positive_int, default=8,
        help="default capacity of an edge with no declared capacity (default 8)")
    parser.add_argument(
        "--max-inflight", type=_positive_int, default=8,
        help="max records assigned to one node but not yet finished (default 8)")
    parser.add_argument(
        "--max-line-bytes", type=_positive_int, default=None,
        help="hard byte limit for one physical input line (default: graph "
             "value or 1 MiB)")
    parser.add_argument(
        "--max-record-bytes", type=_positive_int, default=None,
        help="hard byte limit for one encoded record (default: graph value "
             "or 1 MiB)")
    parser.add_argument(
        "--result-capacity", type=_positive_int, default=64,
        help="bounded sink-to-caller result buffer (default 64; only used in "
             "the Python API's result_stream mode)")
    parser.add_argument(
        "--max-nodes", type=_positive_int, default=256,
        help="reject graphs with more nodes than this (default 256)")
    parser.add_argument(
        "--max-edges", type=_positive_int, default=1024,
        help="reject graphs with more edges than this (default 1024)")


def register(sub) -> None:
    """Attach the ``flow`` command group to the distagg parser."""
    parser = sub.add_parser(
        "flow",
        help="local asynchronous flow executor (JSONL sources, operators, sinks)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    flow_sub = parser.add_subparsers(dest="flow_command", required=True)

    check = flow_sub.add_parser(
        "check", help="validate a graph file without opening any input or "
                      "creating any output")
    check.add_argument("graph", help="graph definition JSON")
    check.set_defaults(func=_cmd_check)

    run = flow_sub.add_parser(
        "run", help="run a graph into a fresh result directory")
    run.add_argument("graph", help="graph definition JSON")
    run.add_argument(
        "--results", default=None,
        help="fresh result directory (default: a new directory under the "
             "current directory); an existing path is refused")
    _add_common_limits(run)
    run.set_defaults(func=_cmd_run)


def main(argv=None) -> int:
    """Standalone entry used by ``python -m distagg.flow``."""
    parser = argparse.ArgumentParser(prog="distagg flow", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    flow_sub = parser.add_subparsers(dest="flow_command", required=True)
    check = flow_sub.add_parser("check")
    check.add_argument("graph")
    check.set_defaults(func=_cmd_check)
    run = flow_sub.add_parser("run")
    run.add_argument("graph")
    run.add_argument("--results", default=None)
    _add_common_limits(run)
    run.set_defaults(func=_cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
