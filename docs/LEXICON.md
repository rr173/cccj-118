# Controlled-vocabulary annotation (`distagg lexicon`)

Supplier remarks mention the same material under short names, old names
and mixed case.  `distagg lexicon` keeps the original text untouched and
marks every mention with a unified term id: a maintainer **compiles** a
dictionary of aliases into a read-only matching library, and an
integrator **scans** a remark file with that library to get the hit
positions and the chosen terms.  It is batch standardisation from a
dictionary to business text — no model infers word senses, and the
original text is never rewritten.

```bash
distagg lexicon compile DICTIONARY.jsonl LIBRARY.sqlite [options]
distagg lexicon check   LIBRARY.sqlite [--limit N]
distagg lexicon scan    LIBRARY.sqlite INPUT.jsonl OUTPUT.jsonl [--mode all|resolved] [options]
```

```python
from distagg.lexicon import compile_dictionary, scan_remarks

compile_dictionary("dictionary.jsonl", "terms.sqlite")
stats = scan_remarks("terms.sqlite", "remarks.jsonl", "out.jsonl",
                     mode="resolved")
```

All commands print JSON on stdout; failures print one JSON error object
on stdout plus a one-line summary on stderr and exit non-zero.

## The dictionary

One JSON object per physical line (UTF-8 JSONL), with exactly the
members `term_id`, `priority`, `aliases`:

```json
{"term_id": "MAT-PUMP", "priority": 10, "aliases": [{"text": "pump", "boundary": "ascii_word"}]}
{"term_id": "MAT-PUMP-STATION", "priority": 10, "aliases": [{"text": "pump station", "boundary": "ascii_word"}, {"text": "pumpstation", "boundary": "any"}]}
```

* `term_id` — non-empty string, unique across the whole file.
* `priority` — an integer; smaller numbers win in `resolved` mode.  A
  boolean is **not** an integer.  Values must fit the signed 64-bit
  range.
* `aliases` — a non-empty array of `{"text", "boundary"}` objects.
  `text` is a non-empty string; `boundary` is `any` or `ascii_word`.

Different terms may carry the same alias text, and one term may carry
aliases that fold to the same comparison string — nothing is dropped
for sharing a name.  The *alias index* reported in matches is the
0-based position inside the term's own `aliases` array, so it does not
depend on the line order of the dictionary file.

Every record is validated as it streams in.  Format errors, a duplicate
`term_id`, an unknown boundary mode or an alias that folds to an empty
comparison string abort the compile and name the physical 1-based
dictionary line and, where relevant, the 0-based alias index:

```json
{"error": "lexicon_dictionary_error", "line_number": 5, "alias_index": 1,
 "message": ".../dict.jsonl(line 5) alias[1]: unknown boundary mode 'word'; ..."}
```

A failed compile never leaves a file that can be opened as a complete
library.  The target must not exist: compile refuses to overwrite, and
the new library is built in a private temporary file and published with
`os.link` (atomic, fails if the target appeared meanwhile) only after
the `complete` marker is committed.  One physical dictionary line is
limited to `--max-record-bytes` bytes (default 1 MiB); exceeding the
limit fails the compile rather than truncating anything.

## The comparison string

Aliases and remark bodies are folded by exactly the same fixed rule:

1. every original Unicode character is case-folded **on its own**
   (`str.casefold()` per character);
2. every maximal run of `str.isspace()` characters is then folded into
   one U+0020 space.

Nothing else happens: no trimming, no punctuation stripping, no other
Unicode normalisation.  Every comparison character remembers the
half-open interval of *original* characters it came from, counted in
Unicode characters (never UTF-8 bytes).  Folding may expand one
character into several (`ß` → `ss`) or collapse several into one (a
whitespace run → one space), so a match is only reportable when both of
its ends land on a **cut point** — a position that does not split one
original character's expansion.  Consequences, all directly observable
in the output slices:

* alias `strasse` matches the body `Straße`; the reported slice is the
  original 6 characters `Straße`, not the 7 folded ones;
* alias `ss` matches the body `ß` (slice `ß`);
* alias `s` matches the `S` in `Straße` but **not** either of the two
  folded `s` characters inside `ß`;
* an alias containing a space matches a body run of tabs/newlines/
  spaces, and the reported interval covers the whole run;
* the reported slice is always taken from the original text.

## Boundary modes

* `any` — the hit may appear anywhere.
* `ascii_word` — the original characters immediately left and right of
  the hit (when they exist) must not be ASCII letters, digits or `_`.
  Only the original text is consulted: Chinese characters and other
  Unicode letters are **not** ASCII word characters, so `中文pump中文`
  matches a `pump` alias while `xpump`, `pump2` and `pump_1` do not.

## The two annotation modes

Both modes start from the same candidate set: every alias occurrence in
the comparison string whose ends are cut points and whose boundary rule
accepts the neighbouring original characters.  When several aliases of
the *same* term hit the *same* original interval, only the smallest
alias index is kept (different terms are all kept).

* `all` — every surviving candidate, sorted by
  `(start, end, term_id, alias_index)`.
* `resolved` — a non-overlapping subset of the `all` candidates, chosen
  greedily: repeatedly take the leftmost start; break ties by longer
  original span, then smaller `priority`, then lexicographically smaller
  `term_id`, then smaller alias index.  After a pick, every candidate
  whose start lies before the pick's end is ignored; a candidate
  starting exactly at the pick's end (adjacent) stays eligible.

The rules are fixed and total — no behaviour depends on dictionary
insertion order, on Python dict iteration order, or on an undeclared
database return order.  Reordering the dictionary lines changes nothing
except the state numbering inside the library.

