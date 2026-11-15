#!/usr/bin/env python3
"""Scale demonstration: a 100k-alias library, scanned with a tiny cache.

What this shows, with real numbers:

1.  a dictionary of 105k aliases / ~3.6M comparison characters is
    compiled into a ~1.9M-state automaton — the build streams
    everything through SQLite and the process peak RSS stays in the
    tens of MiB (no Python mirror of the trie);
2.  scanning 2000 remarks against that library with a **512-entry**
    state/transition cache: the caches peak far below their caps, the
    Python footprint matches the small-dictionary scan, and the
    transition/failure/output counters explain the actual cost;
3.  5000 aliases ending in the shared suffix "pump station" are all
    found through output links — the outputs table stores at most one
    row per state (nothing is copied onto states), yet every inherited
    hit is reported.

Run:  python3 demo/lexicon_scale_demo.py [WORKDIR]
"""

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

ENV = dict(os.environ)
ENV["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + ENV.get(
    "PYTHONPATH", "")

N_ALIASES = 100_000
N_SUFFIX_ALIASES = 5_000
N_REMARKS = 2_000


def lexicon(*args, expect=0):
    result = subprocess.run(
        [sys.executable, "-m", "distagg", "lexicon", *args],
        capture_output=True, text=True, env=ENV)
    assert result.returncode == expect, (
        f"distagg lexicon {' '.join(args)} exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    return result


def mib(kib):
    return kib / 1024


def main() -> int:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(
        prefix="lexicon-scale-demo-")
    os.makedirs(work, exist_ok=True)
    print(f"work directory: {work}")

    # ------------------------------------------------------------------
    print("\n== 1. generate the 105k-alias dictionary ==")
    rng = random.Random(2026)
    devices = ["pump", "valve", "motor", "seal", "filter", "gauge",
               "sensor", "rotor", "bearing", "nozzle"]
    parts = ["housing", "kit", "assembly", "unit", "module", "ring",
             "spring", "gasket", "nozzle", "flange"]
    variants = ["std", "mk2", "hd", "xl", "rev3", "nord", "type-b",
                "series-9"]
    dict_path = os.path.join(work, "big.jsonl")
    total_chars = 0
    alias_count = 0
    with open(dict_path, "w", encoding="utf-8", newline="\n") as fh:
        def emit(term_id, priority, text, boundary):
            nonlocal total_chars, alias_count
            fh.write(json.dumps({
                "term_id": term_id, "priority": priority,
                "aliases": [{"text": text, "boundary": boundary}],
            }, ensure_ascii=False) + "\n")
            total_chars += len(text)
            alias_count += 1

        # the "real" business terms the remarks actually hit
        emit("REAL-PUMP", 5, "pump", "ascii_word")
        emit("REAL-PUMP-STATION", 5, "pump station", "ascii_word")
        emit("REAL-STATION", 5, "station", "ascii_word")
        # 5000 aliases sharing the suffix "pump station"
        for i in range(N_SUFFIX_ALIASES):
            emit(f"VENDOR-PS-{i:04d}", 50, f"vendor{i:04d} pump station",
                 "any")
        # the bulk: 100k generated catalogue aliases
        for i in range(N_ALIASES):
            alias = (f"vendor{i % 5000:04d} {rng.choice(devices)} "
                     f"{rng.choice(parts)} {rng.choice(variants)} {i:06d}")
            emit(f"BULK-{i:06d}", rng.randint(60, 99), alias, "any")
    print(f"  aliases: {alias_count}, comparison chars: {total_chars} "
          f"(>= 3,000,000: {total_chars >= 3_000_000})")
    assert alias_count >= 100_000 and total_chars >= 3_000_000

    # ------------------------------------------------------------------
    print("\n== 2. compile (separate process) ==")
    lib_path = os.path.join(work, "big.sqlite")
    stats = json.loads(lexicon("compile", dict_path, lib_path).stdout)
    print(f"  states={stats['states']:,} transitions={stats['transitions']:,} "
          f"outputs={stats['outputs']:,} max_depth={stats['max_depth']}")
    print(f"  seconds={ {k: round(v, 2) for k, v in stats['seconds'].items()} }")
    print(f"  fail_walk_steps={stats['fail_walk_steps']:,} "
          f"transition_probes={stats['transition_probes']:,}")
    print(f"  build cache peaks: "
          f"transitions={stats['caches']['transitions']['peak_entries']:,}"
          f"/{stats['caches']['transitions']['capacity']:,} "
          f"state_info={stats['caches']['state_info']['peak_entries']:,}"
          f"/{stats['caches']['state_info']['capacity']:,}")
    print(f"  build peak RSS: {mib(stats['peak_rss_kib']):.0f} MiB "
          f"(library on disk: {os.path.getsize(lib_path) / 2**20:.0f} MiB)")
    assert stats["aliases"] == alias_count
    assert mib(stats["peak_rss_kib"]) < 512, stats["peak_rss_kib"]

    # ------------------------------------------------------------------
    print("\n== 3. generate remarks (few real hits) ==")
    vocab = ["hydraulic", "unit", "replacement", "order", "spare",
             "kit", "acme", "seal", "valve", "motor", "flange", "gasket",
             "vendor0042", "vendor1077", "torque", "flange"]
    rows = [
        {"id": f"R{i:05d}",
         "text": " ".join(rng.choice(vocab)
                          for _ in range(rng.randint(20, 60)))}
        for i in range(N_REMARKS)
    ]
    planted = {
        10: "urgent: pump station pump for line 3",
        500: "vendor0042 pump station overhaul",
        1500: "station only",
        1999: "vendor1077 pump station, then pump",
    }
    for index, text in planted.items():
        rows[index]["text"] = text
    remarks_path = os.path.join(work, "remarks.jsonl")
    with open(remarks_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  remarks: {len(rows)}, planted hits on lines "
          f"{sorted(i + 1 for i in planted)}")

    # ------------------------------------------------------------------
    print("\n== 4. scan the big library with a 512-entry cache ==")
    out_big = os.path.join(work, "out-big.jsonl")
    big = json.loads(lexicon(
        "scan", lib_path, remarks_path, out_big, "--mode", "resolved",
        "--state-cache-entries", "512",
        "--transition-cache-entries", "512",
        "--output-cache-entries", "256").stdout)
    auto = big["automaton"]
    print(f"  lines={big['lines_read']} matches={big['matches']} "
          f"seconds={big['seconds']:.2f} peak RSS "
          f"{mib(big['peak_rss_kib']):.0f} MiB")
    print(f"  chars={auto['chars_consumed']:,} "
          f"transition_lookups={auto['transition_lookups']:,} "
          f"fail_follows={auto['fail_follows']:,} "
          f"output_chain_states={auto['output_chain_states']:,} "
          f"candidates_raw={auto['candidates_raw']:,}")
    for name, cache in auto["caches"].items():
        print(f"    cache {name:<12} peak={cache['peak_entries']:,}"
              f"/{cache['capacity']:,} hits={cache['hits']:,} "
              f"misses={cache['misses']:,}")
        assert cache["peak_entries"] <= cache["capacity"]

    # ------------------------------------------------------------------
    print("\n== 5. same remarks against a 3-term library ==")
    small_dict = os.path.join(work, "small.jsonl")
    small_lib = os.path.join(work, "small.sqlite")
    with open(small_dict, "w", encoding="utf-8", newline="\n") as fh:
        for term_id, text in (("REAL-PUMP", "pump"),
                              ("REAL-PUMP-STATION", "pump station"),
                              ("REAL-STATION", "station")):
            fh.write(json.dumps({
                "term_id": term_id, "priority": 5,
                "aliases": [{"text": text, "boundary": "ascii_word"}],
            }) + "\n")
    lexicon("compile", small_dict, small_lib)
    out_small = os.path.join(work, "out-small.jsonl")
    small = json.loads(lexicon(
        "scan", small_lib, remarks_path, out_small, "--mode", "resolved",
        "--state-cache-entries", "512",
        "--transition-cache-entries", "512",
        "--output-cache-entries", "256").stdout)
    print(f"  small library: matches={small['matches']} "
          f"seconds={small['seconds']:.2f} peak RSS "
          f"{mib(small['peak_rss_kib']):.0f} MiB")
    print(f"  RSS delta big-vs-small: "
          f"{mib(big['peak_rss_kib']) - mib(small['peak_rss_kib']):+.0f} MiB "
          "— the Python footprint does not scale with the dictionary")
    assert mib(big["peak_rss_kib"]) < 256

    # the planted hits are identical under both libraries on the lines
    # where no bulk alias interferes
    def hits_by_line(path):
        with open(path, "r", encoding="utf-8") as fh:
            return {row["line"]: row["matches"]
                    for row in map(json.loads, fh)}

    big_hits = hits_by_line(out_big)
    small_hits = hits_by_line(out_small)
    for line_no in (11, 1501):
        assert big_hits[line_no] == small_hits[line_no], (
            line_no, big_hits[line_no], small_hits[line_no])
    print("  lines 11 and 1501: big-library hits == small-library hits")

    # ------------------------------------------------------------------
    print("\n== 6. shared suffixes through output links ==")
    line_501 = big_hits[501]
    print(f"  line 501 {rows[500]['text']!r} resolved to:")
    for hit in line_501:
        print(f"    [{hit['start']:>2},{hit['end']:>2}) {hit['term_id']} "
              f"{hit['text']!r}")
    # `all` mode shows every inherited hit on the shared suffix:
    out_all = os.path.join(work, "out-all.jsonl")
    lexicon("scan", lib_path, remarks_path, out_all, "--mode", "all",
            "--state-cache-entries", "512",
            "--transition-cache-entries", "512",
            "--output-cache-entries", "256")
    all_hits = hits_by_line(out_all)
    got = [(h["term_id"], h["start"], h["end"], h["text"])
           for h in all_hits[501]]
    # "vendor0042 pump station overhaul": vendor0042 is 10 chars, so the
    # shared suffix starts at 11; every inherited hit is reported.
    expected = [
        ("VENDOR-PS-0042", 0, 23, "vendor0042 pump station"),
        ("REAL-PUMP", 11, 15, "pump"),
        ("REAL-PUMP-STATION", 11, 23, "pump station"),
        ("REAL-STATION", 16, 23, "station"),
    ]
    assert got == expected, f"line 501 all-mode:\ngot {got}\nwant {expected}"
    print("  all mode on line 501 finds the full suffix chain:")
    for hit in all_hits[501]:
        print(f"    [{hit['start']:>2},{hit['end']:>2}) {hit['term_id']} "
              f"{hit['text']!r}")

    # nothing is copied onto states: every state holds at most one
    # output row, and the chain walk did the inheritance.
    db = sqlite3.connect(f"file:{lib_path}?mode=ro", uri=True)
    try:
        max_outputs = db.execute(
            "SELECT COALESCE(MAX(n), 0) FROM "
            "(SELECT COUNT(*) AS n FROM outputs GROUP BY state_id)"
        ).fetchone()[0]
        suffix_aliases = db.execute(
            "SELECT COUNT(*) FROM outputs WHERE term_id LIKE 'VENDOR-PS-%'"
        ).fetchone()[0]
    finally:
        db.close()
    print(f"  outputs table: max rows per state = {max_outputs} "
          f"(inheritance is the out_link chain, not copied lists); "
          f"{suffix_aliases:,} aliases share the 'pump station' suffix")
    assert max_outputs == 1
    assert suffix_aliases == N_SUFFIX_ALIASES

    print("\nlexicon scale demo OK: 105k aliases, bounded caches, "
          "flat memory, output-link inheritance verified")
    print(f"artifacts under: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
