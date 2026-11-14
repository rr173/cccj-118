"""Parser for the restricted COBOL copybook subset understood by distagg.

This is deliberately **not** a full COBOL parser.  It accepts exactly the
subset documented in ``docs/COPYBOOK.md`` and rejects everything else with
the copybook line and field:

* one ``01`` root group; nested groups and fields at levels ``02``-``49``;
* ``FILLER`` placeholders (elementary items only);
* ``PIC X(n)`` text, ``PIC 9(n)`` unsigned display numerics and
  ``PIC [S]9(n)V9(m)`` numerics with an implied decimal point;
* ``USAGE DISPLAY`` (default), ``COMP`` and ``COMP-3`` — signed DISPLAY
  numerics are refused so the decoder never has to guess a zoned sign;
* fixed ``OCCURS n`` and ``OCCURS min TO max DEPENDING ON full.path``;
* ``REDEFINES`` of the immediately preceding item in the same group.

Format rules: free format, case-insensitive, every declaration ends with a
period, ``*>`` starts a comment that runs to the end of the line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .errors import CopybookSyntaxError, UnsupportedError

_NAME_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?$", re.IGNORECASE)
_LEVEL_RE = re.compile(r"^\d{1,2}$")
_PIC_ELEMENT_RE = re.compile(r"([A-Z0-9])(?:\((\d+)\))?")

# Clause-leading keywords that are recognised only to be rejected with a
# precise "unsupported" message (they must not be silently ignored).
_UNSUPPORTED_CLAUSES = {
    "VALUE": "VALUE clauses are not supported (this entry decodes records, it does not initialise them)",
    "VALUES": "VALUE clauses are not supported (this entry decodes records, it does not initialise them)",
    "JUSTIFIED": "JUSTIFIED is not supported",
    "JUST": "JUSTIFIED is not supported",
    "BLANK": "BLANK WHEN ZERO is not supported",
    "SIGN": "SIGN clauses are not supported (signed DISPLAY numerics are refused entirely)",
    "SYNCHRONIZED": "SYNCHRONIZED is not supported (records are decoded packed, as exported)",
    "SYNC": "SYNCHRONIZED is not supported (records are decoded packed, as exported)",
    "RENAMES": "level 66 RENAMES is not supported",
    "GLOBAL": "GLOBAL is not supported",
    "EXTERNAL": "EXTERNAL is not supported",
    "LEADING": "SIGN LEADING/TRAILING is not supported",
    "TRAILING": "SIGN LEADING/TRAILING is not supported",
    "SEPARATE": "SIGN SEPARATE is not supported",
    "WHEN": "BLANK WHEN ZERO is not supported",
    "ZERO": "BLANK WHEN ZERO is not supported",
}

_USAGE_ALIASES = {
    "DISPLAY": "DISPLAY",
    "COMP": "COMP",
    "COMPUTATIONAL": "COMP",
    "BINARY": "COMP",
    "COMP-3": "COMP-3",
    "COMPUTATIONAL-3": "COMP-3",
    "PACKED-DECIMAL": "COMP-3",
}


@dataclass
class Pic:
    """A parsed PICTURE string.

    ``category`` is ``"text"`` or ``"numeric"``.  For numerics ``digits`` is
    the number of integer digits and ``scale`` the number of implied decimal
    places (the ``V9(m)`` part); the stored/declared value is always the
    integer ``digits`` followed by ``scale`` fraction digits.
    """

    category: str           # "text" | "numeric"
    length: int             # text: number of bytes
    digits: int = 0         # numeric: integer digits
    scale: int = 0          # numeric: implied decimal places
    signed: bool = False    # numeric: S present
    raw: str = ""

    @property
    def total_digits(self) -> int:
        return self.digits + self.scale


@dataclass
class Occurs:
    min_times: int
    max_times: int
    depending_on: Optional[str] = None   # full field path, already upper-cased

    @property
    def variable(self) -> bool:
        return self.depending_on is not None


@dataclass
class RawDecl:
    """One parsed declaration, still flat (no tree built yet)."""

    level: int
    name: Optional[str]          # None for FILLER
    line: int                    # copybook line of the level number
    pic: Optional[Pic] = None
    usage: str = "DISPLAY"
    occurs: Optional[Occurs] = None
    redefines: Optional[str] = None
    clauses: List[str] = field(default_factory=list)  # clause keywords seen, in order

    @property
    def display_name(self) -> str:
        return self.name if self.name is not None else "FILLER"


def _tokenize(source: str) -> List[Tuple[str, int]]:
    """Split copybook text into (word, line) tokens.

    A period only terminates a declaration when it is not immediately
    followed by more word characters, so qualified paths such as
    ``ORDER-RECORD.DETAIL-COUNT`` stay single tokens.
    """
    tokens: List[Tuple[str, int]] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        comment = line.find("*>")
        if comment >= 0:
            line = line[:comment]
        for match in re.finditer(r"[^\s.]+(?:\.[^\s.]+)*|\.", line):
            tokens.append((match.group(0), lineno))
    return tokens


def _split_declarations(tokens: List[Tuple[str, int]]) -> List[List[Tuple[str, int]]]:
    decls: List[List[Tuple[str, int]]] = []
    current: List[Tuple[str, int]] = []
    for word, line in tokens:
        if word == ".":
            if not current:
                raise CopybookSyntaxError("unexpected '.' with no declaration before it", line=line)
            decls.append(current)
            current = []
        else:
            current.append((word, line))
    if current:
        word, line = current[-1]
        raise CopybookSyntaxError(
            f"declaration starting at line {current[0][1]} is not terminated by a period",
            line=line,
        )
    return decls


def parse_pic(text: str, line: int, field_name: str) -> Pic:
    """Parse one PICTURE string restricted to the supported subset."""
    raw = text
    upper = text.upper()
    pos = 0
    elements: List[Tuple[str, int]] = []
    while pos < len(upper):
        match = _PIC_ELEMENT_RE.match(upper, pos)
        if not match:
            raise UnsupportedError(
                f"unsupported PICTURE string {raw!r}: unexpected character "
                f"{upper[pos]!r} (only X(n), 9(n), S and V are supported)",
                line=line, field=field_name,
            )
        char, count = match.group(1), match.group(2)
        repeat = 1
        if count is not None:
            repeat = int(count)
            if repeat < 1:
                raise CopybookSyntaxError(
                    f"PICTURE string {raw!r}: repeat count must be at least 1",
                    line=line, field=field_name,
                )
        elements.append((char, repeat))
        pos = match.end()

    chars = [c for c, _ in elements]
    if set(chars) == {"X"}:
        length = sum(n for _, n in elements)
        return Pic(category="text", length=length, raw=raw)

    # Numeric: optional leading S, then 9-runs, optional single V then 9-runs.
    signed = False
    index = 0
    if chars and chars[0] == "S":
        signed = True
        index = 1
    digits = 0
    saw_digit = False
    while index < len(elements) and elements[index][0] == "9":
        digits += elements[index][1]
        saw_digit = True
        index += 1
    scale = 0
    if index < len(elements) and elements[index][0] == "V":
        index += 1
        while index < len(elements) and elements[index][0] == "9":
            scale += elements[index][1]
            index += 1
        if scale == 0:
            raise CopybookSyntaxError(
                f"PICTURE string {raw!r}: V must be followed by at least one 9",
                line=line, field=field_name,
            )
    if index != len(elements) or not saw_digit:
        raise UnsupportedError(
            f"unsupported PICTURE string {raw!r}: only X(n), 9(n) and "
            f"[S]9(n)V9(m) are supported (no edited or alphabetic pictures)",
            line=line, field=field_name,
        )
    if digits + scale > 18:
        raise UnsupportedError(
            f"PICTURE string {raw!r}: {digits + scale} digits exceeds the "
            f"supported maximum of 18",
            line=line, field=field_name,
        )
    return Pic(category="numeric", length=0, digits=digits, scale=scale,
               signed=signed, raw=raw)


def _parse_occurs(words: List[Tuple[str, int]], i: int, field_name: str):
    """Parse the tokens following OCCURS.  Returns (Occurs, next_index)."""
    line = words[i][1]

    def integer(index: int, what: str) -> int:
        word, wline = words[index]
        if not word.isdigit():
            raise CopybookSyntaxError(
                f"OCCURS: expected {what}, got {word!r}", line=wline, field=field_name)
        return int(word)

    i += 1
    if i >= len(words):
        raise CopybookSyntaxError("OCCURS clause is incomplete", line=line, field=field_name)
    first = integer(i, "an integer occurrence count")
    i += 1
    had_to = False
    min_times, max_times, depending = first, first, None
    if i < len(words) and words[i][0].upper() == "TO":
        had_to = True
        i += 1
        if i >= len(words):
            raise CopybookSyntaxError("OCCURS ... TO: missing upper bound", line=line,
                                      field=field_name)
        max_times = integer(i, "an integer upper bound")
        min_times = first
        i += 1
    if i < len(words) and words[i][0].upper() == "TIMES":
        i += 1
    if i < len(words) and words[i][0].upper() == "DEPENDING":
        i += 1
        if i < len(words) and words[i][0].upper() == "ON":
            i += 1
        if i >= len(words):
            raise CopybookSyntaxError("OCCURS ... DEPENDING ON: missing field path",
                                      line=line, field=field_name)
        depending = words[i][0].upper()
        i += 1
    if i < len(words) and words[i][0].upper() in ("ASCENDING", "DESCENDING", "INDEXED", "KEY"):
        raise UnsupportedError(
            "OCCURS ... KEY / INDEXED BY is not supported",
            line=words[i][1], field=field_name)
    if depending is None and had_to:
        raise CopybookSyntaxError(
            "OCCURS min TO max requires DEPENDING ON <field-path>",
            line=line, field=field_name)
    if depending is not None and not had_to:
        # OCCURS n DEPENDING ON path  ->  0 TO n DEPENDING ON path
        min_times = 0
    return Occurs(min_times=min_times, max_times=max_times, depending_on=depending), i


def parse_declaration(words: List[Tuple[str, int]]) -> RawDecl:
    """Parse one flat declaration (tokens between two periods)."""
    level_word, line = words[0]
    upper_level = level_word.upper()
    if not _LEVEL_RE.match(level_word):
        if upper_level in ("77", "88", "66") or upper_level in _UNSUPPORTED_CLAUSES:
            raise UnsupportedError(
                f"unsupported construct starting with {level_word!r}",
                line=line)
        raise CopybookSyntaxError(
            f"declaration must start with a level number, got {level_word!r}", line=line)
    level = int(level_word)
    if level == 0 or level > 49:
        special = {66: "RENAMES", 77: "independent items", 88: "condition names"}
        if level in (66, 77, 88):
            raise UnsupportedError(
                f"level {level} ({special[level]}) is not supported",
                line=line)
        raise CopybookSyntaxError(
            f"level number {level} is outside the supported range 01-49", line=line)

    if len(words) < 2:
        raise CopybookSyntaxError("level number without a field name", line=line)
    name_word, name_line = words[1]
    if name_word.upper() == "FILLER":
        name = None
    else:
        if not _NAME_RE.match(name_word):
            raise CopybookSyntaxError(
                f"invalid COBOL word {name_word!r} (letters, digits and inner "
                f"hyphens only)", line=name_line)
        name = name_word.upper()
    decl = RawDecl(level=level, name=name, line=line)
    display = decl.display_name

    i = 2
    while i < len(words):
        word, wline = words[i]
        upper = word.upper()
        if upper in _UNSUPPORTED_CLAUSES:
            raise UnsupportedError(_UNSUPPORTED_CLAUSES[upper], line=wline, field=display)
        if upper == "REDEFINES":
            if decl.redefines is not None:
                raise CopybookSyntaxError("duplicate REDEFINES clause", line=wline,
                                          field=display)
            i += 1
            if i >= len(words):
                raise CopybookSyntaxError("REDEFINES without a target name", line=wline,
                                          field=display)
            target, tline = words[i]
            if not _NAME_RE.match(target):
                raise CopybookSyntaxError(
                    f"REDEFINES target {target!r} is not a valid COBOL word",
                    line=tline, field=display)
            decl.redefines = target.upper()
            decl.clauses.append("REDEFINES")
            i += 1
        elif upper in ("PIC", "PICTURE"):
            if decl.pic is not None:
                raise CopybookSyntaxError("duplicate PICTURE clause", line=wline,
                                          field=display)
            i += 1
            if i < len(words) and words[i][0].upper() == "IS":
                i += 1
            if i >= len(words):
                raise CopybookSyntaxError("PICTURE clause without a picture string",
                                          line=wline, field=display)
            pic_word, pline = words[i]
            decl.pic = parse_pic(pic_word, pline, display)
            decl.clauses.append("PIC")
            i += 1
        elif upper == "USAGE":
            if "USAGE" in decl.clauses:
                raise CopybookSyntaxError("duplicate USAGE clause", line=wline,
                                          field=display)
            i += 1
            if i < len(words) and words[i][0].upper() == "IS":
                i += 1
            if i >= len(words):
                raise CopybookSyntaxError("USAGE clause without a usage name",
                                          line=wline, field=display)
            usage_word, uline = words[i]
            usage = _USAGE_ALIASES.get(usage_word.upper())
            if usage is None:
                raise UnsupportedError(
                    f"USAGE {usage_word.upper()} is not supported (only DISPLAY, "
                    f"COMP and COMP-3 are)",
                    line=uline, field=display)
            decl.usage = usage
            decl.clauses.append("USAGE")
            i += 1
        elif upper in _USAGE_ALIASES:
            # Bare usage without the USAGE keyword (standard COBOL shorthand).
            if "USAGE" in decl.clauses:
                raise CopybookSyntaxError("duplicate USAGE clause", line=wline,
                                          field=display)
            decl.usage = _USAGE_ALIASES[upper]
            decl.clauses.append("USAGE")
            i += 1
        elif upper == "OCCURS":
            if decl.occurs is not None:
                raise CopybookSyntaxError("duplicate OCCURS clause", line=wline,
                                          field=display)
            decl.occurs, i = _parse_occurs(words, i, display)
            decl.clauses.append("OCCURS")
        else:
            raise CopybookSyntaxError(
                f"unexpected word {word!r} in declaration (not a supported clause)",
                line=wline, field=display)

    # Cross-clause validation that needs the whole declaration.
    if decl.pic is not None and decl.pic.category == "numeric":
        if decl.usage == "DISPLAY" and decl.pic.signed:
            raise UnsupportedError(
                "signed DISPLAY numerics (S with USAGE DISPLAY) are refused: "
                "zoned sign encodings cannot be decoded without guessing; use "
                "COMP or COMP-3 for signed values",
                line=decl.line, field=display)
        if decl.usage == "COMP" and decl.pic.total_digits > 18:
            raise UnsupportedError(
                "COMP fields are supported up to 18 decimal digits",
                line=decl.line, field=display)
    if decl.pic is not None and decl.pic.category == "text" and decl.usage != "DISPLAY":
        raise UnsupportedError(
            f"USAGE {decl.usage} on a PIC X field is not supported",
            line=decl.line, field=display)
    if decl.level == 1:
        if decl.pic is not None:
            raise CopybookSyntaxError(
                "the 01 root item must be a group (no PICTURE clause)",
                line=decl.line, field=display)
        if decl.redefines is not None:
            raise CopybookSyntaxError(
                "the 01 root item cannot redefine anything", line=decl.line, field=display)
    return decl


def parse_copybook(source: str) -> List[RawDecl]:
    """Parse copybook text into the flat list of declarations, in order."""
    tokens = _tokenize(source)
    if not tokens:
        raise CopybookSyntaxError("the copybook is empty", line=1)
    declarations = _split_declarations(tokens)
    return [parse_declaration(words) for words in declarations]
