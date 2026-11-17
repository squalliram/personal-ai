import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from command_centre_fixtures import Client, Clock, make_app, make_settings

from command_centre.security import Permission
from instagram_agent import cli, drafts, learnings, post_report, storage
from instagram_agent.graph import GraphAPIError, GraphClient

PUBLISHED = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)


class InsightsGraph(GraphClient):
    """Read-only fake: media fields and insights per media ID, or an error."""

    def __init__(self, metrics, *, fail=False, product="FEED", media_type="CAROUSEL_ALBUM", at=PUBLISHED):
        super().__init__("IGtoken-secret", "17841408862128347")
        self.timestamp = at.strftime("%Y-%m-%dT%H:%M:04+0000")
        self.metrics, self.fail, self.product, self.media_type, self.calls = metrics, fail, product, media_type, []

    def get(self, path_or_url, params=None):
        self.calls.append((path_or_url, dict(params or {})))
        if self.fail:
            raise GraphAPIError("Graph API request failed (HTTP 500)")
        if path_or_url.endswith("/insights"):
            names = params["metric"].split(",")
            return {"data": [{"name": n, "values": [{"value": v}]} for n, v in self.metrics.items() if n in names]}
        return {
            "id": path_or_url,
            "timestamp": self.timestamp,
            "permalink": f"https://www.instagram.com/p/{path_or_url}/",
            "media_type": self.media_type,
            "media_product_type": self.product,
            "caption": "Save this.",
        }

    def post(self, path, params, *, retry=True):
        raise AssertionError("post reports must never write to Instagram")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "instagram.sqlite3"
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.notes = self.root / "notifications.sqlite3"
        self.env = {"COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.notes)}

    def publish(self, media_id="9001", *, at=PUBLISHED, topic="Flat whites", kind="carousel", hook="Flat white 101",
                tags=("#coffee", "#london"), category="coffee", words=100):  # fmt: skip
        draft_id = f"{int(media_id):016x}"
        with self.conn:
            storage.insert_draft(
                self.conn,
                {
                    "draft_id": draft_id,
                    "category": category,
                    "topic": topic,
                    "slides": [{"text": hook}, {"text": "two"}, {"text": "three"}],
                    "caption": " ".join(["word"] * words),
                    "hashtags": list(tags),
                    "review_score": 80,
                    "scheduled_for": at.isoformat(timespec="minutes"),
                    "kind": kind,
                    "reel": {"seconds": 12.0} if kind == "reel" else None,
                },
                at.date().isoformat(),
                (at - timedelta(hours=2)).isoformat(timespec="seconds"),
            )
            self.conn.execute(
                "INSERT INTO publish_runs(draft_id,started_at,outcome,media_id) VALUES(?,?,?,?)",
                (draft_id, at.isoformat(timespec="seconds"), "published", media_id),
            )
            self.conn.execute("UPDATE drafts SET status='published', media_id=? WHERE draft_id=?", (media_id, draft_id))
        return draft_id

    def inbox(self):
        with sqlite3.connect(self.notes) as conn:
            return conn.execute("SELECT outcome, title, link, dedupe_key FROM notifications ORDER BY id").fetchall()


class PostReportTests(Case):
    def test_nothing_is_due_before_24_hours(self):
        self.publish()
        self.assertEqual(post_report.tick(self.conn, self.env, PUBLISHED + timedelta(hours=23)), "reports 0")
        self.assertFalse(self.notes.exists())

    def test_reports_once_per_checkpoint_with_the_permalink(self):
        self.publish()
        graph = InsightsGraph({"reach": 500, "likes": 40, "comments": 3, "saved": 12, "shares": 2, "views": 900})
        at24 = PUBLISHED + timedelta(hours=24, minutes=5)
        self.assertEqual(post_report.tick(self.conn, self.env, at24, client=graph), "reports 1")
        self.assertEqual(post_report.tick(self.conn, self.env, at24 + timedelta(minutes=15), client=graph), "reports 0")
        at72 = PUBLISHED + timedelta(hours=72, minutes=5)
        self.assertEqual(post_report.tick(self.conn, self.env, at72, client=graph), "reports 1")
        rows = self.inbox()
        self.assertEqual([r[3] for r in rows], ["instagram-post-report:9001:24h", "instagram-post-report:9001:72h"])
        outcome, title, link, _ = rows[0]
        self.assertEqual((outcome, link), ("info", "https://www.instagram.com/p/9001/"))
        self.assertIn("24h report: Flat whites (carousel, posted 12 Nov 18:00)", title)
        self.assertIn("reach 500", title)
        self.assertIn("No earlier carousels to compare with yet.", title)
        self.assertLessEqual(len(title), post_report.TITLE_LIMIT)
        stored = self.conn.execute("SELECT reach, saved, format FROM post_reports WHERE checkpoint='24h'").fetchone()
        self.assertEqual(tuple(stored), (500, 12, "carousel"))
        self.assertEqual(self.conn.execute("SELECT permalink FROM posts").fetchone()[0], rows[0][2])
        self.assertTrue(all(not path.endswith("media_publish") for path, _ in graph.calls))

    def test_times_are_london_time(self):
        summer = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)
        text = post_report.summarise("Iced latte", "reel", "24h", summer, {"reach": 1}, {"posts": 0, "medians": {}})
        self.assertIn("posted 01 Jul 18:00", text)

    def test_compares_with_the_same_format_median_and_labels_a_small_sample(self):
        for n, reach in enumerate((100, 200, 300), 1):
            self.publish(str(9000 + n), at=PUBLISHED - timedelta(days=10 - n))
            graph = InsightsGraph({"reach": reach, "saved": 10, "likes": 20})
            post_report.tick(
                self.conn, self.env, PUBLISHED - timedelta(days=10 - n) + timedelta(hours=25), client=graph
            )
        self.publish("9100", topic="Cortado guide")
        graph = InsightsGraph({"reach": 400, "saved": 5, "likes": 20})
        post_report.tick(self.conn, self.env, PUBLISHED + timedelta(hours=25), client=graph)
        title = self.inbox()[-1][1]
        self.assertIn("reach 400 (2.0x)", title)
        self.assertIn("Worked: reach.", title)
        self.assertIn("Didn't: saves.", title)
        self.assertIn("vs 24h median of 3 carousels, small sample", title)

    def test_reels_use_watch_time_and_compare_only_with_reels(self):
        self.publish("9001")
        post_report.tick(self.conn, self.env, PUBLISHED + timedelta(hours=25), client=InsightsGraph({"reach": 50}))
        self.publish("9002", kind="reel", at=PUBLISHED + timedelta(days=1))
        graph = InsightsGraph(
            {"reach": 900, "views": 2000, "ig_reels_avg_watch_time": 6200, "ig_reels_video_view_total_time": 9},
            product="REELS",
            media_type="VIDEO",
        )
        post_report.tick(self.conn, self.env, PUBLISHED + timedelta(days=2, hours=1), client=graph)
        title = self.inbox()[-1][1]
        self.assertIn("(reel,", title)
        self.assertIn("avg watch 6.2s", title)
        self.assertIn("No earlier reels to compare with yet.", title)
        row = self.conn.execute("SELECT avg_watch_ms FROM post_reports WHERE media_id='9002'").fetchone()
        self.assertEqual(row[0], 6200)

    def test_failed_fetches_retry_then_report_once(self):
        self.publish()
        graph = InsightsGraph({}, fail=True)
        start = PUBLISHED + timedelta(hours=24)
        for tick in range(post_report.MAX_ATTEMPTS + 2):
            post_report.tick(self.conn, self.env, start + timedelta(minutes=15 * tick), client=graph)
        rows = self.inbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "failed")
        self.assertIn("24h report not made", rows[0][1])
        self.assertEqual(len(graph.calls), post_report.MAX_ATTEMPTS)

    def test_missed_checkpoints_are_skipped_not_backfilled(self):
        self.publish()
        graph = InsightsGraph({"reach": 10})
        post_report.tick(self.conn, self.env, PUBLISHED + timedelta(hours=80), client=graph)
        self.assertEqual([r[3].rsplit(":", 1)[1] for r in self.inbox()], ["72h"])
        self.assertEqual(
            post_report.tick(self.conn, self.env, PUBLISHED + timedelta(days=30), client=graph), "reports 0"
        )

    def test_off_without_a_token_and_a_failure_never_breaks_the_daily_tick(self):
        self.publish()
        self.assertEqual(post_report.tick(self.conn, self.env, PUBLISHED + timedelta(hours=25)), "reports off")

        class Broken:
            def execute(self, *args):
                raise sqlite3.OperationalError("disk I/O error")

        self.assertEqual(cli._post_reports(Broken(), self.env), "reports failed (OperationalError)")

    def test_titles_stay_within_the_notification_limit(self):
        values = {m: 123456 for m in post_report.METRICS}
        base = {"posts": 2, "basis": "lifetime", "medians": {m: 1 for m in post_report.METRICS}}
        text = post_report.summarise("x" * 200, "carousel", "72h", PUBLISHED, values, base)
        self.assertLessEqual(len(text), post_report.TITLE_LIMIT)
        self.assertIn("lifetime median of 2 carousels, small sample", text)


