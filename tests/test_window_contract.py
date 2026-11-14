"""API-level contract tests: path guards, workspace isolation and references."""

import json
import os
import tempfile
import unittest

from distagg.windowing import (
    WindowConfig,
    WindowError,
    load_declaration,
    run_window_job,
)
from distagg.windowing.declaration import parse_declaration

DECLARATION = {
    "partition_by": ["g"],
    "order_by": "ts",
    "value": "amount",
    "lag_lead_k": 1,
    "windows": {"w": {"mode": "ROWS", "start": 0, "end": 0,
                      "exclude": "none"}},
}


class PathAndWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="window-api-")
        self.in_path = os.path.join(self.work, "in.jsonl")
        with open(self.in_path, "w", encoding="utf-8") as handle:
            for i in range(1, 4):
                handle.write(json.dumps(
                    {"g": "a", "ts": i, "amount": i}) + "\n")
        self.decl_path = os.path.join(self.work, "decl.json")
        with open(self.decl_path, "w", encoding="utf-8") as handle:
            json.dump(DECLARATION, handle)

    def test_same_path_rejected_at_api_level(self):
        with self.assertRaises(WindowError):
            run_window_job(self.in_path, self.in_path,
                           parse_declaration(DECLARATION))

    def test_symlink_aliasing_same_file_rejected(self):
        alias = os.path.join(self.work, "alias.jsonl")
        os.symlink(self.in_path, alias)
        with self.assertRaises(WindowError):
            run_window_job(self.in_path, alias, parse_declaration(DECLARATION))

    def test_workspace_only_removes_its_own_directory(self):
        # Pre-existing content in the chosen work-dir must survive.
        shared = tempfile.mkdtemp(prefix="window-shared-")
        sentinel = os.path.join(shared, "keep-me")
        with open(sentinel, "w") as handle:
            handle.write("important")
        out_path = os.path.join(self.work, "out.jsonl")
        run_window_job(
            self.in_path, out_path, load_declaration(self.decl_path),
            WindowConfig(batch_size=2, spill_threshold=1),
            work_dir=shared,
        )
        self.assertTrue(os.path.exists(sentinel))
        with open(sentinel) as handle:
            self.assertEqual(handle.read(), "important")
        # The private workspace directory is gone; only the sentinel remains.
        entries = os.listdir(shared)
        self.assertEqual(entries, ["keep-me"])
        self.assertTrue(os.path.exists(out_path))

    def test_existing_output_replaced_only_on_success(self):
        out_path = os.path.join(self.work, "out.jsonl")
        with open(out_path, "w") as handle:
            handle.write("old result\n")
        run_window_job(self.in_path, out_path, parse_declaration(DECLARATION))
        with open(out_path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["source_row"] for r in rows], [1, 2, 3])

    def test_invalid_config_rejected(self):
        for kwargs in (
            {"batch_size": 0},
            {"spill_threshold": -1},
            {"sqlite_cache_size_kib": 0},
            {"max_row_bytes": 0},
            {"batch_size": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(WindowError):
                    WindowConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
