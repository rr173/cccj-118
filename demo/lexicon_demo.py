#!/usr/bin/env python3
"""End-to-end demonstration of distagg's controlled-vocabulary annotation.

Everything below is computed twice — by the real compile/scan pipeline
(running as **separate OS processes**, so nothing can leak through
in-memory state) and by hand / by the independent per-position reference
implementation in ``tests/reference_lexicon.py`` — and asserted equal.

Steps:

1.  compile the materials dictionary (pump / pump station / station /
    a second term also called "pump station") in one process;
2.  scan "pump station pump" in another process and check the
    hand-computed ``all`` and ``resolved`` results;
3.  priority tie-breaks: same-span candidates with different priority,
    then equal priority (term_id decides);
4.  folding and boundary edges: Straße/STRASSE, a single 's' against
    'ß', mixed whitespace runs, ASCII words next to Chinese characters,
    underscore/digit boundaries, adjacent hits and an empty remark —
    checking exact slices and intervals, not just counts;
5.  differential check against the independent reference implementation
    on a small random dictionary, both modes;
6.  shuffled dictionary line order: business decisions stay identical;
7.  fault cases: corrupt library, illegal dictionary, bad last input
    line and an unwritable output — each leaves a redoable state.

Run:  python3 demo/lexicon_demo.py [WORKDIR]
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from reference_lexicon import reference_matches  # independent reference

ENV = dict(os.environ)
ENV["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + ENV.get(
    "PYTHONPATH", "")


def lexicon(*args, expect=0):
    result = subprocess.run(
        [sys.executable, "-m", "distagg", "lexicon", *args],
        capture_output=True, text=True, env=ENV)
    assert result.returncode == expect, (
        f"distagg lexicon {' '.join(args)} exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    return result


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            if isinstance(record, str):
                handle.write(record + "\n")
            else:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path):
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def term(term_id, priority, *aliases):
    return {
        "term_id": term_id,
        "priority": priority,
        "aliases": [{"text": text, "boundary": boundary}
                    for text, boundary in aliases],
    }


def m(term_id, alias_index, start, end, text):
    return {"term_id": term_id, "alias_index": alias_index,
            "start": start, "end": end, "text": text}


def main() -> int:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(
        prefix="lexicon-demo-")
    os.makedirs(work, exist_ok=True)
    print(f"work directory: {work}")

    # ------------------------------------------------------------------
    print("\n== 1. compile the materials dictionary (process #1) ==")
    dictionary = [
        term("MAT-PUMP", 10, ("pump", "ascii_word")),
        term("MAT-PUMP-STATION", 10, ("pump station", "ascii_word")),
        term("MAT-STATION", 10, ("station", "ascii_word")),
        # a second, different term carrying the same alias text:
        term("MAT-PUMP-STATION-ALT", 10, ("pump station", "ascii_word")),
    ]
    dict_path = os.path.join(work, "materials.jsonl")
    lib_path = os.path.join(work, "materials.sqlite")
    write_jsonl(dict_path, dictionary)
    stats = json.loads(lexicon("compile", dict_path, lib_path).stdout)
    print(f"  terms={stats['terms']} aliases={stats['aliases']} "
          f"states={stats['states']} transitions={stats['transitions']} "
          f"outputs={stats['outputs']} max_depth={stats['max_depth']}")
    assert stats["terms"] == 4 and stats["aliases"] == 4

    check = json.loads(lexicon("check", lib_path).stdout)
    assert check["complete"] is True
    assert check["counts"]["states"] == check["counts"]["transitions"] + 1
    print(f"  check: complete={check['complete']} "
          f"states={check['counts']['states']} "
          f"transitions={check['counts']['transitions']} "
          f"conventions keys={sorted(check['conventions'])}")

    # ------------------------------------------------------------------
    print("\n== 2. scan 'pump station pump' (process #2), hand-checked ==")
    remarks_path = os.path.join(work, "remarks.jsonl")
    write_jsonl(remarks_path, [{"id": "R1", "text": "pump station pump"}])

    out_all = os.path.join(work, "out-all.jsonl")
    lexicon("scan", lib_path, remarks_path, out_all, "--mode", "all")
    rows = read_jsonl(out_all)
    expected_all = [
        m("MAT-PUMP", 0, 0, 4, "pump"),
        m("MAT-PUMP-STATION", 0, 0, 12, "pump station"),
        m("MAT-PUMP-STATION-ALT", 0, 0, 12, "pump station"),
        m("MAT-STATION", 0, 5, 12, "station"),
        m("MAT-PUMP", 0, 13, 17, "pump"),
    ]
    assert rows[0]["matches"] == expected_all, rows[0]["matches"]
    print("  all (sorted by start, end, term_id, alias_index):")
    for hit in rows[0]["matches"]:
        print(f"    [{hit['start']:>2},{hit['end']:>2}) "
              f"{hit['term_id']:<22} alias[{hit['alias_index']}] "
              f"{hit['text']!r}")

    out_res = os.path.join(work, "out-resolved.jsonl")
    lexicon("scan", lib_path, remarks_path, out_res, "--mode", "resolved")
    rows = read_jsonl(out_res)
    expected_resolved = [
        m("MAT-PUMP-STATION", 0, 0, 12, "pump station"),
        m("MAT-PUMP", 0, 13, 17, "pump"),
    ]
    assert rows[0]["matches"] == expected_resolved, rows[0]["matches"]
    print("  resolved (leftmost, longest, priority, term_id, alias_index):")
    for hit in rows[0]["matches"]:
        print(f"    [{hit['start']:>2},{hit['end']:>2}) "
              f"{hit['term_id']:<22} {hit['text']!r}")
    print("  hand computation confirmed: the long span wins at 0; "
          "MAT-PUMP-STATION beats MAT-PUMP-STATION-ALT on term_id; "
          "the trailing pump is adjacent-eligible.")

    # ------------------------------------------------------------------
    print("\n== 3. priority tie-breaks on same-span candidates ==")
    prio_dict = os.path.join(work, "prio.jsonl")
    prio_lib = os.path.join(work, "prio.sqlite")
    prio_out = os.path.join(work, "prio-out.jsonl")

    def resolve_with(prio_a, prio_b):
        if os.path.exists(prio_lib):
            os.unlink(prio_lib)
        write_jsonl(prio_dict, [
            term("TERM-A", prio_a, ("pump station", "ascii_word")),
            term("TERM-B", prio_b, ("pump station", "ascii_word")),
        ])
        lexicon("compile", prio_dict, prio_lib)
        lexicon("scan", prio_lib, remarks_path, prio_out,
                "--mode", "resolved")
        return read_jsonl(prio_out)[0]["matches"]

    got = resolve_with(20, 5)
    assert [x["term_id"] for x in got] == ["TERM-B"], got
    print("  priority 20 vs 5  -> TERM-B wins (smaller number first)")
    got = resolve_with(7, 7)
    assert [x["term_id"] for x in got] == ["TERM-A"], got
    print("  priority 7 vs 7   -> TERM-A wins (term_id breaks the tie)")

    # ------------------------------------------------------------------
    print("\n== 4. folding and boundary edges (slices checked exactly) ==")
    edge_dict = os.path.join(work, "edge.jsonl")
    edge_lib = os.path.join(work, "edge.sqlite")
    write_jsonl(edge_dict, [
        term("EDGE-STRASSE", 1, ("strasse", "any"), ("STRASSE", "any")),
        term("EDGE-S", 1, ("s", "any")),
        term("EDGE-SS", 1, ("ss", "any")),
        term("EDGE-PUMP", 1, ("pump", "ascii_word")),
        term("EDGE-PUMP-STATION", 1, ("pump station", "any")),
        term("EDGE-AB", 1, ("ab", "any")),
        term("EDGE-CD", 1, ("cd", "any")),
    ])
    lexicon("compile", edge_dict, edge_lib)
    edge_remarks = os.path.join(work, "edge-remarks.jsonl")
    edge_cases = [
        {"id": "strasse", "text": "Straße"},
        {"id": "STRASSE", "text": "STRASSE"},
        {"id": "whitespace", "text": "pump \t\n station"},
        {"id": "chinese", "text": "中文pump中文"},
        {"id": "underscore", "text": "pump_1"},
        {"id": "digit", "text": "2pump"},
        {"id": "adjacent", "text": "ab cd"},
        {"id": "empty", "text": ""},
    ]
    write_jsonl(edge_remarks, edge_cases)
    edge_out = os.path.join(work, "edge-out.jsonl")
    lexicon("scan", edge_lib, edge_remarks, edge_out, "--mode", "all")
    rows = {row["record"]["id"]: row["matches"] for row in read_jsonl(edge_out)}

    # Straße: the slice is the original 6 characters; 'ss' matches the
    # whole ß; a single 's' can only match the leading S — never one of
    # the two folded 's' characters inside ß.
    assert rows["strasse"] == [
        m("EDGE-S", 0, 0, 1, "S"),
        m("EDGE-STRASSE", 0, 0, 6, "Straße"),
        m("EDGE-SS", 0, 4, 5, "ß"),
    ], rows["strasse"]
    print("  Straße      -> 'strasse' matches [0,6) slice 'Straße'; "
          "'ss' matches [4,5) slice 'ß'; single 's' only at [0,1)")
    assert rows["STRASSE"] == [
        m("EDGE-S", 0, 0, 1, "S"),
        m("EDGE-STRASSE", 0, 0, 7, "STRASSE"),
        m("EDGE-S", 0, 4, 5, "S"),
        m("EDGE-SS", 0, 4, 6, "SS"),
        m("EDGE-S", 0, 5, 6, "S"),
    ], rows["STRASSE"]
    print("  STRASSE     -> alias 'strasse' matches uppercase body [0,7); "
          "same-term aliases dedup to alias_index 0")
    ws_text = "pump \t\n station"
    # 'pump station' (any) spans the whole whitespace run as one folded
    # space; 'pump' (ascii_word) matches the leading word; 's' (any)
    # matches the s in "station".
    assert rows["whitespace"] == [
        m("EDGE-PUMP", 0, 0, 4, "pump"),
        m("EDGE-PUMP-STATION", 0, 0, 15, ws_text),
        m("EDGE-S", 0, 8, 9, "s"),
    ], rows["whitespace"]
    print("  whitespace  -> 'pump station' spans ' \\t\\n ' as one folded "
          "space; slice keeps the whole run [0,15)")
    assert rows["chinese"] == [m("EDGE-PUMP", 0, 2, 6, "pump")], \
        rows["chinese"]
    print("  中文pump中文  -> Chinese characters are not ASCII word "
          "characters: boundary passes")
    assert rows["underscore"] == [] and rows["digit"] == []
    print("  pump_1/2pump -> '_' and digits block the ascii_word boundary")
    assert rows["adjacent"] == [
        m("EDGE-AB", 0, 0, 2, "ab"),
        m("EDGE-CD", 0, 3, 5, "cd"),
    ], rows["adjacent"]
    assert rows["empty"] == []
    print("  'ab cd'     -> adjacent hits both reported; empty remark "
          "yields an empty match list")

    # resolved on the same file: adjacent hits survive together.
    edge_res = os.path.join(work, "edge-res.jsonl")
    lexicon("scan", edge_lib, edge_remarks, edge_res, "--mode", "resolved")
    res_rows = {row["record"]["id"]: row["matches"]
                for row in read_jsonl(edge_res)}
    assert res_rows["adjacent"] == [
        m("EDGE-AB", 0, 0, 2, "ab"),
        m("EDGE-CD", 0, 3, 5, "cd"),
    ], res_rows["adjacent"]
    assert res_rows["strasse"] == [m("EDGE-STRASSE", 0, 0, 6, "Straße")]
    print("  resolved    -> adjacent hits coexist; 'Straße' resolves to "
          "the longest span")

    # ------------------------------------------------------------------
    print("\n== 5. differential check against the independent reference ==")
    rng = random.Random(20261115)
    alphabet = ["a", "b", "s", "ß", " ", "\t", "_", "1", "中", "p", "-"]
    ref_terms = []
    for index in range(6):
        aliases = []
        for _ in range(rng.randint(1, 3)):
            text = "".join(rng.choice(alphabet)
                           for _ in range(rng.randint(1, 6)))
            aliases.append((text, rng.choice(["any", "ascii_word"])))
        ref_terms.append((f"T{index:02d}", rng.randint(0, 5), aliases))
    ref_dict = os.path.join(work, "ref.jsonl")
    ref_lib = os.path.join(work, "ref.sqlite")
    write_jsonl(ref_dict, [
        {"term_id": term_id, "priority": priority,
         "aliases": [{"text": text, "boundary": boundary}
                     for text, boundary in aliases]}
        for term_id, priority, aliases in ref_terms
    ])
    lexicon("compile", ref_dict, ref_lib)
    ref_remarks = os.path.join(work, "ref-remarks.jsonl")
    texts = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 20)))
             for _ in range(60)]
    write_jsonl(ref_remarks, [{"text": text} for text in texts])
    for mode in ("all", "resolved"):
        ref_out = os.path.join(work, f"ref-{mode}.jsonl")
        lexicon("scan", ref_lib, ref_remarks, ref_out, "--mode", mode)
        rows = read_jsonl(ref_out)
        for row, text in zip(rows, texts):
            want = reference_matches(ref_terms, text, mode)
            assert row["matches"] == want, (
                f"mode={mode} text={text!r}\ngot:  {row['matches']}\n"
                f"want: {want}")
    print(f"  60 random remarks x 2 modes x {len(ref_terms)} terms: "
          "automaton == per-position reference on every line")

    # ------------------------------------------------------------------
    print("\n== 6. shuffled dictionary line order ==")
    shuffled_dict = os.path.join(work, "materials-shuffled.jsonl")
    shuffled_lib = os.path.join(work, "materials-shuffled.sqlite")
    order = list(dictionary)
    random.Random(42).shuffle(order)
    write_jsonl(shuffled_dict, order)
    lexicon("compile", shuffled_dict, shuffled_lib)
    shuffled_out = os.path.join(work, "shuffled-out.jsonl")
    lexicon("scan", shuffled_lib, remarks_path, shuffled_out,
            "--mode", "resolved")
    assert read_jsonl(shuffled_out)[0]["matches"] == expected_resolved
    shuffled_all = os.path.join(work, "shuffled-all.jsonl")
    lexicon("scan", shuffled_lib, remarks_path, shuffled_all, "--mode", "all")
    assert read_jsonl(shuffled_all)[0]["matches"] == expected_all
    print("  line order changed -> all/resolved outputs identical "
          "(alias_index is defined per term, not by file order)")

    # ------------------------------------------------------------------
    print("\n== 7. fault cases (each leaves a redoable state) ==")

    # 7a. corrupt library
    corrupt_lib = os.path.join(work, "corrupt.sqlite")
    shutil.copyfile(lib_path, corrupt_lib)
    with open(corrupt_lib, "r+b") as handle:
        handle.seek(1500)
        handle.write(b"\x00" * 4096)
    result = lexicon("check", corrupt_lib, expect=1)
    error = json.loads(result.stdout)
    assert error["error"] == "lexicon_library_error", error
    print(f"  corrupt library      -> exit 1, {error['error']}: "
          f"{error['message'][:60]}...")

    # 7b. illegal dictionary: unknown boundary at line 5, alias index 1;
    #     no openable library may be left behind.
    bad_dict = os.path.join(work, "bad.jsonl")
    bad_lib = os.path.join(work, "bad.sqlite")
    write_jsonl(bad_dict, dictionary + [
        {"term_id": "BAD", "priority": 1,
         "aliases": [{"text": "ok", "boundary": "any"},
                     {"text": "x", "boundary": "word"}]},
    ])
    result = lexicon("compile", bad_dict, bad_lib, expect=1)
    error = json.loads(result.stdout)
    assert error["error"] == "lexicon_dictionary_error", error
    assert error["line_number"] == 5 and error["alias_index"] == 1, error
    assert not os.path.exists(bad_lib)
    print(f"  illegal dictionary   -> exit 1, line {error['line_number']} "
          f"alias[{error['alias_index']}]: {error['message'][:50]}...; "
          "no library file left")

    # 7c. bad last input line: previous output survives, exit 1.
    bad_remarks = os.path.join(work, "bad-remarks.jsonl")
    write_jsonl(bad_remarks, [
        {"id": "R1", "text": "pump"},
        '{"id": "R2", "text": "pump station"',  # truncated JSON
    ])
    before = open(out_res, "rb").read()
    result = lexicon("scan", lib_path, bad_remarks, out_res, expect=1)
    error = json.loads(result.stdout)
    assert error["error"] == "lexicon_input_error", error
    assert error["line_number"] == 2, error
    assert open(out_res, "rb").read() == before
    print(f"  bad last line        -> exit 1 at line {error['line_number']}; "
          "previous output untouched")

    # 7d. unwritable output location: no target appears, exit 1.
    readonly_dir = os.path.join(work, "readonly")
    os.mkdir(readonly_dir)
    os.chmod(readonly_dir, 0o500)
    blocked_out = os.path.join(readonly_dir, "out.jsonl")
    try:
        result = lexicon("scan", lib_path, remarks_path, blocked_out,
                         expect=1)
        error = json.loads(result.stdout)
        assert error["error"] in ("lexicon_io_error", "lexicon_error"), error
        assert not os.path.exists(blocked_out)
        print(f"  unwritable output    -> exit 1, {error['error']}; "
              "no target file appeared")
    finally:
        os.chmod(readonly_dir, 0o700)

    print("\nlexicon demo OK: hand computations, reference comparison, "
          "process isolation and fault cases all verified")
    print(f"artifacts under: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
