"""Unit tests for TSV parsing, classification rules and path safety.

These tests exercise the importer's pure logic without touching a staging
store: config validation, the empty-field / empty-bytes / illegal-hex
distinction, duplicate vs conflict rules, raw-byte retention, header
defects, truncated UTF-8 and the symlink / path-escape boundary checks.
"""

import base64
import os
import shutil
import tempfile
import unittest

from distagg.staging import (
    ImportConfigError,
    ImportHeaderError,
    ImportPathError,
    classify_file,
    parse_config,
    resolve_input_path,
)
from distagg.staging.importer import (
    REASON_BLANK_LINE,
    REASON_CONFLICT,
    REASON_EMPTY_FIELD,
    REASON_FIELD_COUNT,
    REASON_HEX_BAD_CHARACTER,
    REASON_HEX_ODD_LENGTH,
    REASON_INVALID_UTF8,
    REASON_SUPPLIER_MISMATCH,
    STATUS_CONFLICT,
    STATUS_DUPLICATE,
    STATUS_IMPORTABLE,
    STATUS_QUARANTINED,
)

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "imports")


def fixture(name: str) -> str:
    return os.path.join(DATA, name)


BASE_CONFIG = {
    "format": 1,
    "supplier_column": "supplier",
    "correction_id_column": "correction_id",
    "key_column": "key_hex",
    "value_column": "value_hex",
    "required_columns": ["supplier", "correction_id", "key_hex", "value_hex"],
    "allowed_extra_columns": ["note"],
    "supplier": {"expected": "acme"},
    "batch_rows": 3,
}


def config(**overrides):
    raw = dict(BASE_CONFIG)
    raw.update(overrides)
    return parse_config(raw)


class ConfigTest(unittest.TestCase):
    def test_minimal_valid_config(self):
        cfg = parse_config({
            "supplier_column": "s",
            "correction_id_column": "c",
            "key_column": "k",
            "value_column": "v",
            "required_columns": ["s", "c", "k", "v"],
            "allowed_extra_columns": [],
        })
        self.assertEqual(cfg.batch_rows, 100)  # default
        self.assertIsNone(cfg.supplier_expected)

    def test_missing_fields_are_all_reported(self):
        with self.assertRaises(ImportConfigError) as ctx:
            parse_config({"required_columns": []})
        self.assertIn("supplier_column", " ".join(ctx.exception.issues))
        self.assertIn("required_columns", " ".join(ctx.exception.issues))

    def test_named_column_must_be_declared(self):
        raw = dict(BASE_CONFIG)
        raw["required_columns"] = ["supplier", "correction_id", "key_hex"]
        with self.assertRaises(ImportConfigError) as ctx:
            parse_config(raw)
        self.assertTrue(any("value_hex" in issue for issue in ctx.exception.issues))

    def test_duplicate_and_overlapping_column_declarations(self):
        raw = dict(BASE_CONFIG)
        raw["required_columns"] = BASE_CONFIG["required_columns"] + ["note"]
        raw["allowed_extra_columns"] = ["note"]
        with self.assertRaises(ImportConfigError):
            parse_config(raw)
        raw2 = dict(BASE_CONFIG)
        raw2["required_columns"] = BASE_CONFIG["required_columns"] + ["note"]
        raw2["allowed_extra_columns"] = ["note", "note"]
        with self.assertRaises(ImportConfigError):
            parse_config(raw2)

    def test_batch_rows_must_be_positive_int(self):
        for bad in (0, -1, "3", True, 1.5):
            with self.assertRaises(ImportConfigError):
                parse_config(dict(BASE_CONFIG, batch_rows=bad))

    def test_fingerprint_is_stable_and_config_sensitive(self):
        cfg1 = config()
        cfg2 = config()
        self.assertEqual(cfg1.fingerprint(), cfg2.fingerprint())
        cfg3 = config(batch_rows=4)
        self.assertNotEqual(cfg1.fingerprint(), cfg3.fingerprint())


def write_file(directory: str, name: str, data: bytes) -> str:
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


HEADER = b"supplier\tcorrection_id\tkey_hex\tvalue_hex\tnote\n"


