"""Independent reference implementation for lexicon differential tests.

This module re-implements the matching semantics from the specification
*without* using the production Aho–Corasick automaton, its traversal, or
its conflict-selection code:

* folding is re-derived here (whitespace runs are collapsed while
  scanning the original text, casefold is applied per character);
* matching enumerates every alias at every comparison position
  (``str.startswith`` per position), which is exactly what the
  production code is forbidden to do at scale;
* dedup, sorting and the resolved selection are written out again from
  the rules.

It is only suitable for small dictionaries, which is all the
differential tests need.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

ASCII_WORD = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def ref_fold(text):
    """Return ``(comparison, mapping)`` — an independent folding pass."""
    comparison = []
    mapping = []
    index = 0
    total = len(text)
    while index < total:
        ch = text[index]
        if ch.isspace():
            end = index + 1
            while end < total and text[end].isspace():
                end += 1
            comparison.append(" ")
            mapping.append((index, end))
            index = end
        else:
            for folded_ch in ch.casefold():
                comparison.append(folded_ch)
                mapping.append((index, index + 1))
            index += 1
    return "".join(comparison), mapping


def _cut(mapping, length, position):
    if position <= 0 or position >= length:
        return True
    return mapping[position][0] >= mapping[position - 1][1]


def reference_matches(terms, text, mode):
    """Compute the expected match list for one remark.

    ``terms`` is a list of ``(term_id, priority, [(alias_text,
    boundary), ...])`` in dictionary order; ``mode`` is ``"all"`` or
    ``"resolved"``.  Returns the same shape as the production output:
    a list of ``{"term_id", "alias_index", "start", "end", "text"}``.
    """
    comparison, mapping = ref_fold(text)
    comp_len = len(comparison)
    candidates = []
    for term_id, priority, aliases in terms:
        for alias_index, (alias_text, boundary) in enumerate(aliases):
            folded_alias, _ = ref_fold(alias_text)
            if not folded_alias:
                continue
            alias_len = len(folded_alias)
            for start in range(0, comp_len - alias_len + 1):
                if not comparison.startswith(folded_alias, start):
                    continue
                end = start + alias_len
                if not _cut(mapping, comp_len, start):
                    continue
                if not _cut(mapping, comp_len, end):
                    continue
                orig_start = mapping[start][0]
                orig_end = mapping[end - 1][1]
                if boundary == "ascii_word":
                    if orig_start > 0 and text[orig_start - 1] in ASCII_WORD:
                        continue
                    if orig_end < len(text) and text[orig_end] in ASCII_WORD:
                        continue
                candidates.append(
                    (orig_start, orig_end, term_id, alias_index, priority))

    # Same term, same interval: keep the smallest alias index.
    best = {}
    for candidate in candidates:
        key = (candidate[0], candidate[1], candidate[2])
        if key not in best or candidate[3] < best[key][3]:
            best[key] = candidate
    deduped = sorted(best.values(), key=lambda c: (c[0], c[1], c[2], c[3]))

    if mode == "all":
        chosen = deduped
    else:
        ordered = sorted(
            deduped, key=lambda c: (c[0], -(c[1] - c[0]), c[4], c[2], c[3]))
        chosen = []
        earliest = 0
        for candidate in ordered:
            if candidate[0] >= earliest:
                chosen.append(candidate)
                earliest = candidate[1]

    return [
        {
            "term_id": term_id,
            "alias_index": alias_index,
            "start": start,
            "end": end,
            "text": text[start:end],
        }
        for start, end, term_id, alias_index, _priority in chosen
    ]
