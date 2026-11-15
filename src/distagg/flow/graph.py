"""Flow-graph definition, the JSON format and static validation.

JSON format (all members are required unless marked optional)::

    {
      "name": "supplier-batch",                  // optional label
      "max_line_bytes": 1048576,                 // optional, default 1 MiB
      "max_record_bytes": 1048576,               // optional, default 1 MiB
      "nodes": [
        {"name": "in",  "type": "source",  "path": "in.jsonl"},
        {"name": "bad", "type": "select",  "field": "status",
         "condition": "eq", "value": "error"},
        {"name": "items", "type": "explode", "field": "items"},
        {"name": "flat", "type": "project",
         "fields": {"item_id": "id", "qty": "qty"}},
        {"name": "m",   "type": "merge"},
        {"name": "out", "type": "sink",    "path": "merged.jsonl"}
      ],
      "edges": [
        {"from": "in", "to": "bad"},
        {"from": "in", "to": "items"},
        {"from": "bad", "to": "out"},
        {"from": "items", "to": "flat"},
        {"from": "flat", "to": "m"},
        {"from": "bad", "to": "m"}
      ]
    }

Node types and their members:

===============  ===========================================================
``source``       ``path`` — one UTF-8 JSONL file. No inputs; one or more
                 outputs.
``select``       ``field``, ``condition`` (one of eq ne lt le gt ge
                 is_null), ``value`` (integer, string, boolean or null; must
                 be absent for ``is_null``). One input, one or more outputs.
``project``      ``fields`` — object mapping *output* field name to *input*
                 field name. One input, one or more outputs.
``explode``      ``field`` — top-level array field. One input, one or more
                 outputs.
``merge``        no extra members. At least two inputs, one or more outputs.
``sink``         ``path`` — output file name relative to the run's fresh
                 result directory. One or more inputs, no outputs.
``custom``       Python API only (never accepted from a graph file): a caller
                 supplied co-operative async one-to-many operator.
===============  ===========================================================

An edge is ``{"from": node, "to": node}`` plus an optional ``capacity``
(positive integer, default from :class:`FlowConfig`).  Multiple edges out of
one node are a **broadcast** (each successor receives its own copy of every
record), never a work split.

Validation happens entirely before the run opens a source or creates a
result directory.  It rejects duplicate node names, edges that reference an
unknown node, duplicate edges, self loops, every cycle, wrong input counts
(ordinary nodes exactly one, ``merge`` at least two, sources none, sinks
none), wrong output counts (sinks none; every other node at least one), and
nodes from which no sink can be reached.  Two sinks writing the same path and
sink paths that escape the result directory are rejected as well.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import GraphError
from .ops import validate_condition, validate_projection

NODE_TYPES = ("source", "select", "project", "explode", "merge", "sink")
FILE_NODE_TYPES = NODE_TYPES  # custom is API-only

_TOP_KEYS = {"name", "max_line_bytes", "max_record_bytes", "nodes", "edges"}
_NODE_KEYS = {
    "source": {"name", "type", "path"},
    "select": {"name", "type", "field", "condition", "value"},
    "project": {"name", "type", "fields"},
    "explode": {"name", "type", "field"},
    "merge": {"name", "type"},
    "sink": {"name", "type", "path"},
    "custom": {"name", "type"},
}
_EDGE_KEYS = {"from", "to", "capacity"}

INT64_MAX = 2 ** 63 - 1


class Node:
    """One graph node.  ``options`` holds the type-specific settings."""

    __slots__ = ("name", "kind", "options", "operator")

    def __init__(self, name: str, kind: str, options: Dict[str, Any],
                 operator: Any = None):
        self.name = name
        self.kind = kind
        self.options = options
        self.operator = operator  # only for kind == "custom"

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Node({self.name!r}, {self.kind!r})"


class Edge:
    __slots__ = ("source", "target", "capacity")

    def __init__(self, source: str, target: str, capacity: Optional[int]):
        self.source = source
        self.target = target
        self.capacity = capacity

    def key(self) -> Tuple[str, str]:
        return (self.source, self.target)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Edge({self.source!r} -> {self.target!r})"


class FlowGraph:
    """A finite directed processing graph with named nodes and edges."""

    def __init__(self, name: Optional[str] = None,
                 max_line_bytes: Optional[int] = None,
                 max_record_bytes: Optional[int] = None):
        self.name = name
        self.max_line_bytes = max_line_bytes
        self.max_record_bytes = max_record_bytes
        self.nodes: List[Node] = []
        self.edges: List[Edge] = []
        self._by_name: Dict[str, Node] = {}
        self._validated = False
        # Populated by validate():
        self.order: List[str] = []
        self.successors: Dict[str, List[Edge]] = {}
        self.predecessors: Dict[str, List[Edge]] = {}
        self.sink_paths: Dict[str, str] = {}

    # -- construction ----------------------------------------------------- #

    def add_node(self, name: str, kind: str, operator: Any = None,
                 **options: Any) -> Node:
        self._validated = False
        node = Node(name, kind, dict(options), operator)
        self.nodes.append(node)
        return node

    def add_edge(self, source: str, target: str,
                 capacity: Optional[int] = None) -> Edge:
        self._validated = False
        edge = Edge(source, target, capacity)
        self.edges.append(edge)
        return edge

    def source(self, name: str, path: str) -> Node:
        return self.add_node(name, "source", path=path)

    def select(self, name: str, field: str, condition: str,
               value: Any = None) -> Node:
        options = {"field": field, "condition": condition}
        if condition != "is_null":
            options["value"] = value
        return self.add_node(name, "select", **options)

    def project(self, name: str, fields: Dict[str, str]) -> Node:
        return self.add_node(name, "project", fields=dict(fields))

    def explode(self, name: str, field: str) -> Node:
        return self.add_node(name, "explode", field=field)

    def merge(self, name: str) -> Node:
        return self.add_node(name, "merge")

    def sink(self, name: str, path: str) -> Node:
        return self.add_node(name, "sink", path=path)

    def custom(self, name: str, operator: Any) -> Node:
        """Register a caller-supplied co-operative async operator (API only)."""
        return self.add_node(name, "custom", operator=operator)

    # -- validation ------------------------------------------------------- #

    def validate(self) -> "FlowGraph":
        problems: List[str] = []
        self._index_nodes(problems)
        if problems:
            raise GraphError(problems)
        self._validate_limits(problems)
        self._validate_node_options(problems)
        self._index_edges(problems)
        self._validate_degrees(problems)
        self._validate_paths(problems)
        self._find_cycle(problems)
        self._validate_reachability(problems)
        if problems:
            raise GraphError(problems)
        self._build_order()
        self._validated = True
        return self

    def _index_nodes(self, problems: List[str]) -> None:
        if not self.nodes:
            problems.append("graph has no nodes")
            return
        self._by_name = {}
        for node in self.nodes:
            if not isinstance(node.name, str) or not node.name:
                problems.append("every node needs a non-empty string name")
                continue
            if node.name in self._by_name:
                problems.append(f"duplicate node name {node.name!r}")
                continue
            if node.kind not in _NODE_KEYS:
                problems.append(
                    f"node {node.name!r} has unknown type {node.kind!r}"
                )
                continue
            self._by_name[node.name] = node

    def _validate_limits(self, problems: List[str]) -> None:
        for label, value in (("max_line_bytes", self.max_line_bytes),
                             ("max_record_bytes", self.max_record_bytes)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                problems.append(f"{label} must be a positive integer")

    def _validate_node_options(self, problems: List[str]) -> None:
        for node in self._by_name.values():
            opts = node.options
            if node.kind == "custom":
                if node.operator is None:
                    problems.append(
                        f"custom node {node.name!r} has no callable operator"
                    )
                continue
            allowed = _NODE_KEYS[node.kind]
            unknown = set(opts) - allowed
            if unknown:
                problems.append(
                    f"node {node.name!r} ({node.kind}) has unknown setting(s) "
                    + ", ".join(repr(sorted(unknown)))
                )
            if node.kind in ("source", "sink"):
                path = opts.get("path")
                if not isinstance(path, str) or not path:
                    problems.append(
                        f"node {node.name!r} ({node.kind}) needs a non-empty "
                        "string 'path'"
                    )
                elif node.kind == "sink":
                    _check_sink_path(path, node.name, problems)
            elif node.kind == "select":
                try:
                    validate_condition(opts.get("field"),
                                       opts.get("condition"),
                                       opts.get("value"))
                except GraphError as exc:
                    for problem in exc.problems:
                        problems.append(f"node {node.name!r}: {problem}")
                if opts.get("condition") == "is_null" and "value" in opts:
                    problems.append(
                        f"node {node.name!r}: 'value' must be omitted for is_null"
                    )
            elif node.kind == "project":
                fields = opts.get("fields")
                if not isinstance(fields, dict) or not fields:
                    problems.append(
                        f"node {node.name!r}: 'fields' must be a non-empty "
                        "object mapping output names to input names"
                    )
                else:
                    for out_name, in_name in fields.items():
                        try:
                            validate_projection(out_name, in_name)
                        except GraphError as exc:
                            for problem in exc.problems:
                                problems.append(f"node {node.name!r}: {problem}")
            elif node.kind == "explode":
                field = opts.get("field")
                if not isinstance(field, str) or not field:
                    problems.append(
                        f"node {node.name!r}: 'field' must be a non-empty string"
                    )

    def _index_edges(self, problems: List[str]) -> None:
        self.successors = {name: [] for name in self._by_name}
        self.predecessors = {name: [] for name in self._by_name}
        seen = set()
        for edge in self.edges:
            if edge.source not in self._by_name:
                problems.append(
                    f"edge {edge.source!r} -> {edge.target!r} starts at an "
                    "unknown node"
                )
                continue
            if edge.target not in self._by_name:
                problems.append(
                    f"edge {edge.source!r} -> {edge.target!r} ends at an "
                    "unknown node"
                )
                continue
            key = edge.key()
            if key in seen:
                problems.append(
                    f"duplicate edge {edge.source!r} -> {edge.target!r}"
                )
                continue
            seen.add(key)
            if edge.capacity is not None and (
                isinstance(edge.capacity, bool)
                or not isinstance(edge.capacity, int)
                or edge.capacity <= 0
            ):
                problems.append(
                    f"edge {edge.source!r} -> {edge.target!r} capacity must be "
                    "a positive integer"
                )
            self.successors[edge.source].append(edge)
            self.predecessors[edge.target].append(edge)

    def _validate_degrees(self, problems: List[str]) -> None:
        for node in self._by_name.values():
            inputs = len(self.predecessors[node.name])
            outputs = len(self.successors[node.name])
            if node.kind == "source":
                if inputs != 0:
                    problems.append(
                        f"source {node.name!r} must have no input, has {inputs}"
                    )
            elif node.kind == "sink":
                if outputs != 0:
                    problems.append(
                        f"sink {node.name!r} must have no output, has {outputs}"
                    )
                if inputs == 0:
                    problems.append(
                        f"sink {node.name!r} has no input (no records can arrive)"
                    )
            elif node.kind == "merge":
                if inputs < 2:
                    problems.append(
                        f"merge {node.name!r} needs at least two inputs, has "
                        f"{inputs}"
                    )
            else:
                if inputs != 1:
                    problems.append(
                        f"node {node.name!r} ({node.kind}) needs exactly one "
                        f"input, has {inputs}"
                    )
            if node.kind != "sink" and outputs == 0:
                problems.append(
                    f"node {node.name!r} has no output (records would stop here)"
                )
        for node in self._by_name.values():
            if node.kind == "source" and len(self.predecessors[node.name]) == 0 \
                    and len(self.successors[node.name]) == 0:
                # Already reported as "no output"; keep the message specific.
                pass

    def _validate_paths(self, problems: List[str]) -> None:
        sources = {}
        for node in self._by_name.values():
            if node.kind == "source":
                value = node.options["path"]
                if isinstance(value, str):
                    sources[node.name] = os.path.normpath(value)
        sinks: Dict[str, str] = {}
        for node in self._by_name.values():
            if node.kind == "sink":
                value = node.options.get("path")
                if not isinstance(value, str) or not value:
                    continue
                normalised = os.path.normpath(value)
                if normalised in sinks:
                    problems.append(
                        f"sinks {sinks[normalised]!r} and {node.name!r} both "
                        f"write {value!r}"
                    )
                sinks[normalised] = node.name
        self.sink_paths = {
            name: self._by_name[name].options["path"]
            for name in self._by_name
            if self._by_name[name].kind == "sink"
            and isinstance(self._by_name[name].options.get("path"), str)
        }

    def _find_cycle(self, problems: List[str]) -> None:
        # Nodes not yet reached by the topological sort are exactly those on or
        # behind a cycle.  Report one concrete cycle to make the error useful.
        node_names = [name for name in self._by_name]
        state: Dict[str, int] = {}  # 0 = unseen, 1 = on stack, 2 = done
        stack: List[str] = []

        def visit(name: str) -> Optional[List[str]]:
            state[name] = 1
            stack.append(name)
            for edge in self.successors[name]:
                target = edge.target
                if state.get(target, 0) == 0:
                    found = visit(target)
                    if found is not None:
                        return found
                elif state.get(target) == 1:
                    start = stack.index(target)
                    return stack[start:] + [target]
            stack.pop()
            state[name] = 2
            return None

        import sys

        old_limit = sys.getrecursionlimit()
        try:
            if len(node_names) + 100 > old_limit:
                sys.setrecursionlimit(len(node_names) + 1000)
            for name in node_names:
                if state.get(name, 0) == 0:
                    cycle = visit(name)
                    if cycle is not None:
                        problems.append(
                            "graph contains a cycle: " + " -> ".join(cycle)
                        )
                        return
        finally:
            sys.setrecursionlimit(old_limit)

    def _validate_reachability(self, problems: List[str]) -> None:
        # Reverse reachability from all sinks: every node must be able to send
        # a record to some sink.
        to_sinks = set()
        todo = [name for name, node in self._by_name.items()
                if node.kind == "sink"]
        to_sinks.update(todo)
        while todo:
            current = todo.pop()
            for edge in self.predecessors[current]:
                if edge.source not in to_sinks:
                    to_sinks.add(edge.source)
                    todo.append(edge.source)
        unreachable = sorted(set(self._by_name) - to_sinks)
        if unreachable:
            problems.append(
                "node(s) do not lead to any sink: " + ", ".join(unreachable)
            )

    def _build_order(self) -> None:
        indegree = {
            name: len(self.predecessors[name]) for name in self._by_name
        }
        ready = [name for name in self._by_name if indegree[name] == 0]
        order: List[str] = []
        while ready:
            name = ready.pop(0)
            order.append(name)
            for edge in self.successors[name]:
                indegree[edge.target] -= 1
                if indegree[edge.target] == 0:
                    ready.append(edge.target)
        self.order = order

    # -- helpers ---------------------------------------------------------- #

    def node(self, name: str) -> Node:
        if name not in self._by_name:
            self._by_name = {node.name: node for node in self.nodes}
        return self._by_name[name]

    def ensure_valid(self) -> None:
        if not self._validated:
            self.validate()

    def to_dict(self) -> dict:
        data: Dict[str, Any] = {"nodes": [], "edges": []}
        if self.name is not None:
            data["name"] = self.name
        if self.max_line_bytes is not None:
            data["max_line_bytes"] = self.max_line_bytes
        if self.max_record_bytes is not None:
            data["max_record_bytes"] = self.max_record_bytes
        for node in self.nodes:
            entry = {"name": node.name, "type": node.kind}
            entry.update(node.options)
            data["nodes"].append(entry)
        for edge in self.edges:
            entry = {"from": edge.source, "to": edge.target}
            if edge.capacity is not None:
                entry["capacity"] = edge.capacity
            data["edges"].append(entry)
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return f"FlowGraph({len(self.nodes)} nodes, {len(self.edges)} edges)"


def _check_sink_path(path: str, node_name: str, problems: List[str]) -> None:
    normalised = path.replace("\\", "/")
    if normalised.startswith("/") or (len(normalised) > 1 and normalised[1] == ":"):
        problems.append(
            f"sink {node_name!r} path {path!r} must be relative to the "
            "result directory"
        )
        return
    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        problems.append(
            f"sink {node_name!r} path {path!r} must stay inside the result "
            "directory"
        )


# --------------------------------------------------------------------------- #
# JSON loading
# --------------------------------------------------------------------------- #

class _DuplicateMember(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _reject_duplicates(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateMember(key)
        seen.add(key)
    return dict(pairs)


def _loads_strict(text: str, path: Optional[str]) -> Any:
    def reject_constant(value):
        raise GraphError([f"non-JSON constant {value!r} is not allowed"], path)

    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates,
                          parse_constant=reject_constant)
    except _DuplicateMember as exc:
        raise GraphError(
            [f"duplicate JSON member {exc.key!r} in the graph document"], path
        ) from exc
    except json.JSONDecodeError as exc:
        raise GraphError(
            [f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"],
            path,
        ) from exc


def parse_graph(document: Any, *, path: Optional[str] = None,
                source_base: Optional[str] = None) -> FlowGraph:
    """Build and validate a graph from an already decoded JSON document."""
    problems: List[str] = []
    if not isinstance(document, dict):
        raise GraphError(["graph document must be a JSON object"], path)
    unknown = set(document) - _TOP_KEYS
    if unknown:
        problems.append(
            "unknown top-level member(s): " + ", ".join(sorted(map(repr, unknown)))
        )

    name = document.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        problems.append("'name' must be a non-empty string when present")

    def positive_int(label: str) -> Optional[int]:
        value = document.get(label)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            problems.append(f"{label} must be a positive integer")
            return None
        return value

    max_line = positive_int("max_line_bytes")
    max_record = positive_int("max_record_bytes")

    raw_nodes = document.get("nodes")
    raw_edges = document.get("edges")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise GraphError(problems + ["'nodes' must be a non-empty array"], path)
    if not isinstance(raw_edges, list):
        raise GraphError(problems + ["'edges' must be an array"], path)

    graph = FlowGraph(name=name, max_line_bytes=max_line,
                      max_record_bytes=max_record)

    for index, raw in enumerate(raw_nodes):
        label = f"nodes[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{label} must be an object")
            continue
        kind = raw.get("type")
        if kind not in _NODE_KEYS or kind == "custom":
            if kind == "custom":
                problems.append(
                    f"{label} uses type 'custom', which is Python-API only: a "
                    "graph file never loads caller code"
                )
            else:
                problems.append(
                    f"{label} has unknown or missing type {kind!r}"
                )
            continue
        node_name = raw.get("name")
        if not isinstance(node_name, str) or not node_name:
            problems.append(f"{label} needs a non-empty string name")
            continue
        allowed = _NODE_KEYS[kind]
        extra = set(raw) - allowed
        if extra:
            problems.append(
                f"{label} ({node_name!r}, {kind}) has unknown setting(s) "
                + ", ".join(sorted(map(repr, extra)))
            )
        missing = set(allowed) - set(raw)
        # 'value' is optional only for is_null; all other declared keys are
        # required.  Mesh this with the per-type rules in validation.
        if kind == "select" and raw.get("condition") == "is_null":
            missing.discard("value")
        required_missing = sorted(missing)
        if required_missing:
            problems.append(
                f"{label} ({node_name!r}, {kind}) is missing "
                + ", ".join(repr(key) for key in required_missing)
            )
            continue
        node_path = raw.get("path")
        if kind == "source" and isinstance(node_path, str) and source_base:
            node_path = os.path.normpath(os.path.join(source_base, node_path))
        options = {}
        for key in allowed - {"name", "type"}:
            if key == "value" and "value" not in raw:
                continue
            options[key] = node_path if key == "path" else raw[key]
        graph.add_node(node_name, kind, **options)

    for index, raw in enumerate(raw_edges):
        label = f"edges[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{label} must be an object")
            continue
        extra = set(raw) - _EDGE_KEYS
        if extra:
            problems.append(
                f"{label} has unknown setting(s): "
                + ", ".join(sorted(map(repr, extra)))
            )
        if "from" not in raw or "to" not in raw:
            problems.append(f"{label} needs both 'from' and 'to'")
            continue
        source, target = raw["from"], raw["to"]
        if not isinstance(source, str) or not isinstance(target, str):
            problems.append(f"{label} 'from'/'to' must be node names")
            continue
        capacity = raw.get("capacity")
        if capacity is not None and (
            isinstance(capacity, bool) or not isinstance(capacity, int)
            or capacity <= 0
        ):
            problems.append(f"{label} capacity must be a positive integer")
            continue
        graph.add_edge(source, target, capacity)

    if problems:
        raise GraphError(problems, path)
    try:
        graph.validate()
    except GraphError as exc:
        raise GraphError(exc.problems, path) from None
    return graph


def load_graph(path: str) -> FlowGraph:
    """Read a JSON graph file, resolve relative source paths and validate."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except UnicodeDecodeError as exc:
        raise GraphError([f"graph file is not valid UTF-8: {exc}"], path) from exc
    except OSError as exc:
        raise GraphError([f"cannot read graph file: {exc}"], path) from exc
    document = _loads_strict(text, path)
    base = os.path.dirname(os.path.abspath(path))
    return parse_graph(document, path=path, source_base=base)
