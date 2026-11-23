import importlib.util
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "reel_now", Path(__file__).resolve().parents[1] / "deploy/command-centre/reel_now.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ReelNowTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE uploads(upload_id TEXT, draft_id TEXT, reel INTEGER, status TEXT);"
            "CREATE TABLE drafts(draft_id TEXT,kind TEXT);"
        )
        self.addCleanup(self.conn.close)

    def test_empty_queue_refuses_generation(self):
        with patch.object(runner.cli, "model_client") as model:
            with self.assertRaises(ValueError):
                runner.make(self.conn, {})
            model.assert_not_called()

    def test_selected_requires_matching_owner_upload(self):
        self.conn.execute("INSERT INTO uploads VALUES(?,?,1,'used')", (runner.UPLOAD_ID, "selected"))
        self.conn.execute("INSERT INTO drafts VALUES('selected','reel')")
        self.assertEqual(runner.selected(self.conn, "selected")["kind"], "reel")
        for draft_id in (runner.PROTECTED_DRAFT, "another", ""):
            with self.assertRaises(ValueError):
                runner.selected(self.conn, draft_id)

    def test_unrenderable_photos_never_fall_back(self):
        self.conn.execute("INSERT INTO uploads VALUES(?,NULL,1,'waiting')", (runner.UPLOAD_ID,))
        with patch.object(runner.render, "can_render", return_value=False):
            with self.assertRaisesRegex(ValueError, "refusing carousel"):
                runner.make(self.conn, {})

    def test_draft_is_held_and_only_selected_reel_is_rendered(self):
        self.conn.execute("INSERT INTO uploads VALUES(?,NULL,1,'waiting')", (runner.UPLOAD_ID,))
        draft = {"kind": "reel", "reel": {"render": "pending"}}
        row = {"reel": '{"render":"pending","hold":true}', "kind": "reel", "status": "pending",
               "scheduled_for": "later", "topic": "food", "caption": "caption", "hashtags": "#food"}
        with patch.object(runner.render, "can_render", return_value=True), \
             patch.object(runner.cli, "model_client", return_value=object()), \
             patch.object(runner.drafts, "load_rules", return_value=""), \
             patch.object(runner.cli, "_read", return_value=""), \
             patch.object(runner.drafts, "generate_from_upload", return_value=draft), \
             patch.object(runner.drafts, "store_upload_draft", return_value="selected") as store, \
             patch.object(runner, "selected", return_value=row), \
             patch.object(runner.render, "render", side_effect=RuntimeError("stop before file stat")) as render:
            with self.assertRaises(RuntimeError):
                runner.make(self.conn, {})
            self.assertTrue(store.call_args.args[1]["reel"]["hold"])
            self.assertEqual(render.call_args.args[2], "selected")
