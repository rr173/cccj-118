"""Shared helpers for the workbook recalculation tests.

Every fixture is assembled with :mod:`workbook_fixtures` (raw ZIP/XML) and
every expected value is hand-written, so no spreadsheet engine is used to
produce either the input bytes or the answers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

from workbook_fixtures import WorkbookBuilder  # noqa: E402


def run_cli(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "distagg", "workbook", *args],
        capture_output=True, text=True, env=env,
        cwd=cwd or tempfile.gettempdir(),
    )


def write_recipe(path, document):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class WorkbookTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="wb-test-")
        self.book_path = os.path.join(self.work, "book.xlsx")
        self.recipe_path = os.path.join(self.work, "recipe.json")
        self.output_path = os.path.join(self.work, "out.jsonl")

    def builder(self) -> WorkbookBuilder:
        return WorkbookBuilder()

    def save(self, builder) -> str:
        builder.write(self.book_path)
        return self.book_path

    def recipe(self, document) -> str:
        write_recipe(self.recipe_path, document)
        return self.recipe_path

    def recalc(self, *extra, mode="annotate"):
        args = ["recalc", self.book_path, self.recipe_path, self.output_path]
        if mode is not None:
            args += ["--mode", mode]
        args += list(extra)
        return run_cli(*args)

    def field(self, row, name):
        return row["cells"][name]
