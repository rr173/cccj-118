#!/bin/sh
# Public demo for the offline row-level window processor.
#
# Runs the hand-computed example from the documentation: one group whose
# ordered (timestamp, value) pairs are (10,5), (10,null), (12,-2).  The four
# named windows and the row_number/rank/dense_rank/lag/lead results are
# written to a fresh output file and pretty-printed for inspection.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT_DIR="${1:-/tmp/distagg-window-demo}"
mkdir -p "$OUT_DIR"
INPUT="$HERE/hand_example.jsonl"
DECLARATION="$HERE/declaration.json"
OUTPUT="$OUT_DIR/hand_example.out.jsonl"

DISTAGG="${DISTAGG:-distagg}"
if ! command -v "$DISTAGG" >/dev/null 2>&1; then
    # Fall back to a source checkout next to this script.
    export PYTHONPATH="$HERE/../../src${PYTHONPATH:+:$PYTHONPATH}"
    DISTAGG="python3 -m distagg"
fi

echo "=== input (source order is preserved in the output) ==="
cat "$INPUT"
echo
echo "=== declaration ==="
cat "$DECLARATION"
echo
echo "=== running window job ==="
$DISTAGG window run "$INPUT" "$OUTPUT" --declaration "$DECLARATION"
echo
echo "=== output: one object per source row, each with computed ==="
cat "$OUTPUT"
echo
echo "output written to $OUTPUT"
