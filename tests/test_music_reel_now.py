import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


story = module("story_now", ROOT / "deploy/command-centre/story_now.py")
with patch.dict(sys.modules, story_now=story):
    runner = module("music_reel_now", ROOT / "deploy/command-centre/music_reel_now.py")


class MusicReelNowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "drafts"
        self.source = self.media / runner.SOURCE_ID
        self.source.mkdir(parents=True)
        (self.source / "slide-1.jpg").write_bytes(b"photo")
        public = self.root / "public"
        public.mkdir()
        (public / "index.html").write_text("existing")
        self.env = {
            "INSTAGRAM_AUTO_PUBLISH": "1",
            "INSTAGRAM_ACCESS_TOKEN": "fake",
            "INSTAGRAM_USER_ID": "1",
            "INSTAGRAM_PUBLIC_DIR": str(public),
            "INSTAGRAM_PUBLIC_URL": "https://example.com",
        }
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "instagram_agent/schema.sql").read_text())
        self.conn.execute(
            "INSERT INTO drafts(draft_id,created_on,created_at,category,topic,slides,caption,hashtags,status,kind,media_id,reel) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                runner.SOURCE_ID,
                "2026-11-23",
                "2026-11-23T13:00:00+00:00",
                "coffee",
                "latte",
                "[{}]",
                "same caption",
                "#coffee",
                "published",
                "reel",
                runner.SOURCE_MEDIA,
                json.dumps({"source": "photos", "photos": 1, "render": "done", "file": "reel.mp4", "seconds": 9}),
            ),
        )
        self.conn.commit()
        self.original = dict(self.conn.execute("SELECT * FROM drafts").fetchone())
        self.addCleanup(patch.stopall)
        patch.object(runner, "MEDIA", self.media).start()
        patch.object(runner.render, "can_render", return_value=True).start()

    def test_track_rejects_empty_and_mismatched_audio(self):
        for data in (b"", b"not audio", b"0000ftypM4A " + b"0" * 30):
            with self.assertRaises(ValueError):
                runner.validate_track(data)

    def test_existing_public_index_is_mandatory(self):
        (Path(self.env["INSTAGRAM_PUBLIC_DIR"]) / "index.html").unlink()
        with self.assertRaisesRegex(ValueError, "index.html required"):
            runner.preflight(self.conn, self.env)
        self.assertFalse((self.media / runner.NEW_ID).exists())

    def test_new_reel_and_story_leave_source_unchanged(self):
        result = {}

        def render(conn, media, draft_id, reel):
            (media / draft_id / "reel.mp4").write_bytes(b"rendered")
            self.assertEqual(reel["music"]["mix"], "replace")
            return {**reel, "render": "done", "file": "reel.mp4"}

        def publish(client, row, *args, **kwargs):
            self.assertEqual(row["draft_id"], runner.NEW_ID)
            if "on_publish" in kwargs:
                kwargs["on_publish"]()
                return "new-reel"
            self.assertIsInstance(client, runner.StoryClient)
            return "new-story"

        with (
            patch.object(runner, "validate_track"),
            patch.object(runner.render, "render", side_effect=render),
            patch.object(runner.video, "check", return_value=({"audio_codec": "aac", "bytes": 8, "seconds": 9}, [])),
            patch.object(runner.cli, "graph_client", return_value=Mock(user_id="owner")),
            patch.object(runner.publish, "publish_reel", side_effect=publish) as send,
            patch.object(
                runner.publish, "permalink", side_effect=lambda client, media_id: "https://example.com/" + media_id
            ),
        ):
            runner.execute(self.conn, self.env, b"0000ftypM4A " + b"0" * 30, result)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result["outcome"], "published")
        self.assertEqual(result["story"]["media_id"], "new-story")
        self.assertEqual(result["render"]["audio_codec"], "aac")
        current = dict(self.conn.execute("SELECT * FROM drafts WHERE draft_id=?", (runner.SOURCE_ID,)).fetchone())
        self.assertEqual(current, self.original)
        runs = self.conn.execute("SELECT draft_id,outcome FROM publish_runs ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in runs], [(runner.NEW_ID, "started"), (runner.NEW_ID, "published")])
        self.assertEqual((self.source / "slide-1.jpg").read_bytes(), b"photo")
        with self.assertRaisesRegex(ValueError, "refusing rerun"):
            runner.preflight(self.conn, self.env)

    def test_failed_final_publish_stops_without_retry(self):
        self.conn.execute("UPDATE drafts SET draft_id=?,status='approved'", (runner.NEW_ID,))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM drafts").fetchone()
        result = {}

        def fail(*args, **kwargs):
            kwargs["on_publish"]()
            raise RuntimeError("outcome unknown")

        with patch.object(runner.publish, "publish_reel", side_effect=fail) as send:
            with self.assertRaises(RuntimeError):
                runner.publish_new(self.conn, self.env, row, Mock(), result)
        send.assert_called_once()
        self.assertEqual(result["reel"]["outcome"], "unknown")
        self.assertEqual(self.conn.execute("SELECT status FROM drafts").fetchone()[0], "failed")
