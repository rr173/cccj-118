"""Build XLSX fixtures by hand-assembling ZIP/XML parts.

Tests use this builder instead of a real workbook library so that every
byte of the container -- relationship graph, shared strings, inline strings,
deliberately wrong cached values, shared-formula members before their
master -- is explicit and the expected answers are hand-written rather than
produced by a spreadsheet engine.
"""

from __future__ import annotations

import zipfile

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '{overrides}'
    '</Types>'
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="{workbook_target}"/>'
    '</Relationships>'
)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class Sheet:
    def __init__(self, name: str, part: str = None):
        self.name = name
        self.part = part
        self.rows = {}          # row -> {col_letter: cell_xml}
        self.merges = []
        self.hidden = False
        self.hidden_rows = set()

    def member(self, ref: str, *, shared: int, cached=None):
        """Set a shared-formula *member* cell (``<f t="shared" si="n"/>``)."""
        attrs = [f'r="{ref}"']
        inner = f'<f t="shared" si="{shared}"/>'
        if cached is not None:
            inner += f"<v>{_escape(cached)}</v>"
        letters = "".join(ch for ch in ref if ch.isalpha())
        digits = int("".join(ch for ch in ref if ch.isdigit()))
        row = self.rows.setdefault(digits, {})
        if letters in row:
            raise ValueError(f"duplicate coordinate {ref}")
        row[letters] = f"<c {' '.join(attrs)}>{inner}</c>"

    def cell(self, ref: str, *, value=None, formula=None, cached=None,
             kind=None, inline=None, style=None, shared=None,
             formula_type=None, raw_value=None, ref_range=None):
        """Set one ``<c>`` element by its A1 reference."""
        attrs = []
        if inline is not None and kind is None:
            kind = "inlineStr"
        if kind is not None:
            attrs.append(f't="{kind}"')
        if style is not None:
            attrs.append(f's="{style}"')
        attrs.append(f'r="{ref}"')
        inner = []
        if formula is not None:
            f_attrs = []
            if shared is not None:
                f_attrs.append('t="shared"')
                f_attrs.append(f'si="{shared}"')
                if ref_range is not None:
                    f_attrs.append(f'ref="{ref_range}"')
            if formula_type is not None:
                f_attrs.append(f't="{formula_type}"')
            attr_text = (" " + " ".join(f_attrs)) if f_attrs else ""
            inner.append(f"<f{attr_text}>{_escape(formula)}</f>")
        if raw_value is not None:
            inner.append(f"<v>{_escape(raw_value)}</v>")
        elif cached is not None:
            inner.append(f"<v>{_escape(cached)}</v>")
        elif value is not None and formula is None:
            inner.append(f"<v>{_escape(str(value))}</v>")
        if inline is not None:
            inner.append(
                '<is><t xml:space="preserve">' + _escape(inline) + "</t></is>"
            )
        letters = "".join(ch for ch in ref if ch.isalpha())
        digits = int("".join(ch for ch in ref if ch.isdigit()))
        row = self.rows.setdefault(digits, {})
        if letters in row:
            raise ValueError(f"duplicate coordinate {ref}")
        row[letters] = f"<c {' '.join(attrs)}>{''.join(inner)}</c>"

    def merge(self, ref: str):
        self.merges.append(ref)

    def hide_row(self, row_number: int):
        self.hidden_rows.add(row_number)

    def xml(self) -> str:
        row_xml = []
        for row_number in sorted(self.rows):
            cells = self.rows[row_number]
            ordered = []
            for letters in sorted(cells, key=_letters_key):
                ordered.append(cells[letters])
            state = ' hidden="1"' if row_number in self.hidden_rows else ""
            row_xml.append(
                f'<row r="{row_number}"{state}>' + "".join(ordered) + "</row>"
            )
        merges = ""
        if self.merges:
            merges = (
                f'<mergeCells count="{len(self.merges)}">'
                + "".join(f'<mergeCell ref="{_escape(m)}"/>'
                          for m in self.merges)
                + "</mergeCells>"
            )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main">'
            f"<sheetData>{''.join(row_xml)}</sheetData>{merges}</worksheet>"
        )


def _letters_key(letters: str):
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - 64)
    return value


class WorkbookBuilder:
    def __init__(self):
        self.sheets = []
        self.shared_strings = []

    def sheet(self, name: str, *, hidden: bool = False) -> Sheet:
        sheet = Sheet(name)
        sheet.hidden = hidden
        self.sheets.append(sheet)
        return sheet

    def shared_string(self, text: str) -> int:
        self.shared_strings.append(text)
        return len(self.shared_strings) - 1

    def write(self, path: str) -> str:
        overrides = [
            '<Override PartName="/xl/worksheets/worksheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.worksheet+xml"/>'.format(i=i + 1)
            for i in range(len(self.sheets))
        ]
        if self.shared_strings:
            overrides.append(
                '<Override PartName="/xl/sharedStrings.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sharedStrings+xml"/>'
            )
        wb_sheets = []
        wb_rels = []
        for index, sheet in enumerate(self.sheets):
            rel_id = f"rId{index + 1}"
            wb_sheets.append(
                f'<sheet name="{_escape(sheet.name)}" sheetId="{index + 1}" '
                f'r:id="{rel_id}"' + (' state="hidden"' if sheet.hidden else "")
                + "/>"
            )
            wb_rels.append(
                f'<Relationship Id="{rel_id}" Type="http://schemas.'
                f'openxmlformats.org/officeDocument/2006/relationships/'
                f'worksheet" Target="worksheets/worksheet{index + 1}.xml"/>'
            )
        if self.shared_strings:
            wb_rels.append(
                '<Relationship Id="rIdSst" Type="http://schemas.'
                'openxmlformats.org/officeDocument/2006/relationships/'
                'sharedStrings" Target="sharedStrings.xml"/>'
            )
        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.'
            'org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(wb_sheets)}</sheets></workbook>'
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES.format(
                overrides="".join(overrides)))
            archive.writestr(
                "_rels/.rels", _ROOT_RELS.format(
                    workbook_target="xl/workbook.xml"))
            archive.writestr("xl/workbook.xml", workbook_xml)
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">'
                + "".join(wb_rels) + "</Relationships>")
            for index, sheet in enumerate(self.sheets):
                archive.writestr(
                    f"xl/worksheets/worksheet{index + 1}.xml", sheet.xml())
            if self.shared_strings:
                si = "".join(
                    "<si><t>" + _escape(text) + "</t></si>"
                    for text in self.shared_strings
                )
                archive.writestr(
                    "xl/sharedStrings.xml",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<sst xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main" count="' + str(len(self.shared_strings))
                    + '" uniqueCount="' + str(len(self.shared_strings)) + '">'
                    + si + "</sst>")
        return path


# ---------------------------------------------------------------------------
# raw-part assembly for container fault tests
# ---------------------------------------------------------------------------

DEFAULT_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ),
    "xl/workbook.xml": (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    ),
    "xl/_rels/workbook.xml.rels": (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    ),
    "xl/worksheets/sheet1.xml": (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><v>7</v></c></row></sheetData>'
        "</worksheet>"
    ),
}


def write_raw_parts(path: str, overrides=None, drop=()):
    """Write a minimal valid package, overriding/dropping named parts."""
    import zipfile as _zip

    parts = dict(DEFAULT_PARTS)
    parts.update(overrides or {})
    with _zip.ZipFile(path, "w", _zip.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            if name in drop:
                continue
            if isinstance(data, str):
                data = data.encode("utf-8")
            archive.writestr(name, data)
    return path
