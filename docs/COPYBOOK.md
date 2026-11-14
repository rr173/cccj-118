# Copybook-driven offline decoding (`distagg copybook`)

`distagg.copybook` turns production records exported from a mainframe into
structured rows for downstream pipelines.  A maintenance programmer first
compiles the record layout from a COBOL copybook (`check`) and then decodes
the real export file (`decode`).  The entry point does layout compilation
and byte decoding only: it has no transaction numbering, quarantine or
resume protocol (those belong to the supplier TSV importer) and never
touches the aggregation database.

```bash
distagg copybook check  order_record.cpy --select selectors.json
distagg copybook decode order_record.cpy orders.dat --select selectors.json
```

```python
from distagg.copybook import compile_copybook, iter_file, load_selector_config

layout = compile_copybook(source_text, selectors=load_selector_config("selectors.json"))
for record in iter_file(layout, "orders.dat"):     # streams, one record in memory
    ...record.data...
```

## Input file format: RDW-framed records

Each record is preceded by a 4-byte record descriptor word:

* bytes 0-1: big-endian unsigned length **including the 4 RDW bytes**
  (so the payload is `length - 4` bytes; `length < 4` is an error);
* bytes 2-3: reserved, must be zero.

There is no block descriptor word (BDW).  The RDW is framing, not business
content: it never appears in the decoded output.  A truncated RDW or a
payload that ends early is reported with the record number and the file
byte offset of the record's first RDW byte.

## Supported copybook subset

Free format, case-insensitive, every declaration ends with a period, `*>`
starts a comment that runs to the end of the line.  One `01` root group per
copybook.

```
01 ORDER-RECORD.
   05 HEADER.
      10 ORDER-ID       PIC X(8).
      10 ORDER-DATE     PIC 9(8).                 *> unsigned DISPLAY
      10 FILLER         PIC X(2).
   05 DETAIL-COUNT      PIC 9(2) COMP.
   05 DETAILS OCCURS 0 TO 5 DEPENDING ON ORDER-RECORD.DETAIL-COUNT.
      10 SKU            PIC X(6).
      10 UNIT-PRICE     PIC S9(5)V99 COMP-3.
   05 ORDER-KIND        PIC X(1).
   05 ORDER-BODY        PIC X(6).
   05 EXTENDED-BODY REDEFINES ORDER-BODY.
      10 GIFT-FLAG      PIC X(1).
      10 LOYALTY-ID     PIC X(9).
```

* **Levels** `01`-`49`.  A group is a declaration without a PICTURE clause;
  an elementary item has one.  `FILLER` is elementary only.
* **PICTURE**: `X(n)`/`XXX` text; `9(n)`/`999` unsigned numeric; optional
  leading `S`; optional `V9(m)` implied decimal places.  Repeat counts and
  repeated characters both work (`9(5)` = `99999`).  At most 18 digits.
* **USAGE**: `DISPLAY` (default), `COMP`, `COMP-3`.  The synonyms
  `COMPUTATIONAL`, `BINARY`, `COMPUTATIONAL-3`, `PACKED-DECIMAL` are
  accepted, as is the bare usage word without the `USAGE` keyword.
* **OCCURS**: fixed `OCCURS n`, or `OCCURS min TO max DEPENDING ON
  full.field.path` (`OCCURS n DEPENDING ON p` means `0 TO n`).
* **REDEFINES** of the immediately preceding item in the same group (see
  below).

Anything else is **rejected with the copybook line and field**, never
silently ignored: level 66/77/88 items, `VALUE`, `SIGN`, `JUSTIFIED`,
`BLANK WHEN ZERO`, `SYNCHRONIZED`, edited pictures (`Z`, `,`, `.`, `*`,
`+`, `-`, `B`, `A`), `OCCURS ... KEY` / `INDEXED BY`, `USAGE` other than
the three above, signed `DISPLAY` numerics, `REDEFINES` chains, and more.

### Names and paths

Field names are unique within one group; different groups may reuse the
same name.  Every reference (DEPENDING ON, selector config) is a **full
path** from the record root, dot-separated, case-insensitive:
`ORDER-RECORD.DETAIL-COUNT`.

## Field decoding

* **Text** (`PIC X`): decoded as cp037.  Spaces, NUL bytes and every other
  byte value are preserved exactly — nothing is trimmed or normalised.
* **Unsigned DISPLAY** (`PIC 9(n)`): every byte must be an EBCDIC digit
  (`F0`-`F9`).  Anything else is an error; illegal digits are never
  repaired to zero.  **Signed DISPLAY is refused at compile time** so the
  decoder never has to guess a zoned sign.
* **COMP**: big-endian binary, 2/4/8 bytes for 1-4 / 5-9 / 10-18 total PIC
  digits.  `S` selects two's-complement signed, otherwise unsigned.  The
  value must fit the PIC digits (`|value| <= 10^digits - 1`); a value
  outside that range is an error.
* **COMP-3**: packed decimal, `digits // 2 + 1` bytes.  Every digit nibble
  must be 0-9; the final nibble is the sign (`C`/`F` = non-negative, `D` =
  negative, anything else is an error); when a padding nibble exists (even
  digit count) it must be zero.  An unsigned field must not carry a `D`
  sign.
* **Implied decimals** (`V`) occupy no bytes.  Numeric values are emitted
  as canonical decimal **strings** that keep the declared scale —
  `PIC S9(3)V99` value `-5` becomes `"-0.05"`, `0` becomes `"0.00"` —
  computed with integers only, never floats.  Negative zero is normalised
  to `"0.00"`.  Integers have no leading zeros (`"42"`, `"0"`).