Worked example (dictionary above, body `pump station pump`, equal
priorities):

```
all:      [0,4)  MAT-PUMP                "pump"
          [0,12) MAT-PUMP-STATION        "pump station"
          [0,12) MAT-PUMP-STATION-ALT    "pump station"   (same alias text, other term)
          [5,12) MAT-STATION             "station"
          [13,17) MAT-PUMP               "pump"
resolved: [0,12) MAT-PUMP-STATION        (longest at 0; term_id beats -ALT)
          [13,17) MAT-PUMP               (adjacent after 12)
```

## Scanning

Input is UTF-8 JSONL, one object per physical line; `text` must be a
string and every other member is preserved untouched (business ids are
not required to be unique).  Output is one JSON object per input line —
no line is ever dropped, and empty or all-whitespace remarks simply get
an empty match list:

```json
{"line": 1, "record": {"id": "R1", "text": "pump station pump"},
 "matches": [{"term_id": "MAT-PUMP-STATION", "alias_index": 0, "start": 0, "end": 12, "text": "pump station"},
             {"term_id": "MAT-PUMP", "alias_index": 0, "start": 13, "end": 17, "text": "pump"}]}
```

`start`/`end` are half-open offsets in Unicode characters of the
original `text`; `text` in each match is exactly that slice.

The library is opened **read-only** (`mode=ro` + `PRAGMA query_only`)
and its format name, format version and `complete` marker are verified
before any matching — a corrupted, foreign or half-built file is
rejected with a `lexicon_library_error`.  A bad input line (invalid
UTF-8, duplicate JSON member, non-object, missing or non-string `text`,
line over `--max-remark-bytes`, default 4 MiB) fails the whole run with
a `lexicon_input_error` naming the physical line.  One remark may not
produce more than `--max-candidates-per-record` raw candidates (default
1 000 000); exceeding that fails loudly instead of truncating hits.

The result is written to a private temporary file first and published
with `os.replace` only after the last input line has been read and the
file has been flushed and fsynced: a bad last line or a real write
failure never overwrites a previous result and never leaves a new file
that looks complete.

## The automaton on disk

Matching is a self-contained Aho–Corasick automaton stored in the
SQLite library — no per-remark iteration over the alias list, no
off-the-shelf multi-pattern library:

* `states(state_id, depth, parent, ch, has_output, fail, out_link)` —
  one row per trie state; `fail` is the failure link and `out_link` the
  nearest failure ancestor that carries outputs;
* `transitions(state_id, ch, target)` — the goto function, primary-keyed
  for point probes;
* `outputs(state_id, term_id, alias_index, boundary, priority, length)`
  — one row per alias, stored **only** on the state where the alias
  ends.  Inherited matches are found by walking the `out_link` chain at
  scan time, never by copying ancestor hit lists onto states;
* `terms` / `aliases` — the audited dictionary (used by `check`, not by
  the scan);
* `meta` — format name/version, the `complete` marker, counts and the
  dictionary's SHA-256.

**Compile** streams the dictionary line by line (duplicates are caught
by the `terms` primary key, not by a Python set), then builds the trie
from a sorted alias stream — sorted order means each edge is created
exactly once and the only in-memory structure is one root-to-leaf path —
then computes failure and output links one depth level at a time
(`fail(s)` always lies at a smaller depth), with transition and
state-info lookups served by fixed-capacity LRU caches over the SQLite
indexes.  Neither the trie, the failure links nor the dictionary is
ever held in Python objects.

**Scan** holds, at any moment: one remark (≤ `--max-remark-bytes`), its
comparison string and character map, this remark's candidates, and the
three bounded caches (`--state-cache-entries`,
`--transition-cache-entries`, `--output-cache-entries`).  The corpus
size is never part of the memory bound.

Both commands report real measurements: per-phase build seconds, state/
transition/output counts, failure-walk steps, transition probes, cache
hit/miss/peak figures, scan seconds and process peak RSS.  On the
reference machine (Python 3.11, SQLite 3.40), compiling 105 003 aliases
(3.6 M comparison characters, 2.0 M states) takes ~26 s with a 57 MiB
peak RSS, and scanning 2000 remarks (571 k characters) against that
library with a 512-entry cache takes ~0.5 s at a 25 MiB peak RSS —
identical to scanning the same remarks against a 3-term library.  See
`demo/lexicon_scale_demo.py`.

## `check`

`distagg lexicon check LIBRARY.sqlite [--limit N]` prints the format and
completeness marker, the term/alias/state/transition/output counts, the
matching conventions (the rules above, spelled out), and the terms with
their aliases (`--limit` caps the listing only; counts always cover the
whole library).

## Failure summary

| situation | result |
|---|---|
| dictionary line malformed / duplicate `term_id` / unknown boundary / alias folds empty | exit 1, `lexicon_dictionary_error` with line (+ alias index); no library left |
| library target exists | exit 1, `lexicon_library_error`; existing file untouched |
| library missing, corrupt, foreign or incomplete | exit 1, `lexicon_library_error` |
| remark line bad (encoding, JSON, shape, `text` type, size) | exit 1, `lexicon_input_error` with the physical line; previous output untouched |
| remark produces too many candidates | exit 1, same protocol |
| output cannot be written | exit 1, `lexicon_io_error`; no new target appears |

## Demonstrations

```bash
python3 demo/lexicon_demo.py         # hand-checked semantics, reference
                                     # cross-check, process isolation, faults
python3 demo/lexicon_scale_demo.py   # 105k aliases, bounded caches, real
                                     # timings/RSS, output-link suffix chains
```
