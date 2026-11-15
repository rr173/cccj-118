#!/bin/sh
# Public demo for the offline XLSX recalculation handover.
#
#   1. builds demo/workbook/supplier_cost.xlsx from raw OOXML parts;
#   2. runs `distagg workbook check` (ranges, sheets, touched formula counts);
#   3. runs `distagg workbook recalc --mode annotate` and prints the JSONL;
#   4. shows strict mode refusing the deliberately broken row;
#   5. runs the 5000-deep / wide fan-in scale report.
#
# Nothing starts Excel or LibreOffice and nothing trusts the workbook's
# cached formula results; the workbook even carries deliberately stale
# caches to prove it.  The workbook is built directly into the output
# directory, so the source tree is not touched.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT_DIR="${1:-/tmp/distagg-workbook-demo}"
mkdir -p "$OUT_DIR"
BOOK="$OUT_DIR/supplier_cost.xlsx"
RECIPE="$OUT_DIR/supplier_cost.recipe.json"
OUTPUT="$OUT_DIR/handover.jsonl"

DISTAGG="${DISTAGG:-distagg}"
PYTHON="${PYTHON:-python3}"
if ! command -v "$DISTAGG" >/dev/null 2>&1; then
    export PYTHONPATH="$HERE/../../src${PYTHONPATH:+:$PYTHONPATH}"
    DISTAGG="$PYTHON -m distagg"
fi
export PYTHONPATH="$HERE/../../src${PYTHONPATH:+:$PYTHONPATH}"

echo "=== 1. building the workbook from raw OOXML parts ==="
"$PYTHON" - "$HERE" "$BOOK" "$RECIPE" <<'PY'
import json
import os
import sys

here, book_path, recipe_path = sys.argv[1:4]
sys.path.insert(0, here)
import build_workbook
build_workbook.build_demo(book_path)
with open(recipe_path, "w", encoding="utf-8") as handle:
    json.dump(build_workbook.DEMO_RECIPE, handle, indent=2)
    handle.write("\n")
print("wrote", book_path)
print("wrote", recipe_path)
PY

echo
echo "=== 2. workbook check (recipe ranges and actual touched cells) ==="
$DISTAGG workbook check "$BOOK" "$RECIPE"

echo
echo "=== 3. recalc, annotate mode (every row delivered, errors typed) ==="
$DISTAGG workbook recalc "$BOOK" "$RECIPE" "$OUTPUT" --mode annotate
echo
echo "--- handover JSONL ---"
cat "$OUTPUT"

echo
echo "=== 4. strict mode on the same recipe (the deliberate #DIV/0!) ==="
set +e
$DISTAGG workbook recalc "$BOOK" "$RECIPE" "$OUT_DIR/strict.jsonl"
STATUS=$?
set -e
echo "strict exit code: $STATUS (no file published: $(test ! -e "$OUT_DIR/strict.jsonl" && echo yes || echo NO))"
if [ -e "$OUT_DIR/strict.jsonl.partial" ]; then
    echo "rows completed before the stop, kept as an explicitly partial file:"
    cat "$OUT_DIR/strict.jsonl.partial"
fi

echo
echo "=== 5. scale report (real evaluation counts and peak RSS) ==="
"$PYTHON" "$HERE/../../tools/workbook_stress.py" \
    --out "$OUT_DIR/stress" --rows 5000

echo
echo "artifacts in $OUT_DIR"
