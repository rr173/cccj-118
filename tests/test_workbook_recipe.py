"""Recipe validation: bad sheets, bands and field mappings fail early."""

import json
import os
import unittest

from workbook_helpers import WorkbookTestCase
from distagg.workbook.errors import RecipeError
from distagg.workbook.recipe import load_recipe, parse_recipe


def recipe(*selections):
    return {"sheets": list(selections)}


def selection(name="Costs", first=2, last=3, fields=None):
    return {
        "sheet": name,
        "first_row": first,
        "last_row": last,
        "fields": fields or [{"name": "a", "column": "A"}],
    }


class RecipeDocumentTest(WorkbookTestCase):
    def test_duplicate_target_field_rejected(self):
        document = recipe(selection(fields=[
            {"name": "total", "column": "A"},
            {"name": "total", "column": "B"},
        ]))
        with self.assertRaises(RecipeError) as caught:
            parse_recipe(document)
        self.assertIn("duplicate target field", str(caught.exception))

    def test_same_field_name_in_two_selections_is_allowed(self):
        document = recipe(selection(last=3), selection(last=5))
        parsed = parse_recipe(document)
        self.assertEqual(len(parsed.selections), 2)
        self.assertEqual(parsed.field_names(), ["a", "a"])

    def test_illegal_row_bands(self):
        cases = [
            selection(first=5, last=2),
            selection(first=0),
            selection(last=1048577),
        ]
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(RecipeError):
                    parse_recipe(recipe(document))

    def test_boolean_row_rejected(self):
        bad = selection()
        bad["first_row"] = True
        with self.assertRaises(RecipeError):
            parse_recipe(recipe(bad))

    def test_bad_columns(self):
        for column in ("A1", "$A", "XFE", "", "AA A", "1"):
            with self.subTest(column=column):
                with self.assertRaises(RecipeError):
                    parse_recipe(recipe(selection(fields=[
                        {"name": "x", "column": column}])))

    def test_unknown_members_rejected(self):
        document = recipe(selection())
        document["sheets"][0]["extra"] = 1
        with self.assertRaises(RecipeError):
            parse_recipe(document)
        document = recipe(selection())
        document["sheets"][0]["fields"][0]["unit"] = "EUR"
        with self.assertRaises(RecipeError):
            parse_recipe(document)
        with self.assertRaises(RecipeError):
            parse_recipe({"sheets": [selection()], "guess_header": True})

    def test_missing_or_wrong_types(self):
        cases = [
            {},
            {"sheets": []},
            {"sheets": {}},
            {"sheets": [{"sheet": "", "first_row": 1, "last_row": 2,
                         "fields": [{"name": "a", "column": "A"}]}]},
            {"sheets": [{"sheet": "S", "first_row": 1, "last_row": 2,
                         "fields": []}]},
            {"sheets": [{"sheet": "S", "first_row": 1, "last_row": 2,
                         "fields": [{"name": "", "column": "A"}]}]},
            {"sheets": [{"sheet": "S", "first_row": 1, "last_row": 2,
                         "fields": [{"name": "a", "column": 3}]}]},
        ]
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(RecipeError):
                    parse_recipe(document)

    def test_duplicate_json_member_rejected(self):
        path = os.path.join(self.work, "dup.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"sheets": [], "sheets": []}')
        with self.assertRaises(RecipeError):
            load_recipe(path)

    def test_missing_file_is_recipe_error(self):
        with self.assertRaises(RecipeError):
            load_recipe(os.path.join(self.work, "does-not-exist.json"))

    def test_error_path_is_reported(self):
        with self.assertRaises(RecipeError) as caught:
            load_recipe(self.recipe(recipe(selection(first=9, last=1))))
        self.assertEqual(caught.exception.path, self.recipe_path)

    def test_column_letters_are_case_insensitive(self):
        parsed = parse_recipe(recipe(selection(fields=[
            {"name": "a", "column": "f"}])))
        self.assertEqual(parsed.selections[0].fields[0].column, 6)
        self.assertEqual(parsed.selections[0].fields[0].column_letters, "F")

    def test_missing_sheet_rejected_before_output(self):
        builder = self.builder()
        builder.sheet("Costs").cell("A2", value="1")
        self.save(builder)
        self.recipe(recipe(selection(name="DoesNotExist")))
        proc = self.recalc()
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "recipe_error")
        self.assertIn("does not exist", payload["message"])
        self.assertFalse(os.path.exists(self.output_path))

    def test_sheet_name_matching_is_case_insensitive(self):
        builder = self.builder()
        sheet = builder.sheet("Costs")
        sheet.cell("A2", inline="x")
        self.save(builder)
        self.recipe(recipe(selection(name="cOsTs")))
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = read_jsonl_row(self.output_path)
        self.assertEqual(row["sheet"], "Costs")
        self.assertEqual(row["cells"]["a"]["value"], "x")


def read_jsonl_row(path):
    import json as _json

    with open(path, encoding="utf-8") as handle:
        return _json.loads(handle.readline())


if __name__ == "__main__":
    unittest.main()
