"""Compile parsed copybook declarations into a validated record layout.

The compiler

* builds the group/field tree from the flat declarations,
* resolves ``REDEFINES`` alternations (a target plus the branch items that
  immediately follow it in the same group),
* resolves ``OCCURS ... DEPENDING ON`` counters (full path, already decoded,
  non-repeating integer items),
* validates the branch-selection configuration that maps a previously
  decoded text/integer field to one branch per record,
* computes static byte spans and offsets where they exist — anything behind
  a variable-length item is reported as dynamic, never as a fake constant.

The result is a :class:`CompiledLayout`, which is immutable and can be
reused to decode any number of records or files.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union

from .errors import LayoutError, SelectorConfigError, UnsupportedError
from .parser import Occurs, Pic, RawDecl, parse_copybook

_COMP_SIZES = ((4, 2), (9, 4), (18, 8))


def _comp_bytes(total_digits: int) -> int:
    for limit, size in _COMP_SIZES:
        if total_digits <= limit:
            return size
    raise AssertionError("COMP digit count above 18 must be rejected at parse time")


def leaf_bytes(pic: Pic, usage: str) -> int:
    """Byte length of one occurrence of an elementary item."""
    if pic.category == "text":
        return pic.length
    if usage == "DISPLAY":
        return pic.total_digits
    if usage == "COMP":
        return _comp_bytes(pic.total_digits)
    if usage == "COMP-3":
        return pic.total_digits // 2 + 1
    raise AssertionError(f"unknown usage {usage}")


class Node:
    """One item of the record layout (group or elementary field)."""

    __slots__ = (
        "name", "level", "parent", "line", "pic", "usage", "occurs",
        "redefines", "children", "path", "display_path", "byte_len",
        "entries", "static_span", "min_span", "max_span", "static_offset",
        "order", "alternation", "counter",
    )

    def __init__(self, decl: RawDecl, parent: Optional["Node"]):
        self.name = decl.name                      # None for FILLER
        self.level = decl.level
        self.parent = parent
        self.line = decl.line
        self.pic = decl.pic
        self.usage = decl.usage
        self.occurs = decl.occurs
        self.redefines = decl.redefines
        self.children: List[Node] = []
        self.path: Optional[str] = None            # None for FILLER
        self.display_path: str = ""
        self.byte_len = 0                          # leaves: bytes of one occurrence
        self.entries: list = []                    # groups: Node | Alternation
        self.static_span: Optional[int] = None     # full span incl. occurs, if constant
        self.min_span = 0
        self.max_span = 0
        self.static_offset: Optional[int] = None   # offset of first occurrence, if constant
        self.order = -1                            # decode-order index
        self.alternation: Optional[Alternation] = None
        self.counter: Optional[Node] = None        # DEPENDING ON target, for occurs

    @property
    def is_group(self) -> bool:
        return self.pic is None

    @property
    def is_filler(self) -> bool:
        return self.name is None

    def label(self) -> str:
        return self.display_path or (self.name if self.name else "FILLER")


class Alternation:
    """A REDEFINES group: the target item plus its branch items."""

    __slots__ = ("target", "branches", "span", "selector_using", "value_map")

    def __init__(self, target: Node):
        self.target = target
        self.branches: List[Node] = []
        self.span = 0
        self.selector_using: Optional[Node] = None
        self.value_map: Dict[str, Node] = {}

    @property
    def path(self) -> str:
        return self.target.path

    def all_items(self) -> List[Node]:
        return [self.target] + self.branches


class CompiledLayout:
    """A validated, reusable record layout."""

    def __init__(self, root: Node, alternations: List[Alternation],
                 referenced: Dict[str, Node], source_lines: int):
        self.root = root
        self.alternations = alternations
        self.referenced = referenced            # path -> Node (counters + selectors)
        self.source_lines = source_lines

    @property
    def record_name(self) -> str:
        return self.root.name

    @property
    def min_span(self) -> int:
        return self.root.min_span

    @property
    def max_span(self) -> int:
        return self.root.max_span

    # -- layout description (the ``check`` command) -------------------------

    def describe(self) -> dict:
        fields: List[dict] = []
        self._describe_node(self.root, fields)
        return {
            "record": self.root.name,
            "source_lines": self.source_lines,
            "span": _span_dict(self.root),
            "fields": fields,
        }

    def _describe_node(self, node: Node, out: List[dict]) -> None:
        entry = {
            "path": node.display_path,
            "level": node.level,
            "kind": _kind_of(node),
            "offset": node.static_offset,
            "offset_kind": "static" if node.static_offset is not None else "dynamic",
            "span": _span_dict(node),
        }
        if node.pic is not None:
            entry["pic"] = node.pic.raw.upper()
            entry["usage"] = node.usage
        if node.is_filler:
            entry["filler"] = True
        if node.occurs is not None:
            if node.occurs.variable:
                entry["occurs"] = {
                    "kind": "depending",
                    "min": node.occurs.min_times,
                    "max": node.occurs.max_times,
                    "on": node.counter.path,
                }
            else:
                entry["occurs"] = {"kind": "fixed", "times": node.occurs.max_times}
        if node.alternation is not None:
            alt = node.alternation
            if alt.target is node:
                entry["redefines"] = {
                    "role": "target",
                    "group_span": alt.span,
                    "branches": [item.path for item in alt.all_items()],
                    "selected_by": {
                        "using": alt.selector_using.path,
                        "map": {key: branch.path for key, branch in alt.value_map.items()},
                    },
                }
            else:
                entry["redefines"] = {"role": "branch", "target": alt.target.path}
        out.append(entry)
        if node.is_group:
            for sub in node.entries:
                if isinstance(sub, Alternation):
                    for item in sub.all_items():
                        self._describe_node(item, out)
                else:
                    self._describe_node(sub, out)


def _kind_of(node: Node) -> str:
    if node.is_group:
        return "group"
    if node.pic.category == "text":
        return "text"
    return "integer" if node.pic.scale == 0 else "decimal"


def _span_dict(node: Node) -> dict:
    if node.static_span is not None:
        return {"kind": "fixed", "bytes": node.static_span}
    return {"kind": "variable", "min_bytes": node.min_span, "max_bytes": node.max_span}


# -- tree construction -------------------------------------------------------


def _build_tree(decls: List[RawDecl]) -> Node:
    first = decls[0]
    if first.level != 1:
        raise LayoutError(
            "the first declaration must be the 01 root group",
            line=first.line, field=first.display_name)
    root = Node(first, None)
    stack = [root]
    prev = root
    for decl in decls[1:]:
        if decl.level == 1:
            raise LayoutError("only one 01 root group is allowed",
                              line=decl.line, field=decl.display_name)
        if decl.level > prev.level:
            # A higher level than the immediately preceding item makes this
            # item subordinate to it — so it must be a group.
            if not prev.is_group:
                raise LayoutError(
                    f"level {decl.level:02d} item is subordinate to "
                    f"{prev.display_path or prev.name} (line {prev.line}), "
                    f"which has a PICTURE clause and cannot be a group",
                    line=decl.line, field=decl.display_name)
            parent = prev
        else:
            while decl.level <= stack[-1].level:
                stack.pop()
            if not stack:
                raise LayoutError(
                    f"level {decl.level:02d} item has no enclosing group",
                    line=decl.line, field=decl.display_name)
            parent = stack[-1]
        node = Node(decl, parent)
        parent.children.append(node)
        if node.is_group:
            stack.append(node)
        prev = node
    return root


def _finish_names(node: Node, prefix: str) -> None:
    """Assign full paths, check duplicate names, reject empty/FILLER groups."""
    if node.is_group and not node.children:
        raise LayoutError(
            "group has no subordinate items" if node.name else
            "FILLER groups are not supported (FILLER is elementary only)",
            line=node.line, field=node.display_path or node.name)
    if node.is_filler and node.is_group:
        raise UnsupportedError(
            "FILLER groups are not supported; give the group a name",
            line=node.line)
    seen: Dict[str, Node] = {}
    filler_count = 0
    for child in node.children:
        if child.is_filler:
            filler_count += 1
            suffix = "" if filler_count == 1 else f"({filler_count})"
            child.display_path = f"{prefix}.FILLER{suffix}" if prefix else f"FILLER{suffix}"
        else:
            child.path = f"{prefix}.{child.name}" if prefix else child.name
            child.display_path = child.path
            if child.name in seen:
                raise LayoutError(
                    f"duplicate field name {child.name} in this group (first "
                    f"declared at line {seen[child.name].line})",
                    line=child.line, field=child.path)
            seen[child.name] = child
        _finish_names(child, child.display_path)


def _resolve_redefines(group: Node) -> None:
    entries: List[Union[Node, Alternation]] = []
    open_alt: Optional[Alternation] = None
    for child in group.children:
        if child.redefines is None:
            entries.append(child)
            open_alt = None
            continue
        if child.name is None:
            raise LayoutError(
                "FILLER cannot be a REDEFINES branch: the selected branch "
                "needs a name in the decoded output",
                line=child.line, field=child.display_path)
        if child.redefines == child.name:
            raise LayoutError("an item cannot redefine itself",
                              line=child.line, field=child.display_path)
        if open_alt is not None and open_alt.target.name == child.redefines:
            open_alt.branches.append(child)
            child.alternation = open_alt
            continue
        if entries and isinstance(entries[-1], Alternation):
            alt = entries[-1]
            if any(item.name == child.redefines for item in alt.branches):
                raise LayoutError(
                    f"REDEFINES chains are not supported: {child.redefines} is "
                    f"itself a REDEFINES branch",
                    line=child.line, field=child.display_path)
            raise LayoutError(
                f"REDEFINES {child.redefines} must immediately follow the item "
                f"it redefines (or another branch of the same item)",
                line=child.line, field=child.display_path)
        if not entries or entries[-1].name != child.redefines:
            raise LayoutError(
                f"REDEFINES {child.redefines} must name the immediately "
                f"preceding item in the same group",
                line=child.line, field=child.display_path)
        target = entries[-1]
        alt = Alternation(target)
        alt.branches.append(child)
        target.alternation = alt
        child.alternation = alt
        entries[-1] = alt
        open_alt = alt
    group.entries = entries
    for child in group.children:
        if child.is_group:
            _resolve_redefines(child)


def _compute_spans(node: Node) -> None:
    """Fill static_span/min_span/max_span bottom-up (one occurrence)."""
    if node.is_group:
        static = 0
        minimum = 0
        maximum = 0
        for entry in node.entries:
            if isinstance(entry, Alternation):
                items = entry.all_items()
                for item in items:
                    _compute_spans(item)
                for item in items:
                    if item.static_span is None:
                        raise LayoutError(
                            "REDEFINES branches must have a fixed length: "
                            f"{item.display_path} contains a variable-length OCCURS",
                            line=item.line, field=item.display_path)
                entry.span = max(item.static_span for item in items)
                static += entry.span
                minimum += entry.span
                maximum += entry.span
            else:
                _compute_spans(entry)
                minimum += entry.min_span
                maximum += entry.max_span
                if static is not None:
                    if entry.static_span is None:
                        static = None
                    else:
                        static += entry.static_span
        item_static, item_min, item_max = static, minimum, maximum
    else:
        node.byte_len = leaf_bytes(node.pic, node.usage)
        item_static = item_min = item_max = node.byte_len

    if node.occurs is None:
        node.static_span = item_static
        node.min_span = item_min
        node.max_span = item_max
    elif node.occurs.variable:
        node.static_span = None
        node.min_span = node.occurs.min_times * item_min
        node.max_span = node.occurs.max_times * item_max
    else:
        times = node.occurs.max_times
        node.static_span = times * item_static if item_static is not None else None
        node.min_span = times * item_min
        node.max_span = times * item_max


def _assign_offsets_and_order(root: Node) -> None:
    order = [0]

    def visit_entry(entry, offset: Optional[int], in_occurs: bool) -> Optional[int]:
        """Assign static_offset/order for one entry; return the offset after it."""
        if isinstance(entry, Alternation):
            for item in entry.all_items():
                item.static_offset = None if in_occurs else offset
                _visit(item, None if in_occurs else offset, in_occurs)
            return None if offset is None else offset + entry.span
        entry.static_offset = None if in_occurs else offset
        _visit(entry, None if in_occurs else offset, in_occurs)
        if offset is None or entry.static_span is None:
            return None
        return offset + entry.static_span

    def _visit(node: Node, offset: Optional[int], in_occurs: bool) -> None:
        node.order = order[0]
        order[0] += 1
        if node.is_group:
            pos = offset
            child_in_occurs = in_occurs or node.occurs is not None
            for entry in node.entries:
                pos = visit_entry(entry, pos, child_in_occurs)

    root.static_offset = 0
    _visit(root, 0, False)


def _resolve_path(layout_root: Node, path: str, line: int, field: str,
                  what: str) -> Node:
    parts = path.split(".")
    if parts[0] != layout_root.name:
        raise LayoutError(
            f"{what} {path!r} must start with the record name {layout_root.name!r}",
            line=line, field=field)
    node = layout_root
    for part in parts[1:]:
        match = None
        if node.is_group:
            for child in node.children:
                if child.name == part:
                    match = child
                    break
        if match is None:
            raise LayoutError(
                f"{what} {path!r} does not exist (no field {part!r} under "
                f"{node.display_path})",
                line=line, field=field)
        node = match
    return node


def _check_reference(target: Node, layout_root: Node, line: int, field: str,
                     what: str, before: int) -> None:
    """Shared checks for DEPENDING ON counters and redefines selectors."""
    if target.is_group:
        raise LayoutError(f"{what} {target.path} is a group, not an elementary field",
                          line=line, field=field)
    node = target
    while node is not layout_root:
        if node.occurs is not None:
            raise LayoutError(
                f"{what} {target.path} is a repeating item (inside or itself "
                f"OCCURS); the reference must be a non-repeating field",
                line=line, field=field)
        if node.alternation is not None:
            raise LayoutError(
                f"{what} {target.path} sits inside a REDEFINES branch, so it is "
                f"not decoded for every record",
                line=line, field=field)
        node = node.parent
    if target.order >= before:
        raise LayoutError(
            f"{what} {target.path} is not decoded before this item; the "
            f"referenced field must appear earlier in the record",
            line=line, field=field)


def _resolve_counters(root: Node, referenced: Dict[str, Node]) -> None:
    def walk(node: Node) -> None:
        if node.occurs is not None and node.occurs.variable:
            occurs = node.occurs
            counter = _resolve_path(root, occurs.depending_on, node.line,
                                    node.display_path, "DEPENDING ON")
            if counter.pic.category != "numeric" or counter.pic.scale != 0:
                raise LayoutError(
                    f"DEPENDING ON {counter.path} must be an integer numeric "
                    f"field (no V decimal places)",
                    line=node.line, field=node.display_path)
            _check_reference(counter, root, node.line, node.display_path,
                             "DEPENDING ON", node.order)
            node.counter = counter
            referenced[counter.path] = counter
        if node.is_group:
            for entry in node.entries:
                if isinstance(entry, Alternation):
                    for item in entry.all_items():
                        walk(item)
                else:
                    walk(entry)

    walk(root)


def _check_occurs_bounds(root: Node) -> None:
    def walk(node: Node) -> None:
        occurs = node.occurs
        if occurs is not None:
            if occurs.max_times < 1:
                raise LayoutError("OCCURS upper bound must be at least 1",
                                  line=node.line, field=node.display_path)
            if occurs.min_times < 0 or occurs.min_times > occurs.max_times:
                raise LayoutError(
                    f"OCCURS bounds {occurs.min_times} TO {occurs.max_times} are "
                    f"not a valid range",
                    line=node.line, field=node.display_path)
        if node.is_group:
            for entry in node.entries:
                if isinstance(entry, Alternation):
                    for item in entry.all_items():
                        walk(item)
                else:
                    walk(entry)

    walk(root)


def _collect_alternations(root: Node) -> List[Alternation]:
    found: List[Alternation] = []

    def walk(node: Node, inside_occurs: bool) -> None:
        if not node.is_group:
            return
        for entry in node.entries:
            if isinstance(entry, Alternation):
                if inside_occurs or node.occurs is not None:
                    raise LayoutError(
                        "REDEFINES inside an OCCURS group is not supported: the "
                        "branch selector is resolved once per record",
                        line=entry.target.line, field=entry.target.display_path)
                found.append(entry)
                for item in entry.all_items():
                    walk(item, inside_occurs)
            else:
                walk(entry, inside_occurs or entry.occurs is not None)

    walk(root, False)
    return found


def _apply_selectors(root: Node, alternations: List[Alternation],
                     selectors: Optional[dict], referenced: Dict[str, Node]) -> None:
    selectors = selectors or {}
    if not isinstance(selectors, dict):
        raise SelectorConfigError("selector configuration must be a JSON object "
                                  "mapping a redefines group path to "
                                  '{"using": path, "map": {value: branch}}')
    normalized: Dict[str, dict] = {}
    for key, value in selectors.items():
        if not isinstance(key, str):
            raise SelectorConfigError("selector group paths must be strings")
        normalized[key.upper()] = value

    known = {alt.path: alt for alt in alternations}
    for key in normalized:
        if key not in known:
            raise SelectorConfigError(
                f"selector configured for {key!r}, which is not a REDEFINES "
                f"group in this copybook")

    for alt in alternations:
        raw = normalized.get(alt.path)
        if raw is None:
            raise SelectorConfigError(
                f"no selector configured for REDEFINES group {alt.path}: pass a "
                f"selection config mapping a previously decoded text/integer "
                f"field to one branch per record")
        if not isinstance(raw, dict) or "using" not in raw or "map" not in raw:
            raise SelectorConfigError(
                f"selector for {alt.path} must be an object with \"using\" and "
                f"\"map\" members")
        using = raw["using"]
        branch_map = raw["map"]
        if not isinstance(using, str):
            raise SelectorConfigError(f"selector for {alt.path}: \"using\" must be "
                                      f"a full field path string")
        using = using.upper()
        try:
            using_node = _resolve_path(root, using, alt.target.line,
                                       alt.target.path, "selector")
        except LayoutError as exc:
            raise SelectorConfigError(exc.message)
        if using_node.is_group or (
                using_node.pic.category != "text" and using_node.pic.scale != 0):
            raise SelectorConfigError(
                f"selector for {alt.path}: {using} must be a text (PIC X) or "
                f"integer numeric field")
        try:
            _check_reference(using_node, root, alt.target.line, alt.target.path,
                             "selector", alt.target.order)
        except LayoutError as exc:
            raise SelectorConfigError(exc.message)
        if not isinstance(branch_map, dict) or not branch_map:
            raise SelectorConfigError(
                f"selector for {alt.path}: \"map\" must be a non-empty object "
                f"mapping field values to branch paths")
        valid = {item.path: item for item in alt.all_items()}
        value_map: Dict[str, Node] = {}
        for key, branch_path in branch_map.items():
            if not isinstance(branch_path, str):
                raise SelectorConfigError(
                    f"selector for {alt.path}: branch path for value {key!r} must "
                    f"be a string")
            branch = valid.get(branch_path.upper())
            if branch is None:
                raise SelectorConfigError(
                    f"selector for {alt.path}: {branch_path!r} is not a branch of "
                    f"this REDEFINES group (branches: {', '.join(sorted(valid))})")
            value_map[str(key)] = branch
        missing = [item.path for item in alt.all_items()
                   if item not in value_map.values()]
        if missing:
            raise SelectorConfigError(
                f"selector for {alt.path}: branch(es) {', '.join(missing)} are "
                f"not reachable through the map")
        alt.selector_using = using_node
        alt.value_map = value_map
        referenced[using_node.path] = using_node


def compile_copybook(source: str, selectors: Optional[dict] = None) -> CompiledLayout:
    """Compile copybook text into a reusable :class:`CompiledLayout`.

    ``selectors`` maps each REDEFINES group (full path of the redefined
    item) to ``{"using": <full path of a previously decoded text/integer
    field>, "map": {<field value>: <full path of the branch to decode>}}``.
    A top-level ``{"selectors": {...}}`` wrapper (the on-disk file format)
    is accepted as well.
    """
    if isinstance(selectors, dict) and set(selectors.keys()) == {"selectors"}:
        selectors = selectors["selectors"]
    decls = parse_copybook(source)
    root = _build_tree(decls)
    if root.occurs is not None:
        raise LayoutError("the 01 root item cannot have an OCCURS clause",
                          line=root.line, field=root.name)
    root.path = root.name
    root.display_path = root.name
    _finish_names(root, root.name)
    _resolve_redefines(root)
    _check_occurs_bounds(root)
    _compute_spans(root)
    _assign_offsets_and_order(root)
    referenced: Dict[str, Node] = {}
    _resolve_counters(root, referenced)
    alternations = _collect_alternations(root)
    _apply_selectors(root, alternations, selectors, referenced)
    return CompiledLayout(root, alternations, referenced,
                          source_lines=source.count("\n") + 1)
