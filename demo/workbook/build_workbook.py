"""Build the packaged supplier-cost demo workbook from raw OOXML parts.

The workbook is assembled by hand (not by Excel/LibreOffice/OpenPyXL) so the
demo can state exactly which bytes are in it -- in particular which cached
formula results are deliberately stale, and which formula has no cache at
all.  Every expected answer in ``hand_expected.json`` was derived from the
documented arithmetic, not read back from a spreadsheet engine.

Run as a script to (re)create the checked-in fixture::

    python3 demo/workbook/build_workbook.py
"""

from __future__ import annotations

import os
import zipfile

_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

_CONTENT_TYPES = (
    _HEAD
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/worksheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/worksheets/worksheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/worksheets/worksheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    _HEAD
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_WORKBOOK = (
    _HEAD
    + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    "<sheets>"
    '<sheet name="Costs" sheetId="1" r:id="rId3"/>'
    '<sheet name="Tax Rates" sheetId="2" r:id="rId2"/>'
    '<sheet name="Notes 2024" sheetId="3" r:id="rId1"/>'
    "</sheets></workbook>"
)

_WORKBOOK_RELS = (
    _HEAD
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/worksheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/worksheet2.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/worksheet3.xml"/>'
    '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
    "</Relationships>"
)

# Shared string table.  Index 5 is the empty string, which must stay distinct
# from a blank cell; index 1 is a space-only string, also not blank.
_SHARED = [
    "part",              # 0
    " ",                 # 1
    "Bracket",           # 2
    "Washer",            # 3
    "Cable",             # 4
    "",                  # 5  explicit empty string, NOT blank
    "P-100",             # 6
    "P-101",             # 7
    "P-102",             # 8
    "P-103",             # 9
    "P-104",             # 10
    "total_label",       # 11
]


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


_SHARED_XML = (
    _HEAD
    + '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    f'count="{len(_SHARED)}" uniqueCount="{len(_SHARED)}">'
    + "".join(
        '<si><t xml:space="preserve">' + _escape(text) + "</t></si>"
        for text in _SHARED
    )
    + "</sst>"
)


def _worksheet(body: str, merges: str = "") -> str:
    return (
        _HEAD
        + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{body}</sheetData>{merges}</worksheet>"
    )


# ---------------------------------------------------------------------------
# Hand-computable demo semantics
#
#   tax rate      = 'Tax Rates'!B2 = 0.13
#   total row r   = B{r} * C{r} * (1 - D{r}) * (1 + 'Tax Rates'!$B$2)
#
# `unit_cost` = B, `quantity` = C, `discount` = D, `total` = F.
#
# Rows 2..6 are the first handover band:
#   r=2  unit 10 qty 2 disc 0.10  -> 10*2*0.90*1.13 = 20.34
#        (formula cache deliberately says 999999)
#   r=3  unit  5 qty 3 disc 0.20  ->  5*3*0.80*1.13 = 13.56
#        (formula has NO cached <v> at all)
#   r=4  unit  7 qty 2 disc 0.05  ->  7*2*0.95*1.13 = 15.029  (shared member)
#   r=5  unit  3 qty 4 disc 0.50  ->  3*4*0.50*1.13 =  6.78   (shared member,
#        its stale cache says 0)
#   r=6  unit 12 qty 1 disc blank -> 12*1*1.00*1.13 = 13.56   (shared MASTER,
#        appears in the XML *after* its members)
# Rows 7..9 are the second handover band:
#   r=7  unit 0 qty 9 disc 0     -> 0 (zero quantity)
#   r=8  unit 4 qty 5 discount cell absent -> blank -> 0 in arithmetic: 22.6
#   r=9  formula B9/(C9-C9)*(...): deliberate #DIV/0!
#
# The shared formula master is F6 with ref F4:F6, so relative B/C/D shift by
# row while the absolute 'Tax Rates'!$B$2 stays put.  A member whose cache is
# wrong must still recompute to its own row's value, never the master's.
# ---------------------------------------------------------------------------


def _costs_rows() -> str:
    return "".join([
        '<row r="1">'
        '<c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="inlineStr"><is><t>unit_cost</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>quantity</t></is></c>'
        '<c r="D1" t="inlineStr"><is><t>discount</t></is></c>'
        '<c r="F1" t="s"><v>11</v></c>'
        "</row>",
        '<row r="2">'
        '<c r="A2" t="inlineStr"><is><t>P-100</t></is></c>'
        '<c r="B2"><v>10</v></c>'
        '<c r="C2"><v>2</v></c>'
        '<c r="D2"><v>0.1</v></c>'
        '<c r="F2"><f>B2*C2*(1-D2)*(1+\'Tax Rates\'!$B$2)</f><v>999999</v></c>'
        "</row>",
        '<row r="3">'
        '<c r="A3" t="s"><v>7</v></c>'
        '<c r="B3"><v>5</v></c>'
        '<c r="C3"><v>3</v></c>'
        '<c r="D3"><v>0.2</v></c>'
        '<c r="F3"><f>B3*C3*(1-D3)*(1+\'Tax Rates\'!$B$2)</f></c>'
        "</row>",
        # Shared members before the master, with deliberately wrong caches.
        '<row r="4">'
        '<c r="A4" t="s"><v>8</v></c>'
        '<c r="B4"><v>7</v></c>'
        '<c r="C4"><v>2</v></c>'
        '<c r="D4"><v>0.05</v></c>'
        '<c r="F4"><f t="shared" si="7"/><v>0</v></c>'
        "</row>",
        '<row r="5">'
        '<c r="A5" t="s"><v>9</v></c>'
        '<c r="B5"><v>3</v></c>'
        '<c r="C5"><v>4</v></c>'
        '<c r="D5"><v>0.5</v></c>'
        '<c r="F5"><f t="shared" si="7"/><v>999</v></c>'
        "</row>",
        '<row r="6">'
        '<c r="A6" t="s"><v>10</v></c>'
        '<c r="B6"><v>12</v></c>'
        '<c r="C6"><v>1</v></c>'
        '<c r="F6"><f t="shared" si="7" ref="F4:F6">'
        "B6*C6*(1-D6)*(1+'Tax Rates'!$B$2)</f></c>"
        "</row>",
        '<row r="7">'
        '<c r="A7" t="inlineStr"><is><t>P-105</t></is></c>'
        '<c r="B7"><v>0</v></c>'
        '<c r="C7"><v>9</v></c>'
        '<c r="D7"><v>0</v></c>'
        '<c r="F7"><f>B7*C7*(1-D7)*(1+\'Tax Rates\'!$B$2)</f><v>77</v></c>'
        "</row>",
        # Row 8 has no D8 cell at all: discount is blank, not zero text and
        # not an empty string.
        '<row r="8">'
        '<c r="A8" t="inlineStr"><is><t>P-106</t></is></c>'
        '<c r="B8"><v>4</v></c>'
        '<c r="C8"><v>5</v></c>'
        '<c r="F8"><f>B8*C8*(1-D8)*(1+\'Tax Rates\'!$B$2)</f></c>'
        "</row>",
        '<row r="9">'
        '<c r="A9" t="inlineStr"><is><t>P-107</t></is></c>'
        '<c r="B9"><v>4</v></c>'
        '<c r="C9"><v>5</v></c>'
        '<c r="D9"><v>0</v></c>'
        '<c r="F9"><f>B9/(C9-C9)*(1+\'Tax Rates\'!$B$2)</f></c>'
        "</row>",
    ])


_TAX_RATES = _worksheet(
    '<row r="1"><c r="A1" t="inlineStr"><is><t>label</t></is></c>'
    '<c r="B1" t="inlineStr"><is><t>rate</t></is></c></row>'
    '<row r="2"><c r="A2" t="inlineStr"><is><t>VAT</t></is></c>'
    '<c r="B2"><v>0.13</v></c></row>'
)

_NOTES = _worksheet(
    '<row r="1"><c r="A1" t="inlineStr"><is><t>'
    "unrelated sheet: proves parts are found through relationships, not "
    "by guessing sheet1.xml"
    "</t></is></c></row>"
)


def build_demo(path: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        # Deliberately shuffled part order: xl/worksheets/worksheet1.xml is
        # the *Notes* sheet, while the cost sheet is worksheet3.xml.
        archive.writestr("xl/worksheets/worksheet1.xml", _NOTES)
        archive.writestr("xl/worksheets/worksheet2.xml", _TAX_RATES)
        archive.writestr("xl/worksheets/worksheet3.xml", _worksheet(_costs_rows()))
        archive.writestr("xl/sharedStrings.xml", _SHARED_XML)


#: Recipe matching the workbook above: two handover bands on ``Costs`` (in
#: recipe order) and one extraction from the space-named ``Tax Rates`` sheet.
DEMO_RECIPE = {
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
                {"name": "total", "column": "F"},
            ],
        },
        {
            "sheet": "Costs",
            "first_row": 7,
            "last_row": 9,
            "fields": [
                {"name": "part", "column": "A"},
                {"name": "quantity", "column": "C"},
                {"name": "discount", "column": "D"},
                {"name": "total", "column": "F"},
            ],
        },
        {
            "sheet": "Tax Rates",
            "first_row": 2,
            "last_row": 2,
            "fields": [
                {"name": "rate_label", "column": "A"},
                {"name": "tax_rate", "column": "B"},
            ],
        },
    ]
}


if __name__ == "__main__":
    import json

    here = os.path.dirname(os.path.abspath(__file__))
    workbook_path = os.path.join(here, "supplier_cost.xlsx")
    build_demo(workbook_path)
    recipe_path = os.path.join(here, "supplier_cost.recipe.json")
    with open(recipe_path, "w", encoding="utf-8") as handle:
        json.dump(DEMO_RECIPE, handle, indent=2)
        handle.write("\n")
    print("wrote", workbook_path)
    print("wrote", recipe_path)
