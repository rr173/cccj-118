#!/bin/sh
# Replay the packaged demo transactions one by one with the CLI and print the
# table after each step.  Usage: ./replay.sh /tmp/replay.db
set -eu
DB="${1:-/tmp/distagg-replay.db}"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
rm -f "$DB" "$DB-wal" "$DB-shm"
DISTAGG="${DISTAGG:-distagg}"
if ! command -v "$DISTAGG" >/dev/null 2>&1; then
    # Fall back to a source checkout next to this script.
    export PYTHONPATH="$HERE/../src${PYTHONPATH:+:$PYTHONPATH}"
    DISTAGG="python3 -m distagg"
fi
for f in "$HERE"/demo_transactions/*.json; do
    echo "=== $f"
    $DISTAGG submit "$DB" "$f" >/dev/null
    $DISTAGG summary "$DB"
done
echo "=== final check"
$DISTAGG check "$DB"
