"""Lexer and parser for the documented workbook recalculation subset.

The subset is intentionally small and is **not** a claim of Excel
compatibility.  It supports:

* decimal number literals (``1``, ``2.5``, ``1e-3``);
* double-quoted text with ``""`` as the escaped quote;
* ``TRUE`` / ``FALSE``;
* parentheses, unary ``+``/``-``;
* ``+ - * /`` and the comparisons ``= <> < <= > >=``;
* A1 references with optional ``$`` markers, same-sheet references,
  ``Sheet!A1`` references and rectangular ranges ``A1:B2``;
* quoted sheet names (``'P&L 2024'!A1``, ``''`` escapes one quote);
* the functions ``SUM``, ``MIN``, ``MAX``, ``COUNT``, ``IF`` and ``IFERROR``
  (case-insensitive names).

Anything else -- external workbook references, defined names, array
formulas, structured table references, volatile functions, other functions,
the intersection/union operators -- is rejected with the offending cell and
the construct, never guessed at and never evaluated by a partial regex.

The parser is recursive descent over the *whole* formula text and fails if
any trailing token remains, so a formula is either fully understood or
explicitly refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .errors import WorkbookFormulaError, WorkbookUnsupported
from .refs import MAX_COLUMN, MAX_ROW, RefError
from .values import parse_decimal_literal


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Token:
    kind: str          # NUMBER TEXT BOOL CELL ERROR NAME OP LPAREN RPAREN COMMA COLON
    text: str
    value: object
    pos: int


_OP_CHARS = set("+-*/<>=()[],:")
_WHITESPACE = " \t\n\r"


class _CharStream:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def advance(self, count: int = 1) -> str:
        chunk = self.text[self.pos:self.pos + count]
        self.pos += count
        return chunk


def tokenize(text: str, *, sheet: str = None, address: str = None) -> List[Token]:
    stream = _CharStream(text)
    tokens: List[Token] = []
    length = len(text)
    while stream.pos < length:
        char = stream.peek()
        start = stream.pos
        if char in _WHITESPACE:
            stream.advance()
            continue
        if char == '"':
            tokens.append(_read_text(stream, sheet, address))
            continue
        if char == "'":
            tokens.append(_read_quoted_sheet(stream, sheet, address))
            continue
        if char.isdigit() or (char == "." and stream.pos + 1 < length
                              and text[stream.pos + 1].isdigit()):
            tokens.append(_read_number(stream, sheet, address))
            continue
        if char.isalpha() or char in "$_":
            tokens.append(_read_word(stream, sheet, address))
            continue
        if char in "()" or char == ",":
            tokens.append(Token({
                "(": "LPAREN", ")": "RPAREN", ",": "COMMA"
            }[char], char, None, start))
            stream.advance()
            continue
        if char in "<>":
            two = stream.text[stream.pos:stream.pos + 2]
            if two in ("<=", ">=", "<>"):
                tokens.append(Token("OP", two, None, start))
                stream.advance(2)
            else:
                tokens.append(Token("OP", char, None, start))
                stream.advance()
            continue
        if char == "=":
            tokens.append(Token("OP", "=", None, start))
            stream.advance()
            continue
        if char in "+-*/":
            tokens.append(Token("OP", char, None, start))
            stream.advance()
            continue
        if char == ":":
            tokens.append(Token("COLON", ":", None, start))
            stream.advance()
            continue
        if char == "#":
            match = re.match(r"#[A-Za-z0-9/?!_]+!?", text[stream.pos:])
            if not match:
                raise WorkbookFormulaError(
                    "malformed error literal", sheet, address
                )
            literal = match.group(0)
            tokens.append(Token("ERROR", literal, literal, start))
            stream.advance(len(literal))
            continue
        if char == "[":
            raise WorkbookUnsupported(
                "structured table / external workbook reference is not supported",
                sheet, address, "structured or external reference",
            )
        if char == "!":
            raise WorkbookUnsupported(
                "unexpected '!' -- external or 3-D references are not supported",
                sheet, address, "external reference",
            )
        if char == "&":
            raise WorkbookUnsupported(
                "text concatenation is not in the supported subset",
                sheet, address, "&",
            )
        if char == "\\":
            raise WorkbookUnsupported(
                "backslash escapes are not Excel formula syntax", sheet,
                address, "backslash",
            )
        raise WorkbookUnsupported(
            f"character {char!r} is not in the supported formula subset",
            sheet, address, "formula character",
        )
    tokens.append(Token("EOF", "", None, length))
    return tokens


def _read_text(stream: _CharStream, sheet, address) -> Token:
    start = stream.pos
    stream.advance()  # opening quote
    parts = []
    while True:
        char = stream.peek()
        if char == "":
            raise WorkbookFormulaError(
                "unterminated text literal", sheet, address
            )
        if char == '"':
            stream.advance()
            if stream.peek() == '"':
                parts.append('"')
                stream.advance()
                continue
            break
        parts.append(char)
        stream.advance()
    return Token("TEXT", "".join(parts), "".join(parts), start)


def _read_quoted_sheet(stream: _CharStream, sheet, address) -> Token:
    """Read ``'Quoted Name'`` followed by ``!`` and keep the name text."""
    start = stream.pos
    stream.advance()
    parts = []
    while True:
        char = stream.peek()
        if char == "":
            raise WorkbookFormulaError(
                "unterminated quoted sheet name", sheet, address
            )
        if char == "'":
            stream.advance()
            if stream.peek() == "'":
                parts.append("'")
                stream.advance()
                continue
            break
        parts.append(char)
        stream.advance()
    if stream.peek() != "!":
        raise WorkbookUnsupported(
            "quoted text not followed by '!' is not a supported reference",
            sheet, address, "quoted name",
        )
    stream.advance()
    return Token("SHEET", "".join(parts), "".join(parts), start)


def _read_number(stream: _CharStream, sheet, address) -> Token:
    start = stream.pos
    text = stream.text
    pos = start
    length = len(text)
    if text[pos] == ".":
        pos += 1
        while pos < length and text[pos].isdigit():
            pos += 1
    else:
        while pos < length and text[pos].isdigit():
            pos += 1
        if pos < length and text[pos] == ".":
            pos += 1
            while pos < length and text[pos].isdigit():
                pos += 1
    if pos < length and text[pos] in "eE":
        exp_start = pos
        pos += 1
        if pos < length and text[pos] in "+-":
            pos += 1
        digits_start = pos
        while pos < length and text[pos].isdigit():
            pos += 1
        if digits_start == pos:
            pos = exp_start
    literal = text[start:pos]
    stream.pos = pos
    try:
        value = parse_decimal_literal(literal)
    except ValueError as exc:
        raise WorkbookFormulaError(str(exc), sheet, address) from exc
    return Token("NUMBER", literal, value, start)


_NUMBER_RE = None


def _read_word(stream: _CharStream, sheet, address) -> Token:
    start = stream.pos
    text = stream.text
    pos = start
    length = len(text)
    # ``$`` may prefix a column and/or row marker.  The word ends at the
    # first character that cannot be part of a cell reference or a name.
    while pos < length and (text[pos].isalnum() or text[pos] in "_.$"):
        pos += 1
    word = text[start:pos]
    if pos == start:  # a lone '$' with nothing after it
        raise WorkbookUnsupported(
            "dangling reference marker in formula", sheet, address,
            "reference",
        )
    stream.pos = pos
    if stream.peek() == "!":
        # Unquoted sheet name followed by '!'.  It must look like a name and
        # is resolved case-insensitively by the evaluator.
        stream.advance()
        if not _valid_unquoted_sheet(word):
            raise WorkbookUnsupported(
                f"invalid sheet name {word!r}", sheet, address, "sheet name"
            )
        return Token("SHEET", word, word, start)
    if stream.peek() == "(":
        # Function call: the name is validated by the parser.
        stream.advance()
        return Token("FUNC", word, word, start)
    if stream.peek() == "[":
        raise WorkbookUnsupported(
            f"structured table reference {word}[...] is not supported",
            sheet, address, "structured reference",
        )
    if word.upper() in ("TRUE", "FALSE"):
        return Token("BOOL", word, word.upper() == "TRUE", start)
    if word.startswith("#"):
        return Token("ERROR", word, word, start)
    cell = _try_parse_cell(word)
    if cell is not None:
        return Token("CELL", word, cell, start)
    if word.startswith("$") or word[0].isdigit():
        raise WorkbookFormulaError(
            f"malformed cell reference {word!r}", sheet, address
        )
    # Whole-column / whole-row tokens (``A:A``, ``1:1``) reach here as bare
    # letters; report them as unsupported range forms rather than names.
    if len(word) <= 3 and word.isalpha():
        raise WorkbookUnsupported(
            f"whole-column or whole-row reference {word!r} is not supported",
            sheet, address, "unbounded range",
        )
    raise WorkbookUnsupported(
        f"name {word!r} (defined names are not supported)", sheet, address,
        "defined name",
    )


def _valid_unquoted_sheet(name: str) -> bool:
    if not name:
        return False
    if name[0].isdigit():
        return False
    return all(ch.isalnum() or ch in "_.$" for ch in name)


def _try_parse_cell(word: str):
    from .refs import parse_cell_text

    try:
        row, col, abs_row, abs_col = parse_cell_text(word)
    except RefError:
        return None
    return (row, col, abs_row, abs_col)


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Literal:
    value: object


@dataclass(frozen=True)
class CellRef:
    sheet: Optional[str]
    row: int
    col: int
    abs_row: bool
    abs_col: bool


@dataclass(frozen=True)
class RangeRef:
    sheet: Optional[str]
    top: int
    left: int
    bottom: int
    right: int
    top_row_abs: bool = True
    bottom_row_abs: bool = True
    left_col_abs: bool = True
    right_col_abs: bool = True

    def translated(self, row_delta: int, col_delta: int):
        top = self.top if self.top_row_abs else self.top + row_delta
        bottom = self.bottom if self.bottom_row_abs else self.bottom + row_delta
        left = self.left if self.left_col_abs else self.left + col_delta
        right = self.right if self.right_col_abs else self.right + col_delta
        return RangeRef(
            self.sheet, top, left, bottom, right,
            self.top_row_abs, self.bottom_row_abs,
            self.left_col_abs, self.right_col_abs,
        )


@dataclass(frozen=True)
class Unary:
    op: str
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class FunctionCall:
    name: str
    args: Tuple


@dataclass(frozen=True)
class BadReference:
    """A syntactically valid reference that lies outside the XFD/1048576 grid.

    Kept as a node so it evaluates to ``#REF!`` with a *location* instead of
    failing the whole job at parse time, while evaluation never touches a
    non-existent cell.
    """

    reason: str


# Functions and their arity rules are documented in docs/WORKBOOK.md.
_SCALAR_FUNCS = {"SUM", "MIN", "MAX", "COUNT"}
_SUPPORTED = _SCALAR_FUNCS | {"IF", "IFERROR"}
_VOLATILE = {
    "RAND", "RANDBETWEEN", "NOW", "TODAY", "OFFSET", "INDIRECT", "CELL",
    "INFO", "AREAS", "ROW", "COLUMN", "ROWS", "COLUMNS",
}


class Parser:
    def __init__(self, tokens: List[Token], *, sheet: str, address: str):
        self.tokens = tokens
        self.index = 0
        self.sheet = sheet
        self.address = address

    # -- helpers --------------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        idx = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def take(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def expect(self, kind: str) -> Token:
        token = self.peek()
        if token.kind != kind:
            raise self.fail(f"expected {kind}, found {token.text or 'end of formula'!r}")
        return self.take()

    def fail(self, message: str) -> WorkbookFormulaError:
        return WorkbookFormulaError(message, self.sheet, self.address)

    def unsupported(self, message: str, construct: str):
        return WorkbookUnsupported(message, self.sheet, self.address, construct)

    # -- grammar --------------------------------------------------------
    def parse(self):
        node = self.parse_comparison()
        token = self.peek()
        if token.kind != "EOF":
            raise self.fail(
                f"unexpected token {token.text!r} after a complete expression"
            )
        return node

    def parse_comparison(self):
        node = self.parse_additive()
        while self.peek().kind == "OP" and self.peek().text in (
                "=", "<>", "<", "<=", ">", ">="):
            op = self.take().text
            node = Binary(op, node, self.parse_additive())
        return node

    def parse_additive(self):
        node = self.parse_multiplicative()
        while self.peek().kind == "OP" and self.peek().text in ("+", "-"):
            op = self.take().text
            node = Binary(op, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self):
        node = self.parse_unary()
        while self.peek().kind == "OP" and self.peek().text in ("*", "/"):
            op = self.take().text
            node = Binary(op, node, self.parse_unary())
        return node

    def parse_unary(self):
        token = self.peek()
        if token.kind == "OP" and token.text in ("+", "-"):
            self.take()
            return Unary(token.text, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        token = self.peek()
        if token.kind == "NUMBER":
            self.take()
            return Literal(("number", token.value))
        if token.kind == "TEXT":
            self.take()
            return Literal(("text", token.value))
        if token.kind == "BOOL":
            self.take()
            return Literal(("bool", token.value))
        if token.kind == "ERROR":
            raise self.unsupported(
                f"error literal {token.text!r} is not in the supported subset",
                token.text,
            )
        if token.kind == "LPAREN":
            self.take()
            node = self.parse_comparison()
            self.expect("RPAREN")
            return node
        if token.kind == "FUNC":
            return self.parse_function()
        if token.kind == "CELL":
            return self.parse_reference(None)
        if token.kind == "SHEET":
            sheet_name = self.take().text
            return self.parse_reference(sheet_name)
        if token.kind == "OP" and token.text in ("=", "<", "<=", ">", ">=", "<>"):
            raise self.fail("comparison operator without a left operand")
        if token.kind == "EOF":
            raise self.fail("formula ended where a value was expected")
        raise self.fail(f"unexpected token {token.text!r}")

    def parse_reference(self, sheet_name: Optional[str]):
        token = self.expect("CELL")
        row, col, abs_row, abs_col = token.value
        if self.peek().kind == "COLON":
            self.take()
            second = self.peek()
            if second.kind != "CELL":
                raise self.fail(
                    "range endpoint is not a cell reference; whole-column, "
                    "whole-row and multi-area references are not supported"
                )
            self.take()
            row2, col2, abs_row2, abs_col2 = second.value
            if not (1 <= row <= MAX_ROW and 1 <= row2 <= MAX_ROW
                    and 1 <= col <= MAX_COLUMN and 1 <= col2 <= MAX_COLUMN):
                return BadReference("range endpoint outside XFD1048576")
            top, bottom = sorted((row, row2))
            left, right = sorted((col, col2))
            return RangeRef(
                sheet_name, top, left, bottom, right,
                top_row_abs=abs_row if row <= row2 else abs_row2,
                bottom_row_abs=abs_row2 if row <= row2 else abs_row,
                left_col_abs=abs_col if col <= col2 else abs_col2,
                right_col_abs=abs_col2 if col <= col2 else abs_col,
            )
        if not (1 <= row <= MAX_ROW and 1 <= col <= MAX_COLUMN):
            return BadReference("cell outside XFD1048576")
        return CellRef(sheet_name, row, col, abs_row, abs_col)

    def parse_function(self):
        token = self.take()
        name = token.text.upper()
        if name in _VOLATILE:
            raise self.unsupported(
                f"function {name} is not supported", name
            )
        if name not in _SUPPORTED:
            raise self.unsupported(
                f"function {name} is not supported", name
            )
        args = []
        if self.peek().kind == "RPAREN":
            self.take()
        else:
            while True:
                args.append(self.parse_comparison())
                if self.peek().kind == "COMMA":
                    self.take()
                    if self.peek().kind in ("RPAREN", "COMMA"):
                        raise self.fail(
                            "empty argument is not supported"
                        )
                    continue
                break
            self.expect("RPAREN")
        if name == "IF" and len(args) not in (2, 3):
            raise self.fail(f"IF takes 2 or 3 arguments, got {len(args)}")
        if name == "IFERROR" and len(args) != 2:
            raise self.fail(f"IFERROR takes 2 arguments, got {len(args)}")
        return FunctionCall(name, tuple(args))


def parse_formula(text: str, *, sheet: str = None, address: str = None):
    """Parse one formula body (without the leading ``=``).

    Parenthesis nesting is the only recursive part of the parser; a formula
    nested deeper than the interpreter allows is reported as a formula error
    for that cell instead of escaping as a raw ``RecursionError``.
    """
    if not isinstance(text, str):
        raise WorkbookFormulaError("formula is not text", sheet, address)
    if text.startswith("="):
        text = text[1:]
    if not text.strip():
        raise WorkbookFormulaError("empty formula", sheet, address)
    tokens = tokenize(text, sheet=sheet, address=address)
    try:
        return Parser(tokens, sheet=sheet, address=address).parse()
    except RecursionError as exc:
        raise WorkbookFormulaError(
            "formula nesting is too deep to parse", sheet, address
        ) from exc


# ---------------------------------------------------------------------------
# reference translation for shared formulas
# ---------------------------------------------------------------------------

def translate_ref(node, row_delta: int, col_delta: int, *, sheet, address):
    """Shift the relative parts of every reference in ``node`` by deltas.

    ``row_delta``/``col_delta`` are the member-minus-master offsets.  Absolute
    row/column markers are left untouched, and a range translates both
    endpoints independently.  A translated reference outside the grid becomes
    a :class:`BadReference` (which evaluates to ``#REF!``) rather than an
    exception, matching how the evaluator treats any out-of-grid reference.
    """
    if isinstance(node, CellRef):
        row = node.row if node.abs_row else node.row + row_delta
        col = node.col if node.abs_col else node.col + col_delta
        if not (1 <= row <= MAX_ROW and 1 <= col <= MAX_COLUMN):
            return BadReference(
                "shared-formula translation moved reference outside XFD1048576"
            )
        return CellRef(node.sheet, row, col, node.abs_row, node.abs_col)
    if isinstance(node, RangeRef):
        translated = node.translated(row_delta, col_delta)
        if not (1 <= translated.top <= MAX_ROW
                and 1 <= translated.bottom <= MAX_ROW
                and 1 <= translated.left <= MAX_COLUMN
                and 1 <= translated.right <= MAX_COLUMN):
            return BadReference(
                "shared-formula translation moved range outside XFD1048576"
            )
        return translated
    if isinstance(node, Unary):
        return Unary(node.op, translate_ref(node.operand, row_delta, col_delta,
                                           sheet=sheet, address=address))
    if isinstance(node, Binary):
        return Binary(
            node.op,
            translate_ref(node.left, row_delta, col_delta, sheet=sheet, address=address),
            translate_ref(node.right, row_delta, col_delta, sheet=sheet, address=address),
        )
    if isinstance(node, FunctionCall):
        return FunctionCall(
            node.name,
            tuple(translate_ref(arg, row_delta, col_delta, sheet=sheet, address=address)
                  for arg in node.args),
        )
    return node


def translate_ref_guarded(node, row_delta: int, col_delta: int, *,
                          sheet, address):
    """``translate_ref`` with a deep-expression guard for shared members."""
    try:
        return translate_ref(node, row_delta, col_delta, sheet=sheet,
                             address=address)
    except RecursionError as exc:
        raise WorkbookFormulaError(
            "shared formula is nested too deeply to translate", sheet, address
        ) from exc


def unparse(node) -> str:
    """Render an AST back to formula text (used for shared-formula provenance)."""
    try:
        return _unparse(node)
    except RecursionError:
        return "<formula nested too deeply to render>"


def _unparse(node) -> str:
    if isinstance(node, Literal):
        kind, payload = node.value
        if kind == "number":
            from .values import decimal_text

            return decimal_text(payload)
        if kind == "text":
            return '"' + payload.replace('"', '""') + '"'
        if kind == "bool":
            return "TRUE" if payload else "FALSE"
        return str(payload)
    if isinstance(node, BadReference):
        return "#REF!"
    if isinstance(node, CellRef):
        from .refs import index_to_column, quote_sheet

        col = index_to_column(node.col)
        text = ("$" if node.abs_col else "") + col + \
               ("$" if node.abs_row else "") + str(node.row)
        if node.sheet is not None and quote_sheet(node.sheet) != node.sheet:
            text = quote_sheet(node.sheet) + "!" + text
        elif node.sheet is not None:
            text = node.sheet + "!" + text
        return text
    if isinstance(node, RangeRef):
        from .refs import index_to_column, quote_sheet

        def corner(row, col, abs_row, abs_col):
            return ("$" if abs_col else "") + index_to_column(col) + \
                   ("$" if abs_row else "") + str(row)

        text = corner(node.top, node.left, node.top_row_abs,
                      node.left_col_abs) + ":" + \
            corner(node.bottom, node.right, node.bottom_row_abs,
                   node.right_col_abs)
        if node.sheet is not None and quote_sheet(node.sheet) != node.sheet:
            text = quote_sheet(node.sheet) + "!" + text
        elif node.sheet is not None:
            text = node.sheet + "!" + text
        return text
    if isinstance(node, Unary):
        return "(" + node.op + _unparse(node.operand) + ")"
    if isinstance(node, Binary):
        return "(" + _unparse(node.left) + node.op + _unparse(node.right) + ")"
    if isinstance(node, FunctionCall):
        return node.name + "(" + ",".join(_unparse(arg) for arg in node.args) + ")"
    return "?"  # pragma: no cover - closed AST


def validate_ast(node, *, sheet: str, address: str) -> None:
    """Reject structurally valid but semantically unsupported constructs.

    The only reference position this subset supports is a *direct argument*
    of SUM/MIN/MAX/COUNT; a bare range used as a scalar is refused before any
    row is produced, so it is a job-level failure in both delivery modes.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, RangeRef):
            raise WorkbookUnsupported(
                "a range reference is only supported as a direct argument of "
                "SUM, MIN, MAX or COUNT", sheet, address, "range as scalar"
            )
        elif isinstance(current, Unary):
            stack.append(current.operand)
        elif isinstance(current, Binary):
            stack.extend((current.right, current.left))
        elif isinstance(current, FunctionCall):
            if current.name in _SCALAR_FUNCS:
                for argument in current.args:
                    if isinstance(argument, RangeRef):
                        continue
                    stack.append(argument)
            else:
                stack.extend(current.args)


def iter_references(node):
    """Yield reference AST nodes, left-to-right in formula order."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (CellRef, RangeRef, BadReference)):
            yield current
        elif isinstance(current, Unary):
            stack.append(current.operand)
        elif isinstance(current, Binary):
            stack.append(current.right)
            stack.append(current.left)
        elif isinstance(current, FunctionCall):
            stack.extend(reversed(current.args))
