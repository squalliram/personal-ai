import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "story_now", Path(__file__).resolve().parents[1] / "deploy/command-centre/story_now.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class StoryNowTests(unittest.TestCase):
    def test_adapter_uses_story_and_never_retries(self):
        raw = Mock(user_id="owner")
        client = runner.StoryClient(raw)
        client.post("owner/media", {"video_url": "https://example.com/reel.mp4", "caption": "omit"})
        raw.post.assert_called_once_with(
            "owner/media", {"media_type": "STORIES", "video_url": "https://example.com/reel.mp4"}, retry=False
        )
        client.post("owner/media_publish", {"creation_id": "container"})
        raw.post.assert_called_with("owner/media_publish", {"creation_id": "container"}, retry=False)
        with self.assertRaises(runner.publish.PublishError):
            client.post("owner/media_publish", {"creation_id": "container"})
        with self.assertRaises(runner.publish.PublishError):
            client.post("owner/comments", {})

    def test_story_only_reads_selected_published_draft(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE drafts(draft_id,kind,status,media_id,reel);CREATE TABLE uploads(upload_id,draft_id,reel);"
        )
        with self.assertRaises(runner.publish.PublishError):
            runner.story(conn, {})
        conn.execute(
            "INSERT INTO drafts VALUES(?,?,?,?,?)",
            (
                runner.DRAFT_ID,
                "reel",
                "published",
                runner.REEL_MEDIA_ID,
                json.dumps({"render": "done", "file": "reel.mp4"}),
            ),
        )
        conn.execute("INSERT INTO uploads VALUES(?,?,1)", (runner.UPLOAD_ID, runner.DRAFT_ID))
        conn.commit()
        conn.execute("PRAGMA query_only=ON")
        with (
            patch.object(runner.publish, "configured", return_value=True),
            patch.object(runner.cli, "graph_client", return_value=Mock(user_id="owner")),
            patch.object(runner.publish, "publish_reel", return_value="story-id"),
            patch.object(runner.publish, "permalink", return_value=None),
        ):
            result = runner.story(
                conn, {"INSTAGRAM_PUBLIC_DIR": "/public", "INSTAGRAM_PUBLIC_URL": "https://example.com"}
            )
        self.assertEqual(result["outcome"], "published")
        self.assertEqual(result["media_id"], "story-id")
        self.assertIsNone(result["permalink"])

    def test_failed_final_publish_is_unknown_and_not_retried(self):
        raw = Mock(user_id="owner")
        raw.post.side_effect = RuntimeError("network error")
        client = runner.StoryClient(raw)
        with self.assertRaises(RuntimeError):
            client.post("owner/media_publish", {"creation_id": "container"})
        self.assertTrue(client.attempted)
        with self.assertRaises(runner.publish.PublishError):
            client.post("owner/media_publish", {"creation_id": "container"})
        self.assertEqual(raw.post.call_count, 1)
