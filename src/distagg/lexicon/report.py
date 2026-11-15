"""The ``check`` report: terms, aliases, automaton counts, conventions."""

from __future__ import annotations

from typing import Optional

from .automaton import LexiconLibrary
from .errors import LexiconError

#: The matching conventions, spelled out so a report reader does not have
#: to read the source to learn them.
CONVENTIONS = {
    "comparison": (
        "each original Unicode character is case-folded on its own "
        "(str.casefold per character), then every maximal run of "
        "str.isspace() characters folds into one U+0020 space; no "
        "trimming, no punctuation stripping, no other normalisation"
    ),
    "positions": (
        "start/end are half-open intervals counted in Unicode characters "
        "of the original text (never UTF-8 bytes); the reported slice is "
        "always taken from the original text"
    ),
    "cut_points": (
        "a match may only start or end where no original character's "
        "expansion is split — e.g. one 's' folded out of 'ß' is never a "
        "mention on its own, while 'ss' matching the whole 'ß' is"
    ),
    "boundary_any": "the hit may appear anywhere",
    "boundary_ascii_word": (
        "the original characters immediately left and right of the hit "
        "(if any) must not be ASCII letters, digits or '_'; other "
        "Unicode letters (e.g. Chinese characters) are not ASCII word "
        "characters"
    ),
    "all": (
        "every candidate passing the character and boundary rules; when "
        "several aliases of the same term hit the same original "
        "interval, only the smallest alias index is kept; sorted by "
        "(start, end, term_id, alias_index)"
    ),
    "resolved": (
        "a non-overlapping subset of the 'all' candidates: repeatedly "
        "take the leftmost start, breaking ties by longer original "
        "span, then smaller priority, then lexicographically smaller "
        "term_id, then smaller alias index; candidates starting before "
        "the chosen end are dropped, adjacent hits (start == end) are "
        "allowed"
    ),
}


def library_report(path: str, limit: Optional[int] = None,
                   *, sqlite_cache_size_kib: int = 8192) -> dict:
    """Build the ``check`` report for one library."""
    if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise LexiconError(f"limit must be a non-negative integer, got {limit!r}")
    with LexiconLibrary(path, sqlite_cache_size_kib=sqlite_cache_size_kib) \
            as library:
        counts = library.counts()
        terms = list(library.iter_terms(limit))
        return {
            "library": path,
            "format": library.meta.get("format"),
            "format_version": library.meta.get("format_version"),
            "complete": library.meta.get("complete") == "1",
            "created_utc": library.meta.get("created_utc"),
            "dictionary_sha256": library.meta.get("dictionary_sha256"),
            "counts": counts,
            "conventions": CONVENTIONS,
            "terms_total": counts["terms"],
            "terms_returned": len(terms),
            "terms": terms,
        }