* **FILLER** consumes its bytes (content not validated) and never appears
  in the output.

## OCCURS

* Fixed `OCCURS n` on groups or elementary items; may appear inside a
  variable-length group.
* `OCCURS min TO max DEPENDING ON path`: the counter must be an elementary
  **integer** item (no `V`), **non-repeating** (not itself inside an
  OCCURS), **not inside a REDEFINES branch**, and decoded **earlier** in
  the same record.  A count outside `min..max` is a record error naming
  the array's field path.
* Ordinary fields may follow a variable array; their offsets are computed
  per record from the actual count.  The layout report marks such offsets
  `"dynamic"` — they are never disguised as constants.

## REDEFINES and the selection config

A REDEFINES group is one item plus the branch items that immediately
follow it in the same group (each `REDEFINES <target>`).  The physical
footprint is the **maximum static length** of the branches; following
fields start after that span, and the slack of a shorter branch is
skipped.  Branches must be fixed-length (no variable OCCURS inside) and
the group must not sit inside an OCCURS.

Which branch a record uses is decided by an explicit **selection config**
— never by trying branches until one happens to decode:

```json
{
  "selectors": {
    "ORDER-RECORD.ORDER-BODY": {
      "using": "ORDER-RECORD.ORDER-KIND",
      "map": {"S": "ORDER-RECORD.ORDER-BODY",
              "E": "ORDER-RECORD.EXTENDED-BODY"}
    }
  }
}
```

* `using` is the full path of a previously decoded, non-repeating text or
  integer field (same restrictions as a DEPENDING ON counter).
* `map` keys are strings: text selector values compare byte-for-byte
  (spaces count), integer selector values compare as their canonical
  decimal string.  Map values must be branches of the group, and every
  branch must be reachable.
* A record whose selector value is not in the map is an error naming the
  redefines group.  Unselected branches are skipped without any numeric
  validation.

## Commands

### `distagg copybook check COPYBOOK [--select selectors.json]`

Compiles the copybook and prints the layout report as JSON: for every
field its full path, kind (`group`/`text`/`integer`/`decimal`),
PICTURE/USAGE, fixed span or variable min/max span, OCCURS and REDEFINES
relations, and the static byte offset where one exists
(`"offset_kind": "dynamic"` otherwise).  Exit code 0; compile and config
errors are JSON on stdout with the copybook line and field, exit code 1.

### `distagg copybook decode COPYBOOK DATAFILE [--select selectors.json] [--limit N]`

Streams the data file and prints one JSON object per line:

```json
{"record": 2, "offset": 62, "length": 173, "data": {"HEADER": {...}, ...}}
```

Groups stay objects, OCCURS items stay arrays, FILLER is omitted, a
REDEFINES group contributes only the selected branch (under the branch's
own name).  Every record payload must be consumed **exactly**: trailing
bytes and short records are errors.

On a bad record the lines already printed stay printed, the error object
goes to **stderr** and the exit code is 1 — a failed file is never
reported as fully decoded:

```json
{"error": "decode_error", "record_no": 2, "offset": 62,
 "field_path": "ORDER-RECORD.DETAILS[1].UNIT-PRICE",
 "reason": "invalid packed digit 0xA at digit position 1 ..."}
```

Field paths in errors use COBOL-style 1-based occurrence subscripts.

## Python API

```python
from distagg.copybook import (
    compile_copybook, load_selector_config,
    decode_record, iter_records, iter_file,
    CopybookError, CopybookSyntaxError, UnsupportedError, LayoutError,
    SelectorConfigError, DecodeError,
)

layout = compile_copybook(source, selectors=None)   # reusable, immutable
report = layout.describe()                          # the `check` output
data = decode_record(layout, payload_bytes)         # one record, no RDW
for record in iter_records(layout, fileobj):        # streaming, RDW-framed
    ...
```

`iter_records`/`iter_file` yield `DecodedRecord(record_no, offset, length,
data)` one at a time; memory stays proportional to a single record
regardless of file size.  A malformed record raises `DecodeError` with
`record_no`, `offset`, `field_path` and `reason`; records already yielded
stay yielded.  Compile-time problems raise `CopybookSyntaxError` /
`UnsupportedError` / `LayoutError` (with copybook `line` and `field`) or
`SelectorConfigError`.

## Demonstrations

```bash
python3 demo/copybook/make_order_feed.py        # independent byte constructor
python3 demo/copybook_demo.py                   # layout + decode + byte offsets
python3 demo/copybook_fault_demo.py             # seven corruption cases
python3 demo/copybook_stream_demo.py 20000      # constant-memory streaming
```

`make_order_feed.py` builds `orders.dat` (three records: zero and maximum
detail counts, both REDEFINES branches, negative packed decimals, F sign
nibbles, a NUL inside a text field) with hand-rolled EBCDIC/COMP/COMP-3
builders — no distagg code — and writes `expected.json` with hand-written
field values and key byte offsets that the demo verifies the decoder
against.  The fault demo corrupts the RDW, a packed digit nibble, the
DEPENDING ON count and the branch selector, plus truncates and pads
records, and checks the reported record number, file offset, field path
and reason each time.  The stream demo decodes 20k/100k generated records
and shows peak memory stays flat (a few KiB) and independent of file size.
