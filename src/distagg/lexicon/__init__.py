"""Controlled-vocabulary annotation of supplier remark text.

A maintainer compiles a dictionary of ``term_id`` / ``priority`` /
``aliases`` lines into a read-only SQLite matching library; an integrator
then scans a remark JSONL file with that library and gets, for every
line, the original object plus the hit positions and the chosen terms.
Matching is a self-contained Aho–Corasick automaton stored in SQLite
(states, transitions, failure links, output links) — no model inference,
no rewriting of the original text, no off-the-shelf multi-pattern
library.

Quick start (Python)::

    from distagg.lexicon import compile_dictionary, scan_remarks

    compile_dictionary("dictionary.jsonl", "terms.sqlite")
    stats = scan_remarks("terms.sqlite", "remarks.jsonl", "out.jsonl",
                         mode="resolved")

Quick start (terminal)::

    distagg lexicon compile dictionary.jsonl terms.sqlite
    distagg lexicon check   terms.sqlite
    distagg lexicon scan    terms.sqlite remarks.jsonl out.jsonl --mode all

See ``docs/LEXICON.md`` for the dictionary format, the comparison and
boundary conventions, the selection rules, the memory model and the
failure protocol.
"""

from .automaton import Automaton, LexiconLibrary
from .builder import CompileConfig, CompileStats, compile_dictionary
from .errors import (
    LexiconDictionaryError,
    LexiconError,
    LexiconInputError,
    LexiconLibraryError,
)
from .folding import fold_with_mapping
from .report import CONVENTIONS, library_report
from .scanner import ScanConfig, ScanStats, scan_remarks, scan_text

__all__ = [
    "Automaton",
    "CompileConfig",
    "CompileStats",
    "CONVENTIONS",
    "LexiconDictionaryError",
    "LexiconError",
    "LexiconInputError",
    "LexiconLibrary",
    "LexiconLibraryError",
    "ScanConfig",
    "ScanStats",
    "compile_dictionary",
    "fold_with_mapping",
    "library_report",
    "scan_remarks",
    "scan_text",
]
