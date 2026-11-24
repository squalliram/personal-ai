import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from command_centre.instagram_analytics import MAX_MEDIA, performance
from instagram_agent import storage

NOW = datetime(2026, 11, 24, 18, tzinfo=timezone.utc)
POSTED = "2026-11-20T19:00:00+00:00"


class InstagramAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(storage.SCHEMA_PATH.read_text())
        self.addCleanup(self.conn.close)

    def post(self, media="reel", posted=POSTED):
        self.conn.execute(
            "INSERT INTO posts(media_id,posted_at,media_product_type,synced_at) VALUES(?,?,?,?)",
            (media, posted, "REELS", NOW.isoformat()),
        )

    def report(self, media, checkpoint, fmt="reel", views=100):
        self.conn.execute(
            "INSERT INTO post_reports(media_id,checkpoint,format,posted_at,captured_at,views,saved) "
            "VALUES(?,?,?,?,?,?,0)",
            (media, checkpoint, fmt, POSTED, "2026-11-21T18:00:00+00:00", views),
        )

    def test_merges_media_without_double_counting_checkpoints_or_matching_story(self):
        self.post()
        self.report("reel", "24h", views=100)
        self.report("reel", "72h", views=200)
        self.report("story", "24h", fmt="story", views=50)
        self.report("story", "72h", fmt="story", views=50)
        result = performance(self.conn, NOW)
        items = {item["media_id"]: item for item in result["items"]}
        self.assertEqual(len(items), 2)
        self.assertEqual(items["reel"]["snapshots"]["24h"]["views"], 100)
        self.assertEqual(items["reel"]["snapshots"]["72h"]["views"], 200)
        self.assertEqual(items["story"]["format"], "story")
        self.assertEqual(items["story"]["snapshots"]["24h"]["captured_at"], "2026-11-21T18:00:00+00:00")
        self.assertEqual(result["last_synced_at"], NOW.isoformat())

    def test_missing_metrics_are_not_zero_and_unmeasured_media_stay_visible(self):
        self.post("unmeasured")
        self.report("reel", "24h", views=None)
        items = {item["media_id"]: item for item in performance(self.conn, NOW)["items"]}
        self.assertEqual(items["unmeasured"]["snapshots"], {})
        self.assertIsNone(items["reel"]["snapshots"]["24h"]["views"])
        self.assertIsNone(items["reel"]["snapshots"]["24h"]["follows"])
        self.assertEqual(items["reel"]["snapshots"]["24h"]["saved"], 0)

    def test_uses_observed_post_time_not_publish_attempt_time(self):
        self.post()
        storage.insert_draft(
            self.conn,
            {
                "draft_id": "a" * 16,
                "topic": "Coffee",
                "category": "coffee",
                "slides": [],
                "caption": "",
                "hashtags": [],
                "review_score": 90,
                "kind": "reel",
            },
            "2026-11-20",
            POSTED,
        )
        self.conn.execute(
            "INSERT INTO publish_runs(draft_id,started_at,outcome,media_id) VALUES(?,?,?,?)",
            ("a" * 16, "2026-11-20T18:58:00+00:00", "published", "reel"),
        )
        (item,) = performance(self.conn, NOW)["items"]
        self.assertEqual(item["posted_at"], POSTED)
        self.assertEqual(item["time_basis"], "instagram")
        self.assertEqual(item["topic"], "Coffee")

    def test_limits_window_and_payload(self):
        self.post("old", (NOW - timedelta(days=91)).isoformat())
        self.post("future", (NOW + timedelta(days=1)).isoformat())
        self.post("invalid", "not a date")
        for index in range(MAX_MEDIA + 1):
            self.post(str(index), (NOW - timedelta(minutes=index)).isoformat())
        result = performance(self.conn, NOW)
        self.assertEqual(len(result["items"]), MAX_MEDIA)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["items"][0]["media_id"], "0")

    def test_old_or_empty_databases_do_not_get_migrated(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE posts(media_id TEXT,posted_at TEXT,synced_at TEXT)")
        conn.execute("INSERT INTO posts VALUES('old',?,?)", (POSTED, POSTED))
        before = conn.total_changes
        (item,) = performance(conn, NOW)["items"]
        self.assertEqual(item["format"], "post")
        self.assertEqual(item["snapshots"], {})
        self.assertEqual(conn.total_changes, before)
        self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 1)
