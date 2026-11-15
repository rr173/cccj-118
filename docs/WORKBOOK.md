# Offline XLSX recalculation and verifiable row handover

`distagg workbook` turns a supplier's offline `.xlsx` cost sheet into
**verifiable data rows**.  Many amount cells in such a sheet are formulas
referencing other sheets, and the formula cache stored in the file may be
last month's result or missing entirely.  Reading that cache would present
stale money as new fact.  This entry point instead recomputes selected cells
from the raw OOXML value or formula with an evaluator implemented in this
package, and describes for every field where its value came from.

It is **not** a spreadsheet editor or a finance UI, and it is **not** a claim
of full Excel compatibility.  The formula subset is documented below and
anything outside it is refused, naming the cell and the construct.

What it never does:

* start Excel, LibreOffice or any external calculation service;
* write back to the XLSX;
* use a cached `<v>` result, for ordinary or shared formulas, as a fallback;
* silently fill a failed/unsupported/budget-exhausted field with a blank.

## Commands and API

```bash
distagg workbook recalc BOOK.xlsx RECIPE.json OUTPUT.jsonl \
    [--mode strict|annotate] [--cell-budget N] [--max-formula-cells N]
distagg workbook check  BOOK.xlsx RECIPE.json \
    [--cell-budget N] [--max-formula-cells N]
```

```python
from distagg.workbook import RecalcConfig, run_recalc_job, check_workbook

stats = run_recalc_job(
    "supplier_cost.xlsx", "recipe.json", "handover.jsonl",
    RecalcConfig(mode="annotate", cell_budget=1_000_000),
)
print(stats.rows_written, stats.ok_fields, stats.blank_fields,
      stats.error_fields, stats.budget)
```

Both commands print JSON on stdout and a one-line summary on stderr; a
job-level failure exits non-zero.  The output path must differ from both
inputs (`os.path.samefile` is used, so a hard link or a symlink is caught
too), and an existing destination is only replaced atomically after the
whole job succeeded.

## Recipe

```json
{
  "sheets": [
    {
      "sheet": "Costs",
      "first_row": 2,
      "last_row": 6,
      "fields": [
        {"name": "part", "column": "A"},
        {"name": "unit_cost", "column": "B"},
        {"name": "quantity", "column": "C"},
        {"name": "discount", "column": "D"},
        {"name": "total", "column": "F"}
      ]
    }
  ]
}
```

* `sheet` is matched against the workbook's real sheet names with Excel's
  case-insensitive rule.  A missing sheet, an illegal band
  (`first_row > last_row`, outside `1..1048576`), a non-letter column
  (outside `A..XFD`), an unknown recipe member, or a duplicate target field
  inside one mapping is rejected **before any output is opened**.  Reusing a
  field name in two different selections is allowed (each selection is its
  own row band).
* No header detection, no hidden row/column skipping: every coordinate in the
  band is emitted, including an entirely empty row.  Hidden sheets are listed
  by `check` but are read normally if a recipe names them.
* Several selections are delivered in recipe order; within one selection
  rows go in ascending order.

## JSONL row shape

One object per data row:

```json
{"sheet": "Costs", "row": 2, "cells": {
  "part": {"address": "A2", "origin": "text", "literal": "P-100",
           "status": "ok", "value": "P-100"},
  "total": {"address": "F2", "origin": "formula",
            "formula": "=B2*C2*(1-D2)*(1+'Tax Rates'!$B$2)",
            "status": "ok", "value": "20.340"}
}}
```

* `sheet` and `row` preserve the original worksheet name and row number.
* every field carries `address` (the original cell) and `origin`
  (`text`/`number`/`bool`/`error`/`formula`/`shared-formula`/`blank`/
  `merged-covered`), plus `literal` for constants or `formula` for formulas.
* status is one of `ok` (with `value`), `blank` (no `value` key at all) or
  `error` (with `type`, `message` and the reference `chain`).
* Numbers are serialized as exact plain decimal strings (`"20.340"`,
  `"9007199254740993"`, `"0.1234567890123456789012345678901234"`), never as
  binary floats.  Booleans stay JSON booleans, text stays text.
* A shared-formula member additionally carries `shared_index`, `master`,
  `is_master` and `translated_formula` -- the formula it really computes
  after relative references shifted.  A member is never given the master's
  value.

## OOXML reading contract

* The workbook part is found through `_rels/.rels`, sheets through
  `workbook.xml` relationships.  Part names are never guessed from a fixed
  list, and sheet name -> part mapping follows the relationship ids (so a
  worksheet part named `sheet1.xml` may well be the *second* sheet).
* Any relationship target that is an absolute URI, a UNC/network path, or
  that resolves outside the package (`..`) is an input fault.  Nothing is
  ever fetched.
* Shared strings, inline strings, numbers, booleans, empty strings (`t="str"`
  with an empty `<v>`, or an `<is><t></t></is>`) and Excel error cells
  (`t="e"`) are supported.  The empty string is **not** blank.