def tsv_line(supplier=b"acme", cid=b"C1", key=b"61", value=b"62", note=b"n"):
    return b"\t".join((supplier, cid, key, value, note)) + b"\n"


class ClassificationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="import-unit-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def parse(self, data: bytes, **cfg_overrides):
        path = write_file(self.dir, "input.tsv", data)
        return classify_file(path, config(**cfg_overrides))

    # -- the three-way hex distinction --------------------------------------
    def test_empty_field_vs_empty_bytes_vs_illegal_hex(self):
        data = HEADER
        data += tsv_line(cid=b"E1", key=b"", value=b"01")          # empty field
        data += tsv_line(cid=b"E2", key=b"0x", value=b"0x")        # empty bytes
        data += tsv_line(cid=b"E3", key=b"abc", value=b"01")       # odd length
        data += tsv_line(cid=b"E4", key=b"0xzz", value=b"01")      # bad char
        parsed = self.parse(data)
        by_id = {row.correction_id: row for row in parsed.rows}
        self.assertEqual(by_id["E1"].status, STATUS_QUARANTINED)
        self.assertEqual(by_id["E1"].reason, REASON_EMPTY_FIELD)
        self.assertEqual(by_id["E2"].status, STATUS_IMPORTABLE)
        self.assertEqual(by_id["E2"].key, b"")
        self.assertEqual(by_id["E2"].value, b"")
        self.assertEqual(by_id["E3"].reason, REASON_HEX_ODD_LENGTH)
        self.assertEqual(by_id["E4"].reason, REASON_HEX_BAD_CHARACTER)
        # 0x-prefixed even hex decodes the same as plain hex.
        parsed2 = self.parse(HEADER + tsv_line(cid=b"E5", key=b"0x6162", value=b"6364"))
        self.assertEqual(parsed2.rows[0].key, b"ab")

    def test_prefixed_even_hex_and_odd_prefixed(self):
        data = HEADER
        data += tsv_line(cid=b"P1", key=b"0Xab", value=b"01")     # capital prefix bad
        parsed = self.parse(data)
        self.assertEqual(parsed.rows[0].reason, REASON_HEX_BAD_CHARACTER)
        data2 = HEADER + tsv_line(cid=b"P2", key=b"0xab", value=b"01")
        self.assertEqual(self.parse(data2).rows[0].status, STATUS_IMPORTABLE)

    # -- required-field and supplier checks ---------------------------------
    def test_empty_required_field_in_each_named_column(self):
        for column, kw in (
            ("supplier", {"supplier": b""}),
            ("correction", {"cid": b""}),
            ("value", {"value": b""}),
        ):
            parsed = self.parse(HEADER + tsv_line(**kw))
            self.assertEqual(parsed.rows[0].reason, REASON_EMPTY_FIELD, column)

    def test_supplier_mismatch_is_isolated(self):
        parsed = self.parse(HEADER + tsv_line(supplier=b"rival"))
        self.assertEqual(parsed.rows[0].reason, REASON_SUPPLIER_MISMATCH)
        # Without an expected supplier, any text passes.
        parsed2 = self.parse(
            HEADER + tsv_line(supplier=b"rival"),
            supplier={},
        )
        self.assertEqual(parsed2.rows[0].status, STATUS_IMPORTABLE)

    # -- structural rows -----------------------------------------------------
    def test_blank_lines_and_field_count_mismatch(self):
        data = HEADER + b"\n" + tsv_line() + b"acme\tC9\t61\t62\n"
        parsed = self.parse(data)
        self.assertEqual(parsed.rows[0].reason, REASON_BLANK_LINE)
        self.assertEqual(parsed.rows[1].status, STATUS_IMPORTABLE)
        self.assertEqual(parsed.rows[2].reason, REASON_FIELD_COUNT)

    def test_interior_carriage_return_is_preserved_not_trimmed(self):
        # Only the CRLF terminator is stripped; an interior \r is content.
        data = HEADER + b"acme\tC1\t61\t62\tnote\rare\r\n"
        parsed = self.parse(data)
        self.assertEqual(parsed.rows[0].raw_line, b"acme\tC1\t61\t62\tnote\rare")

    def test_no_trailing_newline_still_parses_last_line(self):
        parsed = self.parse(HEADER + tsv_line().rstrip(b"\n"))
        self.assertEqual(len(parsed.rows), 1)
        self.assertEqual(parsed.rows[0].status, STATUS_IMPORTABLE)

    def test_truncated_utf8_is_quarantined_and_kept_as_raw_bytes(self):
        data = HEADER + tsv_line(cid=b"ok") + b"acme\tbad\t61\t62\t\xff\xfe\n" + tsv_line(cid=b"after")
        parsed = self.parse(data)
        self.assertEqual(parsed.rows[1].reason, REASON_INVALID_UTF8)
        self.assertEqual(parsed.rows[1].field_indexes, (4,))
        self.assertIn(b"\xff", parsed.rows[1].raw_line)
        # The rows around it are unaffected.
        self.assertEqual(parsed.rows[0].status, STATUS_IMPORTABLE)
        self.assertEqual(parsed.rows[2].status, STATUS_IMPORTABLE)

    # -- duplicate vs conflict ----------------------------------------------
    def test_exact_duplicate_is_duplicate_not_conflict(self):
        line = tsv_line(cid=b"D1")
        parsed = self.parse(HEADER + line + line + line)
        self.assertEqual([r.status for r in parsed.rows],
                         [STATUS_IMPORTABLE, STATUS_DUPLICATE, STATUS_DUPLICATE])

    def test_differing_same_id_makes_all_rows_conflicts(self):
        a = tsv_line(cid=b"X1", key=b"61", value=b"62")
        b = tsv_line(cid=b"X1", key=b"61", value=b"63")
        parsed = self.parse(HEADER + a + b)
        self.assertEqual([r.status for r in parsed.rows], [STATUS_CONFLICT, STATUS_CONFLICT])
        self.assertEqual(parsed.conflict_ids, ["X1"])
        self.assertEqual(parsed.quarantine_reasons[REASON_CONFLICT], 2)

    def test_conflict_after_duplicates_poisons_the_whole_id(self):
        a = tsv_line(cid=b"X2")
        different = tsv_line(cid=b"X2", note=b"changed")
        parsed = self.parse(HEADER + a + a + different)
        self.assertTrue(all(r.status == STATUS_CONFLICT for r in parsed.rows))

    def test_duplicate_requires_every_field_identical(self):
        # Same key/value but a different extra column -> conflict.
        a = tsv_line(cid=b"X3", note=b"one")
        b = tsv_line(cid=b"X3", note=b"two")
        parsed = self.parse(HEADER + a + b)
        self.assertEqual(parsed.rows[0].status, STATUS_CONFLICT)

    # -- ordering, batching, raw retention -----------------------------------
    def test_importable_rows_stay_in_file_order_and_batch_plans(self):
        data = HEADER
        for i in range(7):
            data += tsv_line(cid=("C%02d" % i).encode(), key=("6%01d" % i).encode())
        parsed = self.parse(data, batch_rows=3)
        self.assertEqual([p["row_count"] for p in parsed.batches], [3, 3, 1])
        self.assertEqual(parsed.batches[0]["first_line"], 2)
        self.assertEqual(parsed.batches[-1]["last_line"], 8)

    def test_raw_line_bytes_are_retained_for_every_row(self):
        data = HEADER + tsv_line() + b"\n"
        parsed = self.parse(data)
        self.assertEqual(parsed.rows[0].raw_line, tsv_line().rstrip(b"\n"))
        self.assertEqual(parsed.rows[1].raw_line, b"")

    def test_fixture_mixed_file_classification(self):
        parsed = classify_file(fixture("corrections_mixed.tsv"), config())
        self.assertEqual(parsed.counts, {
            STATUS_IMPORTABLE: 6,
            STATUS_DUPLICATE: 2,
            STATUS_CONFLICT: 2,
            STATUS_QUARANTINED: 7,
        })
        reasons = parsed.quarantine_reasons
        self.assertEqual(reasons["blank_line"], 1)
        self.assertEqual(reasons["empty_field"], 1)
        self.assertEqual(reasons["field_count_mismatch"], 1)
        self.assertEqual(reasons["hex_bad_character"], 1)
        self.assertEqual(reasons["hex_odd_length"], 2)
        self.assertEqual(reasons["supplier_mismatch"], 1)
        self.assertEqual(reasons["conflict"], 2)
        self.assertEqual(len(parsed.rows), 17)


class HeaderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="import-header-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = config()

    def test_missing_duplicate_and_unknown_columns_rejected(self):
        for name in ("bad_header_missing.tsv", "bad_header_duplicate.tsv",
                     "bad_header_unknown.tsv"):
            with self.assertRaises(ImportHeaderError):
                classify_file(fixture(name), self.cfg)

    def test_empty_file_is_a_header_error(self):
        path = write_file(self.dir, "empty.tsv", b"")
        with self.assertRaises(ImportHeaderError):
            classify_file(path, self.cfg)

    def test_header_with_truncated_utf8(self):
        path = write_file(self.dir, "h.tsv", b"supplier\tcorrection_id\tkey_hex\tvalue_hex\t\xff\n")
        with self.assertRaises(ImportHeaderError):
            classify_file(path, self.cfg)

    def test_header_error_issues_are_structured(self):
        path = write_file(self.dir, "h.tsv", b"supplier\tkey_hex\n")
        with self.assertRaises(ImportHeaderError) as ctx:
            classify_file(path, self.cfg)
        self.assertTrue(ctx.exception.issues)


class PathSafetyTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="import-path-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.inside = os.path.join(self.root, "input.tsv")
        with open(self.inside, "wb") as handle:
            handle.write(HEADER + tsv_line())

    def test_relative_path_inside_root_resolves(self):
        # Relative paths are interpreted against the process CWD; the root
        # only constrains the resolved location.  Use an absolute path here.
        resolved = resolve_input_path(self.inside, self.root)
        self.assertEqual(resolved, os.path.abspath(self.inside))

    def test_dotdot_escape_is_refused(self):
        outside = os.path.join(tempfile.gettempdir(), "outside.tsv")
        with open(outside, "wb") as handle:
            handle.write(b"x")
        self.addCleanup(os.remove, outside)
        with self.assertRaises(ImportPathError):
            resolve_input_path(os.path.join("..", "outside.tsv"), self.root)

    def test_absolute_outside_root_refused(self):
        outside = os.path.join(tempfile.gettempdir(), "abs-outside.tsv")
        with open(outside, "wb") as handle:
            handle.write(b"x")
        self.addCleanup(os.remove, outside)
        with self.assertRaises(ImportPathError):
            resolve_input_path(outside, self.root)

    def test_symlink_refused_by_default_and_allowed_explicitly(self):
        target = os.path.join(self.root, "target.tsv")
        with open(target, "wb") as handle:
            handle.write(HEADER + tsv_line())
        link = os.path.join(self.root, "link.tsv")
        os.symlink(target, link)
        with self.assertRaises(ImportPathError):
            resolve_input_path(link, self.root)
        resolved = resolve_input_path(link, self.root, allow_symlinks=True)
        self.assertEqual(resolved, os.path.abspath(link))

    def test_symlinked_directory_component_is_caught(self):
        real_dir = os.path.join(self.root, "realdir")
        os.mkdir(real_dir)
        target = os.path.join(real_dir, "f.tsv")
        with open(target, "wb") as handle:
            handle.write(HEADER + tsv_line())
        link_dir = os.path.join(self.root, "linkdir")
        os.symlink(real_dir, link_dir)
        with self.assertRaises(ImportPathError):
            resolve_input_path(os.path.join(link_dir, "f.tsv"), self.root)

    def test_missing_path_refused(self):
        with self.assertRaises(ImportPathError):
            resolve_input_path("nope.tsv", self.root)

    def test_directory_path_refused(self):
        with self.assertRaises(ImportPathError):
            resolve_input_path(".", self.root)


if __name__ == "__main__":
    unittest.main()