class LearningsTests(Case):
    def measure(self, media_id, reach, *, days=0, **draft):
        at = PUBLISHED - timedelta(days=days)
        self.publish(media_id, at=at, **draft)
        graph = InsightsGraph({"reach": reach, "likes": reach // 10, "saved": reach // 20}, at=at)
        post_report.tick(self.conn, self.env, at + timedelta(hours=73), client=graph)

    def test_empty_before_any_report(self):
        found = learnings.summary(self.conn, PUBLISHED)
        self.assertEqual(found["posts_measured"], 0)
        self.assertIn("No post has been measured yet", found["sample_note"])
        self.assertEqual(learnings.for_prompt(found)["beat_median"], [])

    def test_finds_what_beats_the_median_and_labels_small_samples(self):
        self.measure("9001", 1000, days=8, hook="Best flat white", tags=("#flatwhite", "#coffee"))
        self.measure("9002", 900, days=7, hook="Cheap cortado", tags=("#flatwhite", "#coffee"))
        self.measure("9003", 100, days=6, hook="The complete guide to London coffee", tags=("#coffee",))
        self.measure("9004", 120, days=5, hook="Every brunch spot you must try", tags=("#coffee",))
        found = learnings.summary(self.conn, PUBLISHED + timedelta(days=4))
        self.assertEqual(found["posts_measured"], 4)
        self.assertTrue(found["small_sample"])
        self.assertIn("small sample", found["sample_note"])
        working = {(f["feature"], f["value"]) for f in found["working"]}
        self.assertIn(("hook length", "4 words or fewer"), working)
        self.assertIn(("hashtag", "#flatwhite"), working)
        weak = {(f["feature"], f["value"]) for f in found["not_working"]}
        self.assertIn(("hook length", "5-6 words"), weak)
        self.assertTrue(all(f["small_sample"] and "small sample" in f["text"] for f in found["working"]))
        self.assertEqual(found["best_posts"][0]["hook"], "Best flat white")
        self.assertEqual(len(found["reports"]), 4)
        prompt = learnings.for_prompt(found)
        self.assertNotIn("permalink", json.dumps(prompt))
        self.assertTrue(prompt["small_sample"])

    def test_formats_compare_across_the_account(self):
        self.measure("9001", 1000, days=8, kind="reel")
        self.measure("9002", 1200, days=7, kind="reel")
        self.measure("9003", 100, days=6)
        self.measure("9004", 150, days=5)
        found = learnings.summary(self.conn, PUBLISHED + timedelta(days=4))
        self.assertIn(("format", "reel"), {(f["feature"], f["value"]) for f in found["working"]})
        self.assertIn(("format", "carousel"), {(f["feature"], f["value"]) for f in found["not_working"]})

    def test_old_posts_leave_the_window(self):
        self.measure("9001", 1000, days=200)
        self.assertEqual(learnings.summary(self.conn, PUBLISHED)["posts_measured"], 0)

    def test_create_and_review_get_learnings_as_data_not_evidence(self):
        self.measure("9001", 1000, days=8)
        ctx = drafts.context(self.conn, PUBLISHED)
        self.assertEqual(ctx["performance_learnings"]["posts_measured"], 1)
        self.assertIn("performance_learnings", drafts.UNTRUSTED)
        self.assertIn("never evidence for a venue, dish, rating", drafts.UNTRUSTED)

        class AI:
            calls = []

            def chat_json(self, system, user, images=None):
                self.calls.append((system, user))
                return {"reviews": [{"score": 80, "notes": "ok"}]}

        ai = AI()
        draft = {"category": "coffee", "topic": "t", "caption": "c", "hashtags": [], "slides": [{"text": "h"}]}
        drafts.review(ai, ctx, [draft], rules="")
        system, payload = ai.calls[0]
        self.assertIn("performance_learnings", json.loads(payload))
        self.assertIn(drafts.UNTRUSTED, system)


class CommandCentreLearningsTests(Case):
    def test_instagram_page_shows_learnings_and_reports(self):
        self.publish()
        post_report.tick(self.conn, self.env, PUBLISHED + timedelta(hours=25), client=InsightsGraph({"reach": 77}))
        settings = make_settings(
            self.root, permission_ceiling=Permission.READ, instagram_db=self.db, instagram_media=self.root / "m"
        )
        client = Client(make_app(settings, clock=Clock()))
        client.login()
        response = client.request("GET", "/api/v1/instagram/drafts")
        self.assertEqual(response.status, 200)
        learned = response.json()["learnings"]
        self.assertEqual(learned["posts_measured"], 1)
        self.assertIn("small sample", learned["sample_note"])
        self.assertEqual(learned["reports"][0]["checkpoint"], "24h")
        self.assertIn("reach 77", learned["reports"][0]["summary"])

    def test_old_database_without_reports_table_still_lists(self):
        self.conn.execute("DROP TABLE post_reports")
        self.assertEqual(learnings.summary(self.conn)["posts_measured"], 0)


if __name__ == "__main__":
    unittest.main()