* Raw numeric text is turned into `decimal.Decimal` directly; no `float`
  exists on the path from XML to the delivered value.
* **Dates are not converted.**  A date-formatted cell is delivered as its raw
  serial number, because this package never applies a style's number format.
  That is a deliberate caliber choice: the handover is about amounts, not
  display.
* Merged cells: only the top-left anchor has a value.  Any other coordinate
  in the merge reads as `blank` (origin `merged-covered`) even if the XML
  writer left a stale `<c>` under it.
* Missing parts, broken XML, a DTD/entity declaration, duplicate cell
  coordinates in one sheet, an out-of-range shared-string index, or an
  invalid numeric/boolean literal are all reported as
  `workbook_package_error` naming the part/cell.  Nothing is repaired by
  guessing.

## Formula subset

Literals: decimal numbers (`1`, `2.5`, `1e-3`), double-quoted text with `""`
as the escaped quote, `TRUE` / `FALSE`.

Operators, from loosest to tightest binding: comparisons `= <> < <= > >=`,
then `+ -`, then `* /`, then unary `+ -`.  Same-precedence operators are left
associative.  Parentheses group as usual.

References: A1 style with optional `$` on row and/or column (`$A$1`, `A$1`,
`$A1`), same-sheet, `Sheet!A1`, and rectangular `A1:B2`.  Quoted sheet names
use Excel's single-quote rule (`'P&L 2024'!A1`, `'it''s'!A1`).  Column
letters are case-insensitive; sheet names are matched case-insensitively and
a workbook whose names collide after case folding is rejected.  A reference
outside `XFD1048576`, or to a sheet that does not exist, evaluates to
`#REF!` -- never to blank.

Functions (names case-insensitive): `SUM`, `MIN`, `MAX`, `COUNT`, `IF`,
`IFERROR`.

Out of subset (rejected with cell and construct): external workbook
references, defined names, array formulas, structured table references,
volatile functions (`RAND`, `NOW`, `TODAY`, `OFFSET`, `INDIRECT`, ...), any
other function, text concatenation `&`, whole-column/whole-row ranges, and a
bare range used as a scalar (a range is only accepted as a direct argument of
SUM/MIN/MAX/COUNT).  Formulas that are not dependencies of the selection are
not parsed -- but every formula that is touched is parsed completely; a
regex prefix is never used to "approximate" a value.

### Evaluation semantics

* Arithmetic: only numbers participate.  Blank acts as `0`; text and bool are
  `#VALUE!`; division by zero is `#DIV/0!`.  An error operand propagates
  unchanged.
* Comparison: same-kind numbers, text (code-point order) or bools compare.
  Blank equals blank; blank against any other kind is `#VALUE!`.
* `SUM`/`MIN`/`MAX`/`COUNT` accept scalar expressions or rectangular ranges.
  Only numbers contribute; blank, text and bool are ignored (directly or in
  a range).  The first error propagates in argument order and, inside a
  range, in row-major order.  With no numbers, `COUNT` is 0 and `SUM`, `MIN`,
  `MAX` are 0 by this subset's convention (not null).
* `IF` requires a bool condition (anything else is `#VALUE!`), evaluates only
  the selected branch, and a missing third argument yields `FALSE`.
* `IFERROR` evaluates its second argument only when the first is an error.
* All numeric arithmetic uses **Decimal precision 34 with ROUND_HALF_EVEN**.
  Serialization is plain decimal text that parses back to the same value; no
  binary float is ever used as the arithmetic caliber.

### Shared formulas

A shared master carries `<f t="shared" si="n" ref="F4:F6">` and its members
carry only `<f t="shared" si="n"/>`.  For each member, relative row/column
parts of every reference (including sheet-qualified ones and both endpoints
of a range independently) are shifted by the member-minus-master offset;
absolute `$` parts do not move.  The pass is independent of XML element
order, so a member may appear before its master.  A single `si` with no
master, with two masters, or a member outside the master's declared `ref`
is diagnosed with the sheet and cell.  Cached `<v>` values on shared members
are ignored exactly like ordinary formula caches.

## Dependencies, cycles, budget

Before any value is computed, the job walks the dependency graph of every
formula reachable from the selected coordinates -- including branches an
`IF` would not select -- and parses each fully.  Any cycle is a job-level
failure whose JSON names the actual address chain, e.g.
`["S!A1", "S!B1", "S!C1", "S!A1"]`.  Value computation is then a second,
lazy pass, so `IF` still short-circuits (a division by zero in an unselected
branch does not fire) while the reference check has already refused any
cycle hidden there.

Both passes use explicit work lists and generators driven by an outer loop,
never Python call-stack recursion; a 5000-step reference chain needs no
`sys.setrecursionlimit` change, and the scale report runs it with a recursion
limit of 120.  The recorded report (`demo/workbook/stress-report.sample.json`)
shows, on this machine: a 5000-deep chain with 4999 formula evaluations, one
output row and ~40 MB peak RSS, and a wide fan-in of 5000 output rows sharing
one intermediate cell with 5001 evaluations (the intermediate exactly once)
and ~38 MB peak RSS.  Range dependencies and range arguments are walked cell by cell
from the rectangle, never expanded into an address list.  Each computed cell
is memoized for the whole run, so a dependency shared by thousands of output
rows is evaluated once.

