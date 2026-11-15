"""Comparison-string construction with original-text provenance.

The matching convention is fixed and deliberately small — it applies to
alias texts and remark bodies alike:

1. every original Unicode character is case-folded **on its own**
   (``ch.casefold()`` — one call per character; no whole-string
   normalisation, no trimming, no punctuation stripping);
2. every maximal run of characters for which ``str.isspace()`` holds is
   then folded into a single U+0020 SPACE.

Folding may expand one original character into several comparison
characters (``ß`` → ``ss``) or collapse several original characters into
one (a whitespace run → one space), so every comparison character carries
the half-open interval of *original* character offsets it came from,
counted in Unicode characters — never UTF-8 bytes.  A match is only
reportable when both of its ends land on a *cut point*: a position that
does not split the expansion of one original character.  The reported
slice is always taken from the original text, never from the comparison
string.
"""

from __future__ import annotations

from typing import List, Tuple

#: Characters treated as ASCII word characters by the ``ascii_word``
#: boundary rule.  Only these 63 characters count; Chinese characters,
#: accented letters and every other Unicode letter do *not*.
ASCII_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)

BOUNDARY_MODES = ("any", "ascii_word")


def fold_with_mapping(text: str) -> Tuple[str, List[Tuple[int, int]]]:
    """Return ``(comparison, mapping)`` for *text*.

    ``mapping[i]`` is the half-open ``(start, end)`` interval of original
    character offsets that produced comparison character ``i``.  Both
    components are non-decreasing along the comparison string, and the
    intervals of one original character's expansion are identical.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, got {type(text).__name__}")
    # Phase 1: per-character casefold, keeping provenance.
    comp_chars: List[str] = []
    mapping: List[Tuple[int, int]] = []
    for index, ch in enumerate(text):
        folded = ch.casefold()
        for folded_ch in folded:
            comp_chars.append(folded_ch)
            mapping.append((index, index + 1))
    # Phase 2: fold every maximal whitespace run into one U+0020 space.
    out: List[str] = []
    out_map: List[Tuple[int, int]] = []
    total = len(comp_chars)
    j = 0
    while j < total:
        ch = comp_chars[j]
        if ch.isspace():
            k = j + 1
            while k < total and comp_chars[k].isspace():
                k += 1
            out.append(" ")
            out_map.append((mapping[j][0], mapping[k - 1][1]))
            j = k
        else:
            out.append(ch)
            out_map.append(mapping[j])
            j += 1
    return "".join(out), out_map


def is_cut_point(mapping: List[Tuple[int, int]], comp_length: int,
                 position: int) -> bool:
    """True when a match may legitimately start or end at *position*.

    A position is a cut point when it does not split the expansion of one
    original character: either it lies on an edge of the comparison
    string, or the original interval of the character on its left ends at
    or before the start of the interval on its right.  This is what stops
    one of the two ``s`` characters folded out of ``ß`` from being
    reported as a mention on its own.
    """
    if position <= 0 or position >= comp_length:
        return True
    return mapping[position][0] >= mapping[position - 1][1]


def original_interval(mapping: List[Tuple[int, int]], start: int,
                      end: int) -> Tuple[int, int]:
    """Original half-open interval covering comparison ``[start, end)``."""
    return mapping[start][0], mapping[end - 1][1]


def ascii_word_boundary_ok(text: str, start: int, end: int) -> bool:
    """Check the ``ascii_word`` boundary rule against the *original* text.

    The original characters immediately to the left and right of the hit
    (when they exist) must not be ASCII letters, digits or underscore.
    Only the original text is consulted; no Unicode letter is ever
    guessed to be an ASCII word character.
    """
    if start > 0 and text[start - 1] in ASCII_WORD_CHARS:
        return False
    if end < len(text) and text[end] in ASCII_WORD_CHARS:
        return False
    return True
