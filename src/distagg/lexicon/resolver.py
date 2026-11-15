"""Candidate post-processing: provenance mapping, boundaries, selection.

These are pure functions over the candidates of **one** remark — the only
data ever held in memory besides the remark itself and its character
map.  The selection rules are fixed and fully spelled out here so no
behaviour depends on dictionary insertion order or on an unspecified
database return order:

* ``all`` — every candidate whose comparison interval lands on cut
  points and passes its boundary rule.  When several aliases of the
  *same* term hit the *same* original interval, only the smallest alias
  index survives.  Sorted by ``(start, end, term_id, alias_index)``.
* ``resolved`` — a non-overlapping subset of the ``all`` candidates,
  chosen greedily: take the leftmost start; break ties by longer
  original span, then smaller ``priority``, then lexicographically
  smaller ``term_id``, then smaller alias index.  After a pick, every
  candidate whose start lies before the pick's end is ignored; a
  candidate starting exactly at the pick's end (adjacent) stays
  eligible.
"""

from __future__ import annotations

from typing import List, Tuple

from .automaton import RawCandidate
from .folding import ascii_word_boundary_ok, is_cut_point, original_interval

#: One validated match in *original-text* coordinates:
#: (start, end, term_id, alias_index, priority).
ValidatedMatch = Tuple[int, int, str, int, int]


def validate_candidates(
    candidates: List[RawCandidate],
    mapping: List[Tuple[int, int]],
    comparison_length: int,
    text: str,
) -> List[ValidatedMatch]:
    """Map comparison-space candidates to original intervals and filter.

    A candidate survives when both ends are cut points (no split of one
    original character's expansion) and its boundary rule accepts the
    neighbouring original characters.
    """
    validated: List[ValidatedMatch] = []
    for comp_start, comp_end, term_id, alias_index, boundary, priority \
            in candidates:
        if not is_cut_point(mapping, comparison_length, comp_start):
            continue
        if not is_cut_point(mapping, comparison_length, comp_end):
            continue
        start, end = original_interval(mapping, comp_start, comp_end)
        if boundary == "ascii_word" and not ascii_word_boundary_ok(
                text, start, end):
            continue
        validated.append((start, end, term_id, alias_index, priority))
    return validated


def select_all(validated: List[ValidatedMatch]) -> List[ValidatedMatch]:
    """The ``all`` selection: same-term/same-interval dedup + fixed sort."""
    best = {}
    for start, end, term_id, alias_index, priority in validated:
        key = (start, end, term_id)
        if key not in best or alias_index < best[key][0]:
            best[key] = (alias_index, priority)
    deduped = [
        (start, end, term_id, alias_index, priority)
        for (start, end, term_id), (alias_index, priority) in best.items()
    ]
    deduped.sort(key=lambda m: (m[0], m[1], m[2], m[3]))
    return deduped


def select_resolved(all_matches: List[ValidatedMatch]) -> List[ValidatedMatch]:
    """The ``resolved`` selection: greedy leftmost-longest, no overlaps."""
    ordered = sorted(
        all_matches,
        key=lambda m: (m[0], -(m[1] - m[0]), m[4], m[2], m[3]),
    )
    chosen: List[ValidatedMatch] = []
    earliest_start = 0
    for match in ordered:
        if match[0] >= earliest_start:
            chosen.append(match)
            earliest_start = match[1]
    return chosen


def to_match_json(matches: List[ValidatedMatch], text: str) -> List[dict]:
    """Render matches with the exact slice of the *original* text."""
    return [
        {
            "term_id": term_id,
            "alias_index": alias_index,
            "start": start,
            "end": end,
            "text": text[start:end],
        }
        for start, end, term_id, alias_index, _priority in matches
    ]