`--cell-budget N` counts every charged reference resolution and range cell
visit across the check and the evaluation.  Exceeding it is a job-level
`workbook_budget_exceeded`, never a partial success with blank fields.
`--max-formula-cells N` similarly caps the number of distinct formula cells
parsed.

## Delivery modes and failure classes

`strict` (default): the first selected field whose recomputed value is an
error stops the job.  The destination is written only after the whole job
succeeds, so a stopped job never leaves a complete-looking file there.  The
JSON error object carries `sheet`, `row`, `address`, `error_type`,
`rows_written` (rows completed before the failing row, in the deterministic
order described below), and `"complete": false`.  When at least one row had
been written, `partial_output` names an explicitly labelled
`OUTPUT.jsonl.partial` file holding exactly those rows, and `partial_note`
states that they are not a complete handover.  A stopped job therefore
cannot be mistaken for a full one: the destination is absent, and both
labels say why.

(note on rows completing before the stop: rows are built one at a time, so
no row *at or after* the failure is ever emitted; a row is checked for
errors only after every one of its fields was computed.)

`annotate`: every row is delivered; error fields carry `status: "error"` with
type, message and chain; the summary separates `ok_fields`, `blank_fields`
and `error_fields`.

Damage that is not about one selected field -- a broken package, a cycle, an
unsupported construct, a missing master, budget exhaustion -- is a job-level
failure in **both** modes and is never disguised as a blank cell.

## `workbook check`

Lists every sheet (name, part, hidden flag, cell/merge counts), every recipe
selection (rows, fields, and how many *selected* coordinates are formulas,
constants or blank), and the "touched" report: how many formula and constant
cells the reference check actually reached, per sheet, plus any sheet names a
formula referenced that do not exist.  It performs the same validation as
`recalc` but writes nothing.

### Deterministic output order

Output rows are emitted in recipe order and, inside one selection, in
ascending row number.  No ordering by amount or part code is applied and
nothing is de-duplicated, even when two selections name the same sheet.

## Worked supplier-cost handover

`demo/workbook/` contains a workbook built by hand-assembling ZIP/XML parts
(`build_workbook.py`), a recipe, and `hand_expected.json` with the numbers
worked out by hand:

* `'Tax Rates'!B2` holds `0.13`; `Costs` rows 2-6 carry unit cost, quantity
  and discount, and `F{r}` computes
  `B{r}*C{r}*(1-D{r})*(1+'Tax Rates'!$B$2)`.
* `F2` has a deliberately wrong cached `<v>999999</v>`; `F3` has no cache at
  all; `F4`/`F5` are shared members with wrong caches; `F6` is the shared
  master and appears in the XML *after* them.  Every row's recomputed total is
  asserted against the hand value (20.34, 13.56, 15.029, 6.78, 13.56).
* rows 7-9 cover zero quantity, an absent discount cell (blank, not `""`) and
  a deliberate `#DIV/0!` used to demonstrate strict vs annotate.
* a second selection reads the space-named `Tax Rates` sheet, proving quoted
  sheet names work.

Run everything (including the 5000-deep and fan-in scale report with real
evaluation counts and peak RSS):

```bash
sh demo/workbook/run_demo.sh /tmp/distagg-workbook-demo
# or directly
python3 demo/workbook/build_workbook.py
distagg workbook recalc demo/workbook/supplier_cost.xlsx \
    demo/workbook/supplier_cost.recipe.json /tmp/handover.jsonl --mode annotate
distagg workbook check demo/workbook/supplier_cost.xlsx \
    demo/workbook/supplier_cost.recipe.json
python3 tools/workbook_stress.py --rows 5000
```

## Reproducing the tests

```bash
python3 -m unittest discover -s tests -t tests -p 'test_workbook*.py'
```

Tests build every fixture with independent ZIP/XML assembly
(`tests/workbook_fixtures.py`) and assert hand-written answers; no
spreadsheet engine produces either an input or an expected value.

## Known caliber limits

* Only the documented formula subset is evaluated; there is no attempt to
  emulate Excel's date arithmetic, implicit intersection, coercion rules for
  booleans in arithmetic, or number formatting.
* Dates are raw serial numbers (see above).
* Cached results are ignored entirely; recalculation always starts from the
  formula text and the raw cell values.
* `COUNT`/`SUM` conventions with no numbers are 0 rather than null, as
  documented in the subset, not as a general Excel rule.
* Error `chain` lists are capped at 64 entries (`chain_length` and
  `chain_truncated` then describe the full depth) so a pathological chain
  cannot turn one field into a huge JSON blob; the cap is documented, never
  silent.
