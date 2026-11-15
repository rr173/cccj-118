"""Read an XLSX package into an addressable cell store -- and nothing more.

This module follows the real OOXML relationship graph:

* ``_rels/.rels`` -> the package's workbook part (usually
  ``xl/workbook.xml``);
* ``workbook.xml`` -> ``<sheets><sheet name= r:id=...>``;
* ``xl/_rels/workbook.xml.rels`` maps those relationship ids to worksheet
  part names.

No part name is guessed from a fixed list such as ``xl/worksheets/sheet1.xml``.
Every part name is resolved relative to the package root and must stay inside
it; any relationship whose target escapes the package (``..``, an absolute
path, a URI with a scheme such as ``http:``) is an input fault.  Nothing is
ever fetched over the network.

Cell semantics implemented here:

* shared strings (``t="s"``), inline strings (``t="inlineStr"``), numbers
  (``t="n"`` / no type), booleans (``t="b"``), formula-string results
  (``t="str"``), empty strings (``t="str"`` with an empty ``<v>``) and Excel
  error cells (``t="e"``);
* raw numeric text becomes a :class:`decimal.Decimal` **directly**; no
  ``float`` is created on the way in;
* style-based date formatting is deliberately ignored: a date-formatted cell
  is handed over as the raw serial number (documented in docs/WORKBOOK.md);
* merged cells only yield a value through their top-left anchor; every other
  covered coordinate is blank;
* cached ``<v>`` next to a formula is recorded for diagnostics but never used
  as a value;
* shared formulas are resolved from the master's formula, translating
  relative references per member, independent of XML element order.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .errors import WorkbookPackageError, WorkbookFormulaError
from .formula import parse_formula, translate_ref_guarded
from .refs import (
    MAX_COLUMN,
    MAX_ROW,
    RefError,
    address,
    column_to_index,
    in_grid,
)
from .values import (
    BLANK,
    KNOWN_ERROR_CODES,
    Bool,
    Number,
    Scalar,
    Text,
    make_error,
    parse_decimal_literal,
)

# ---------------------------------------------------------------------------
# package and relationship handling
# ---------------------------------------------------------------------------

def _normalize_part(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return posixpath.normpath(name)


def safe_resolve(base_part: str, target: str, *, part: str) -> str:
    """Resolve a relationship target against ``base_part`` without escaping."""
    if not target:
        raise WorkbookPackageError("relationship has an empty target", part)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
        raise WorkbookPackageError(
            f"relationship target {target!r} is an external URI; "
            "external parts are not read", part,
        )
    if target.startswith("//"):
        raise WorkbookPackageError(
            f"relationship target {target!r} is a network path", part
        )
    target = target.replace("\\", "/")
    if target.startswith("/"):
        candidate = posixpath.normpath(target.lstrip("/"))
    else:
        base_dir = posixpath.dirname(base_part)
        candidate = posixpath.normpath(posixpath.join(base_dir, target))
    if candidate == ".." or candidate.startswith("../") or candidate.startswith("/"):
        raise WorkbookPackageError(
            f"relationship target {target!r} escapes the package", part
        )
    return candidate


def rels_part_for(part: str) -> str:
    directory = posixpath.dirname(part)
    name = posixpath.basename(part)
    return posixpath.join(directory, "_rels", name + ".rels")


class Package:
    """Read-only view over the ZIP container's XML relationship graph."""

    def __init__(self, path: str):
        self.path = path
        try:
            self._zip = zipfile.ZipFile(path, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise WorkbookPackageError(
                f"cannot open XLSX container: {exc}"
            ) from exc
        self._names = set(self._zip.namelist())

    def close(self) -> None:
        self._zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def has(self, part: str) -> bool:
        return part in self._names

    def read(self, part: str) -> bytes:
        normalized = _normalize_part(part)
        if normalized not in self._names:
            raise WorkbookPackageError("part is missing from the package", part)
        try:
            return self._zip.read(normalized)
        except (KeyError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise WorkbookPackageError(
                f"cannot read part: {exc}", part
            ) from exc

    def names(self):
        return sorted(self._names)

    def relationships(self, part: str) -> Dict[str, Tuple[str, str, str]]:
        """Return ``{rId: (type, raw_target, target_mode)}`` for ``part``."""
        from .xmlread import parse_xml

        rels = rels_part_for(part)
        if not self.has(rels):
            return {}
        root = parse_xml(self.read(rels), part=rels)
        if root.tag != "Relationships":
            raise WorkbookPackageError(
                f"expected <Relationships>, found <{root.tag}>", rels
            )
        result = {}
        for item in root.children_named("Relationship"):
            rel_id = item.attrs.get("Id")
            target = item.attrs.get("Target")
            rel_type = item.attrs.get("Type", "")
            mode = item.attrs.get("TargetMode", "Internal")
            if not rel_id:
                raise WorkbookPackageError(
                    "relationship without an Id", rels
                )
            if rel_id in result:
                raise WorkbookPackageError(
                    f"duplicate relationship id {rel_id!r}", rels
                )
            if target is None:
                raise WorkbookPackageError(
                    f"relationship {rel_id!r} has no Target", rels
                )
            result[rel_id] = (rel_type, target, mode)
        return result


def find_workbook_part(package: Package) -> str:
    from .xmlread import parse_xml

    rels = package.relationships("")
    if not rels:
        raise WorkbookPackageError(
            "_rels/.rels is missing; cannot find the workbook part"
        )
    for rel_id, (rel_type, target, mode) in rels.items():
        if rel_type.endswith("/officeDocument"):
            if mode != "Internal":
                raise WorkbookPackageError(
                    "the office document relationship is external"
                )
            candidate = safe_resolve("", target, part="_rels/.rels")
            if not package.has(candidate):
                raise WorkbookPackageError(
                    f"workbook part {candidate!r} is missing", candidate
                )
            return candidate
    # Fall back to the conventional location only if the relationship names
    # it (no guessing): a package without an officeDocument relationship is
    # malformed.
    raise WorkbookPackageError(
        "_rels/.rels has no officeDocument relationship"
    )


# ---------------------------------------------------------------------------
# cell values and formulas
# ---------------------------------------------------------------------------

@dataclass
class RawCell:
    """One physical ``<c>`` element as read from a worksheet part."""

    row: int
    col: int
    kind: str                 # number/text/bool/error/blank
    value: Optional[Scalar]   # scalar, or the ("shared", index) placeholder
    formula: Optional[str]    # formula text without '='
    cached: Optional[str]     # raw <v> text next to a formula, for the record
    shared_index: Optional[int]
    style: Optional[str]
    # Declared coverage rectangle of a shared-formula master ("ref" attr).
    shared_ref: Optional[Tuple[int, int, int, int]] = None
    # Resolved AST for formula cells; filled by the shared-formula pass so a
    # member's translated formula is parsed/translated exactly once.
    shared_ast: Optional[object] = None


@dataclass
class Worksheet:
    name: str
    part: str
    cells: Dict[Tuple[int, int], RawCell] = field(default_factory=dict)
    merged: List[Tuple[int, int, int, int]] = field(default_factory=list)
    covered: Dict[Tuple[int, int], Tuple[int, int]] = field(default_factory=dict)
    max_row: int = 0
    max_col: int = 0
    sheet_id: Optional[str] = None
    hidden: bool = False


def _parse_cell_tokens(text: str, *, part: str, row: int) -> Tuple[int, int]:
    letters = []
    digits = []
    seen_digit = False
    for char in text:
        if char == "$":
            continue
        if char.isalpha():
            if seen_digit:
                raise WorkbookPackageError(
                    f"malformed cell reference {text!r}", part
                )
            letters.append(char)
        elif char.isdigit():
            seen_digit = True
            digits.append(char)
        else:
            raise WorkbookPackageError(
                f"malformed cell reference {text!r}", part
            )
    if not letters or not digits:
        raise WorkbookPackageError(
            f"cell reference {text!r} lacks a column or row", part
        )
    try:
        col = column_to_index("".join(letters))
    except RefError as exc:
        raise WorkbookPackageError(str(exc), part) from exc
    cell_row = int("".join(digits))
    if not in_grid(cell_row, col):
        raise WorkbookPackageError(
            f"cell reference {text!r} is outside XFD{MAX_ROW}", part
        )
    return cell_row, col


def _shared_strings(package: Package, workbook_part: str) -> List[str]:
    from .xmlread import parse_xml

    rels = package.relationships(workbook_part)
    target = None
    for _rel_id, (rel_type, raw_target, mode) in rels.items():
        if rel_type.endswith("/sharedStrings"):
            if mode != "Internal":
                raise WorkbookPackageError(
                    "sharedStrings relationship is external", workbook_part
                )
            target = safe_resolve(workbook_part, raw_target, part=workbook_part)
            break
    if target is None or not package.has(target):
        return []
    root = parse_xml(package.read(target), part=target)
    if root.tag != "sst":
        raise WorkbookPackageError(
            f"expected <sst>, found <{root.tag}>", target
        )
    strings = []
    for item in root.children_named("si"):
        strings.append(_rich_text(item))
    return strings


def _rich_text(element) -> str:
    """Concatenate direct text and every run's text, skipping phonetics."""
    parts = []
    for child in element.children:
        if child.tag == "t":
            parts.append(child.text)
        elif child.tag == "r":
            for grandchild in child.children:
                if grandchild.tag == "t":
                    parts.append(grandchild.text)
        elif child.tag == "rPh":
            # Phonetic hints are not part of the string value.
            continue
    return "".join(parts)


def _parse_bool_value(text: str, *, part: str, where: str) -> Bool:
    if text in ("1", "true", "TRUE"):
        return Bool(True)
    if text in ("0", "false", "FALSE"):
        return Bool(False)
    raise WorkbookPackageError(
        f"boolean cell {where} has invalid value {text!r}", part
    )


def _value_from_inline_string(cell, *, part: str, where: str) -> Scalar:
    inline = cell.child("is")
    if inline is None:
        return Text("")
    return Text(_rich_text(inline))


def _parse_worksheet(package: Package, sheet: Worksheet) -> None:
    from .xmlread import parse_xml

    root = parse_xml(package.read(sheet.part), part=sheet.part)
    if root.tag not in ("worksheet", "chartsheet", "dialogsheet"):
        raise WorkbookPackageError(
            f"expected a worksheet element, found <{root.tag}>", sheet.part
        )
    data = root.child("sheetData")
    shared_ids: Dict[int, List[Tuple[RawCell, str]]] = {}
    if data is not None:
        for row_element in data.children_named("row"):
            raw_row_text = row_element.attrs.get("r")
            row_number = None
            if raw_row_text is not None:
                if not raw_row_text.isdigit():
                    raise WorkbookPackageError(
                        f"row number {raw_row_text!r} is not an integer",
                        sheet.part,
                    )
                row_number = int(raw_row_text)
                if not (1 <= row_number <= MAX_ROW):
                    raise WorkbookPackageError(
                        f"row {row_number} is outside 1..{MAX_ROW}",
                        sheet.part,
                    )
            for cell in row_element.children_named("c"):
                ref_text = cell.attrs.get("r")
                if ref_text is None:
                    raise WorkbookPackageError(
                        "cell element without an 'r' coordinate; implicit "
                        "positions are not guessed", sheet.part
                    )
                cell_row, col = _parse_cell_tokens(
                    ref_text, part=sheet.part, row=row_number or 0
                )
                if row_number is not None and cell_row != row_number:
                    raise WorkbookPackageError(
                        f"cell {ref_text!r} is inside <row r=\"{row_number}\">",
                        sheet.part,
                    )
                where = f"{sheet.name}!{address(cell_row, col)}"
                key = (cell_row, col)
                if key in sheet.cells:
                    raise WorkbookPackageError(
                        f"duplicate cell coordinate {ref_text!r} in one sheet",
                        sheet.part,
                    )
                raw = _read_cell(cell, cell_row, col, sheet.part, where)
                sheet.cells[key] = raw
                sheet.max_row = max(sheet.max_row, cell_row)
                sheet.max_col = max(sheet.max_col, col)
                if raw.shared_index is not None:
                    shared_ids.setdefault(raw.shared_index, []).append(
                        (raw, where)
                    )
    _parse_merges(root, sheet)
    _resolve_shared_formulas(sheet, shared_ids)


def _read_cell(cell, row: int, col: int, part: str, where: str) -> RawCell:
    type_attr = cell.attrs.get("t", "n")
    style = cell.attrs.get("s")
    formula_element = cell.child("f")
    value_element = cell.child("v")
    formula_text = None
    shared_index = None
    cached = None
    if formula_element is not None:
        formula_type = formula_element.attrs.get("t", "normal")
        if formula_type == "shared":
            shared_text = formula_element.attrs.get("si")
            if shared_text is None or not shared_text.isdigit():
                raise WorkbookPackageError(
                    f"shared formula at {where} has no valid si", part
                )
            shared_index = int(shared_text)
        elif formula_type in ("array", "dataTable"):
            from .errors import WorkbookUnsupported

            sheet_label, _, cell_label = where.partition("!")
            raise WorkbookUnsupported(
                f"{'array' if formula_type == 'array' else 'data-table'} "
                "formulas are not supported",
                sheet_label, cell_label, f"{formula_type} formula",
            )
        elif formula_type != "normal":
            from .errors import WorkbookUnsupported

            sheet_label, _, cell_label = where.partition("!")
            raise WorkbookUnsupported(
                f"formula type {formula_type!r} is not supported",
                sheet_label, cell_label, formula_type,
            )
        formula_text = formula_element.text or ""
        if value_element is not None:
            cached = value_element.text or ""
        if not formula_text.strip() and formula_type != "shared":
            raise WorkbookPackageError(
                f"formula cell {where} has empty formula text", part
            )
    if formula_element is None:
        value = _constant_value(type_attr, value_element, cell, part, where)
        if isinstance(value, tuple) and value and value[0] == "shared":
            return RawCell(row, col, "text", value, None, None, None, style)
        return RawCell(row, col, value.kind, value, None, None, None, style)
    shared_ref = None
    if formula_element is not None and shared_index is not None \
            and (formula_element.text or "").strip():
        raw_ref = formula_element.attrs.get("ref")
        if raw_ref is not None:
            shared_ref = _parse_shared_ref(raw_ref, part, where)
    return RawCell(row, col, "formula", None, formula_text, cached,
                   shared_index, style, shared_ref)


def _parse_shared_ref(text: str, part: str, where: str):
    if ":" not in text:
        raise WorkbookPackageError(
            f"shared formula at {where} has invalid ref {text!r}", part
        )
    first, second = text.split(":", 1)
    row1, col1 = _parse_cell_tokens(first, part=part, row=0)
    row2, col2 = _parse_cell_tokens(second, part=part, row=0)
    top, bottom = sorted((row1, row2))
    left, right = sorted((col1, col2))
    return (top, left, bottom, right)


def _constant_value(type_attr: str, value_element, cell, part: str, where: str):
    """Decode a non-formula cell.  Returns a Scalar (BLANK for empty)."""
    if type_attr == "inlineStr":
        return _value_from_inline_string(cell, part=part, where=where)
    if value_element is None:
        return BLANK
    text = value_element.text or ""
    if type_attr == "s":
        return ("shared", text)  # resolved after the sst is loaded
    if type_attr == "str":
        return Text(text)
    if type_attr in ("n", ""):
        if text.strip() == "":
            return BLANK
        try:
            return Number(parse_decimal_literal(text.strip()))
        except (ValueError, ArithmeticError) as exc:
            raise WorkbookPackageError(
                f"numeric cell {where} has invalid value {text!r}: {exc}", part
            ) from exc
    if type_attr == "b":
        return _parse_bool_value(text.strip(), part=part, where=where)
    if type_attr == "e":
        code = text.strip()
        if code not in KNOWN_ERROR_CODES:
            raise WorkbookPackageError(
                f"error cell {where} has unknown error code {code!r}", part
            )
        return make_error(code, "error literal stored in the workbook", where)
    if type_attr == "d":
        return Text(text)
    raise WorkbookPackageError(
        f"cell {where} has unsupported type t={type_attr!r}", part
    )


def _parse_merges(root, sheet: Worksheet) -> None:
    merge_cells = root.child("mergeCells")
    if merge_cells is None:
        return
    for item in merge_cells.children_named("mergeCell"):
        ref = item.attrs.get("ref", "")
        if ":" not in ref:
            raise WorkbookPackageError(
                f"mergeCell reference {ref!r} is not a range", sheet.part
            )
        first, second = ref.split(":", 1)
        try:
            row1, col1 = _parse_cell_tokens(first, part=sheet.part, row=0)
            row2, col2 = _parse_cell_tokens(second, part=sheet.part, row=0)
        except WorkbookPackageError:
            raise
        top, bottom = sorted((row1, row2))
        left, right = sorted((col1, col2))
        sheet.merged.append((top, left, bottom, right))
        for row in range(top, bottom + 1):
            for col in range(left, right + 1):
                if (row, col) == (top, left):
                    continue
                if (row, col) in sheet.covered:
                    raise WorkbookPackageError(
                        f"cell {address(row, col)} is covered by two merges",
                        sheet.part,
                    )
                sheet.covered[(row, col)] = (top, left)


def _resolve_shared_formulas(sheet: Worksheet, shared_ids) -> None:
    """Expand shared formulas, independent of XML element order."""
    if not shared_ids:
        return
    problems = []
    for shared_index, members in shared_ids.items():
        masters = [
            (cell, where) for cell, where in members if cell.formula
            and cell.formula.strip()
        ]
        if len(masters) == 0:
            for cell, where in members:
                problems.append((
                    cell,
                    where,
                    f"shared formula si={shared_index} has no master formula",
                ))
            continue
        if len(masters) > 1:
            for cell, where in members:
                problems.append((
                    cell,
                    where,
                    f"shared formula si={shared_index} has "
                    f"{len(masters)} master formulas ({masters[0][1]}, "
                    f"{masters[1][1]})",
                ))
            continue
        master_cell, master_where = masters[0]
        if master_cell.shared_ref is not None:
            top, left, bottom, right = master_cell.shared_ref
            for cell, where in members:
                if not (top <= cell.row <= bottom and left <= cell.col <= right):
                    problems.append((
                        cell, where,
                        f"shared formula si={shared_index} member "
                        f"{address(cell.row, cell.col)} lies outside the "
                        f"master's declared ref "
                        f"{address(top, left)}:{address(bottom, right)}",
                    ))
        try:
            master_ast = parse_formula(
                master_cell.formula, sheet=sheet.name, address=address(
                    master_cell.row, master_cell.col)
            )
        except WorkbookFormulaError as exc:
            for cell, where in members:
                problems.append((cell, where, f"master formula is invalid: {exc}"))
            continue
        for cell, where in members:
            if cell is master_cell:
                _attach_shared_ast(cell, master_ast)
                continue
            if cell.formula and cell.formula.strip():
                problems.append((
                    cell, where,
                    f"shared formula si={shared_index} has more than one "
                    "master formula",
                ))
                continue
            row_delta = cell.row - master_cell.row
            col_delta = cell.col - master_cell.col
            translated = translate_ref_guarded(
                master_ast, row_delta, col_delta,
                sheet=sheet.name, address=address(cell.row, cell.col),
            )
            _attach_shared_ast(cell, translated)
    if problems:
        cell, where, message = problems[0]
        raise WorkbookFormulaError(message, sheet=sheet.name,
                                   address=address(cell.row, cell.col))


def _attach_shared_ast(cell: RawCell, ast) -> None:
    cell.shared_ast = ast


def shared_formula_source(workbook: Workbook, sheet_name: str, row: int,
                          col: int):
    """Return ``(master_address, translated_formula_text, is_master)``.

    The translated text is rendered from the parsed master AST, so it shows
    what the member really computes (relative parts shifted, absolute parts
    unchanged).  Returns ``None`` for cells that are not shared members.
    """
    sheet = workbook.by_name[sheet_name]
    raw = sheet.cells.get((row, col))
    if raw is None or raw.shared_index is None:
        return None
    master = None
    for (master_row, master_col), candidate in sheet.cells.items():
        if candidate.shared_index == raw.shared_index and \
                candidate.formula and candidate.formula.strip():
            master = candidate
            break
    if master is None:
        return None
    from .formula import unparse

    is_master = master.row == row and master.col == col
    if is_master:
        return (address(master.row, master.col), "=" + master.formula, True)
    return (address(master.row, master.col),
            unparse(raw.shared_ast), False)


@dataclass
class Workbook:
    path: str
    sheets: List[Worksheet]
    by_name: Dict[str, Worksheet]
    shared_strings: List[str]
    workbook_part: str


def load_workbook(path: str) -> Workbook:
    """Read the package, resolve sheets through relationships and cache cells."""
    from .xmlread import parse_xml

    package = Package(path)
    try:
        workbook_part = find_workbook_part(package)
        root = parse_xml(package.read(workbook_part), part=workbook_part)
        if root.tag != "workbook":
            raise WorkbookPackageError(
                f"expected <workbook>, found <{root.tag}>", workbook_part
            )
        sheet_elements = []
        sheets_element = root.child("sheets")
        if sheets_element is None:
            raise WorkbookPackageError(
                "workbook has no <sheets> element", workbook_part
            )
        rels = package.relationships(workbook_part)
        seen_names = {}
        for item in sheets_element.children_named("sheet"):
            name = item.attrs.get("name")
            rel_id = item.attrs.get("id")
            if not name:
                raise WorkbookPackageError(
                    "worksheet without a name", workbook_part
                )
            if rel_id is None:
                raise WorkbookPackageError(
                    f"worksheet {name!r} has no relationship id",
                    workbook_part,
                )
            if rel_id not in rels:
                raise WorkbookPackageError(
                    f"worksheet {name!r} points at unknown relationship "
                    f"{rel_id!r}", workbook_part,
                )
            pattern = name.lower()
            if pattern in seen_names:
                raise WorkbookPackageError(
                    f"sheet names {seen_names[pattern]!r} and {name!r} "
                    "collide after case folding", workbook_part,
                )
            seen_names[pattern] = name
            rel_type, target, mode = rels[rel_id]
            if mode != "Internal":
                raise WorkbookPackageError(
                    f"worksheet {name!r} relationship is external",
                    workbook_part,
                )
            part = safe_resolve(workbook_part, target, part=workbook_part)
            if not package.has(part):
                raise WorkbookPackageError(
                    f"worksheet {name!r} part is missing", part
                )
            state = item.attrs.get("state", "visible")
            sheet_elements.append((name, part, state, item))
        strings = _shared_strings(package, workbook_part)
        worksheets = []
        for name, part, state, item in sheet_elements:
            sheet = Worksheet(name=name, part=part,
                              hidden=(state != "visible"))
            _parse_worksheet(package, sheet)
            _resolve_shared_values(sheet, strings)
            worksheets.append(sheet)
        by_name = {sheet.name: sheet for sheet in worksheets}
        return Workbook(path, worksheets, by_name, strings, workbook_part)
    finally:
        package.close()


def _resolve_shared_values(sheet: Worksheet, strings: List[str]) -> None:
    """Replace the ``("shared", index)`` placeholder with a real Scalar."""
    for key, cell in sheet.cells.items():
        if isinstance(cell.value, tuple) and cell.value and cell.value[0] == "shared":
            text = cell.value[1]
            if text.strip() == "" or not text.strip().lstrip("-").isdigit():
                raise WorkbookPackageError(
                    f"shared string index {text!r} is not a valid integer",
                    sheet.part,
                )
            index = int(text.strip())
            if not strings:
                raise WorkbookPackageError(
                    f"cell {key} references shared string {index} but the "
                    "package has no sharedStrings part", sheet.part,
                )
            if index < 0 or index >= len(strings):
                raise WorkbookPackageError(
                    f"shared string index {index} is out of range "
                    f"(0..{len(strings) - 1})", sheet.part,
                )
            cell.value = Text(strings[index])
            cell.kind = "text"
