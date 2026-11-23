import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from instagram_agent import music, storage

SPEC = importlib.util.spec_from_file_location(
    "soup_now", Path(__file__).resolve().parents[1] / "deploy/command-centre/soup_now.py"
)
soup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(soup)


class Fixture(unittest.TestCase):
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


class StopTests(Fixture):
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


class PublishTests(Fixture):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.media = self.root / "drafts"
        source = self.media / soup.SOURCE_ID
        source.mkdir(parents=True)
        (source / "slide-1.jpg").write_bytes(b"\xff\xd8\xffsource")
        self.public = self.root / "public"
        self.public.mkdir()
        (self.public / "index.html").write_text("")
        self.env = {
            "INSTAGRAM_AUTO_PUBLISH": "1",
            "INSTAGRAM_ACCESS_TOKEN": "test",
            "INSTAGRAM_USER_ID": "123",
            "INSTAGRAM_PUBLIC_DIR": str(self.public),
            "INSTAGRAM_PUBLIC_URL": "https://example.com/video",
        }
        self.add(soup.SOURCE_ID, "rejected")
        self.conn.execute("UPDATE drafts SET slides=?", (json.dumps([{"file": "slide-1.jpg", "text": "Soup"}]),))
        self.conn.commit()
        track_id = music.save(
            self.conn,
            self.media,
            "Overcast Cafe",
            "CC0",
            "https://example.com",
            b"ID3test",
            soup.datetime.now(soup.timezone.utc),
        )
        for name, value in (("MEDIA", self.media), ("TRACK_ID", track_id)):
            patcher = patch.object(soup, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(soup.render, "can_render", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preserves_source_and_publishes_same_rendered_video_once(self):
        source = dict(self.conn.execute("SELECT * FROM drafts WHERE draft_id=?", (soup.SOURCE_ID,)).fetchone())
        client = Mock(user_id="123")
        client.post.side_effect = [{"id": "reel-id"}, {"id": "story-id"}]
        seen = []

        def send(wrapper, row, media, public, url, **kwargs):
            seen.append((wrapper.story, row["reel"], media))
            if kwargs.get("on_publish"):
                kwargs["on_publish"]()
            return wrapper.post("123/media_publish", {"creation_id": "container"})["id"]

        with (
            patch.object(
                soup.render, "render", side_effect=lambda c, m, d, r: {**r, "render": "done", "file": "reel.mp4"}
            ),
            patch.object(soup.video, "check", return_value=({"audio_codec": "aac", "seconds": 7, "bytes": 1234}, [])),
            patch.object(soup.cli, "graph_client", return_value=client),
            patch.object(soup.publish, "publish_reel", side_effect=send),
            patch.object(soup.publish, "permalink", side_effect=lambda c, media: "https://example.com/" + media),
        ):
            result = {}
            soup.execute(self.conn, self.env, result)
        self.assertEqual(result["outcome"], "published")
        self.assertEqual((result["reel"]["media_id"], result["story"]["media_id"]), ("reel-id", "story-id"))
        self.assertEqual(seen[0][1:], seen[1][1:])
        self.assertEqual([v[0] for v in seen], [False, True])
        self.assertTrue(all(call.kwargs == {"retry": False} for call in client.post.call_args_list))
        self.assertEqual(
            dict(self.conn.execute("SELECT * FROM drafts WHERE draft_id=?", (soup.SOURCE_ID,)).fetchone()), source
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM drafts WHERE draft_id=?", (soup.NEW_ID,)).fetchone()[0], "published"
        )
        self.assertEqual((self.media / soup.NEW_ID / "slide-1.jpg").read_bytes(), b"\xff\xd8\xffsource")
        with self.assertRaises(ValueError):
            soup.preflight(self.conn, self.env)

    def test_missing_licence_or_source_image_stops_before_creating_replacement(self):
        (self.media / soup.SOURCE_ID / "slide-1.jpg").unlink()
        with self.assertRaises(ValueError):
            soup.execute(self.conn, self.env, {})
        self.assertFalse((self.media / soup.NEW_ID).exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1)


class OnceTests(unittest.TestCase):
    def test_story_parameters_and_never_retry_after_uncertain_publish(self):
        client = Mock(user_id="123")
        wrapper = soup.OnceClient(client, story=True)
        wrapper.post(
            "123/media", {"media_type": "REELS", "video_url": "https://example.com/reel.mp4", "caption": "Soup"}
        )
        client.post.assert_called_once_with(
            "123/media", {"media_type": "STORIES", "video_url": "https://example.com/reel.mp4"}, retry=False
        )
        client.post.side_effect = RuntimeError("connection interrupted")
        with self.assertRaises(RuntimeError):
            wrapper.post("123/media_publish", {"creation_id": "container"})
        with self.assertRaises(soup.publish.PublishError):
            wrapper.post("123/media_publish", {"creation_id": "container"})
        self.assertEqual(client.post.call_count, 2)
