"""Build and run the two scale shapes and record real counts and peak RSS.

Shapes
------
``deep``
    A 5000-cell reference chain: A1 = 1, A2 = A1+1, ...  Selecting A5000 must
    evaluate exactly 4999 formula cells with no recursion-limit tuning.
``fan_in``
    One intermediate formula (B1 = A1*3) referenced by every one of 5000
    output rows (C{r} = B1+r).  B1 must be evaluated exactly once: 5001
    formula evaluations total.

Both workbooks are assembled from raw OOXML parts by this script (no
spreadsheet engine, no package-producing library).  The report prints the
job's own ``budget.formula_evaluations`` plus the peak RSS of the child
process; nothing is estimated.

Usage::

    python3 tools/workbook_stress.py [--out DIR] [--rows N]
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _content_types(parts):
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="application/xml"/>'
        for name in parts
    )
    return (
        _HEAD
        + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + overrides
        + "</Types>"
    )


def _escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def write_deep(path: str, depth: int) -> None:
    rows = ['<row r="1"><c r="A1"><v>1</v></c></row>']
    for row in range(2, depth + 1):
        rows.append(
            f'<row r="{row}"><c r="A{row}"><f>A{row - 1}+1</f></c></row>'
        )
    _write_single_sheet(path, "".join(rows))


def write_fan_in(path: str, rows: int) -> None:
    body = ['<row r="1"><c r="A1"><v>2</v></c><c r="B1"><f>A1*3</f></c></row>']
    for row in range(1, rows + 1):
        if row == 1:
            body[0] = body[0].replace(
                "</row>", f'<c r="C1"><f>B1+1</f></c></row>'
            )
        else:
            body.append(
                f'<row r="{row}"><c r="C{row}"><f>B1+{row}</f></c></row>'
            )
    _write_single_sheet(path, "".join(body))


def _write_single_sheet(path: str, sheet_rows: str) -> None:
    sheet = (
        _HEAD
        + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{sheet_rows}</sheetData></worksheet>"
    )
    workbook = (
        _HEAD
        + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        _HEAD
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    root_rels = (
        _HEAD
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types([
            "xl/worksheets/sheet1.xml"]))
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def run_case(name: str, workbook: str, recipe: dict, output: str,
             extra_args=()) -> dict:
    recipe_path = workbook + ".recipe.json"
    with open(recipe_path, "w", encoding="utf-8") as handle:
        json.dump(recipe, handle)
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    # A tiny recursion limit proves the evaluator is not leaning on Python
    # call-stack depth.
    # The child reports *its own* peak RSS on stderr, because
    # ``RUSAGE_CHILDREN`` in the parent would only give the maximum over all
    # children run so far -- which would make every shape report the same
    # number once a bigger one had run.
    wrapper = """\
import resource
import runpy
import sys

sys.setrecursionlimit(120)
sys.argv = [
    "distagg", "workbook", "recalc", sys.argv[1], sys.argv[2], sys.argv[3],
    "--mode", "annotate",
]
code = 0
try:
    runpy.run_module("distagg", run_name="__main__")
except SystemExit as exc:
    code = exc.code or 0
finally:
    print(
        "peak-rss-kib %d"
        % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        file=sys.stderr,
    )
sys.exit(code)
"""
    started = __import__("time").monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", wrapper, workbook, recipe_path, output],
        capture_output=True, text=True, env=env, cwd=tempfile.gettempdir(),
    )
    elapsed = __import__("time").monotonic() - started
    if proc.returncode != 0:
        raise SystemExit(f"{name} failed:\n{proc.stdout}\n{proc.stderr}")
    payload = json.loads(proc.stdout)
    peak = None
    recursion_limit = 120
    for line in proc.stderr.splitlines():
        if line.startswith("peak-rss-kib "):
            peak = int(line.split()[1])
    return {
        "shape": name,
        "workbook_bytes": os.path.getsize(workbook),
        "elapsed_seconds": round(elapsed, 3),
        "recursion_limit": recursion_limit,
        "peak_child_rss_kib": peak,
        "formula_evaluations": payload["budget"]["formula_evaluations"],
        "cell_accesses": payload["budget"]["cell_accesses"],
        "rows_written": payload["rows_written"],
        "output_path": output,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None)
    parser.add_argument("--rows", type=int, default=5000)
    args = parser.parse_args(argv)
    out_dir = args.out or tempfile.mkdtemp(prefix="distagg-workbook-stress-")
    os.makedirs(out_dir, exist_ok=True)
    cases = []
    deep_path = os.path.join(out_dir, "deep.xlsx")
    write_deep(deep_path, args.rows)
    cases.append(run_case(
        "deep", deep_path,
        {"sheets": [{"sheet": "S", "first_row": args.rows,
                     "last_row": args.rows,
                     "fields": [{"name": "v", "column": "A"}]}]},
        os.path.join(out_dir, "deep.jsonl"),
    ))
    fan_path = os.path.join(out_dir, "fan_in.xlsx")
    write_fan_in(fan_path, args.rows)
    cases.append(run_case(
        "fan_in", fan_path,
        {"sheets": [{"sheet": "S", "first_row": 1, "last_row": args.rows,
                     "fields": [{"name": "v", "column": "C"}]}]},
        os.path.join(out_dir, "fan_in.jsonl"),
    ))
    report = {"rows": args.rows, "cases": cases}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
