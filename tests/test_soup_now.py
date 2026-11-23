import importlib.util
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from instagram_agent import storage

SPEC = importlib.util.spec_from_file_location(
    "soup_now", Path(__file__).resolve().parents[1] / "deploy/command-centre/soup_now.py"
)
soup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(soup)


class StopTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(storage.SCHEMA_PATH.read_text())
        self.addCleanup(self.conn.close)

    def add(self, draft_id, status="pending"):
        storage.insert_draft(
            self.conn,
            {
                "draft_id": draft_id,
                "category": "food",
                "topic": "Soup",
                "caption": "Soup",
                "hashtags": [],
                "slides": [],
                "review_score": 90,
                "scheduled_for": "2026-11-23T20:00:00+00:00",
            },
            "2026-11-23",
            "2026-11-23T09:00:00+00:00",
        )
        self.conn.execute("UPDATE drafts SET status=? WHERE draft_id=?", (status, draft_id))
        self.conn.commit()

    def test_rejects_only_selected_carousel_and_publisher_skips_it(self):
        self.add(soup.SOURCE_ID)
        self.add("other")
        with patch.object(soup.render, "can_render", return_value=False):
            result = soup.stop(self.conn)
        self.assertEqual(result["carousel_status"], "rejected")
        self.assertTrue(result["publisher_skips"])
        self.assertTrue(result["absent_from_due"])
        self.assertEqual(result["slides"], [])
        self.assertEqual(self.conn.execute("SELECT status FROM drafts WHERE draft_id='other'").fetchone()[0], "pending")

    def test_refuses_missing_or_published_source(self):
        with self.assertRaises(ValueError):
            soup.stop(self.conn)
        self.add(soup.SOURCE_ID, "published")
        with self.assertRaises(ValueError):
            soup.stop(self.conn)
        self.assertEqual(self.conn.execute("SELECT status FROM drafts").fetchone()[0], "published")
