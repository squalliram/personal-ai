import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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

    def test_an_alert_that_was_not_stored_is_retried(self):
        self.publish()
        graph = InsightsGraph({"reach": 500})
        at24 = PUBLISHED + timedelta(hours=24, minutes=5)
        with mock.patch.object(post_report, "notify", return_value=None):
            self.assertEqual(post_report.tick(self.conn, self.env, at24, client=graph), "reports 0, report errors 1")
        self.assertIsNone(self.conn.execute("SELECT notified_at FROM post_reports").fetchone()[0])
        self.assertEqual(post_report.tick(self.conn, self.env, at24 + timedelta(hours=1), client=graph), "reports 1")
        self.assertEqual([r[3] for r in self.inbox()], ["instagram-post-report:9001:24h"])

    def test_unavailable_profile_visits_does_not_drop_zero_follows_or_fail_the_report(self):
        self.publish()

        class Graph(InsightsGraph):
            def get(self, path_or_url, params=None):
                if params.get("metric") == "profile_visits":
                    raise GraphAPIError("Metric unsupported for this media")
                return super().get(path_or_url, params)

        at = PUBLISHED + timedelta(hours=25)
        self.assertEqual(
            post_report.tick(self.conn, self.env, at, client=Graph({"reach": 500, "follows": 0})), "reports 1"
        )
        stored = self.conn.execute("SELECT follows, profile_visits FROM post_reports").fetchone()
        self.assertEqual(tuple(stored), (0, None))
        self.assertIn("follows 0", self.inbox()[0][1])

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

    def test_a_failure_alert_that_was_not_stored_is_retried_without_refetching(self):
        self.publish()
        graph = InsightsGraph({}, fail=True)
        start = PUBLISHED + timedelta(hours=24)
        with mock.patch.object(post_report, "notify", return_value=None):
            for tick in range(post_report.MAX_ATTEMPTS + 1):
                post_report.tick(self.conn, self.env, start + timedelta(minutes=15 * tick), client=graph)
        self.assertEqual(len(graph.calls), post_report.MAX_ATTEMPTS)
        post_report.tick(self.conn, self.env, start + timedelta(days=10), client=graph)  # past both windows
        self.assertEqual(len(graph.calls), post_report.MAX_ATTEMPTS)
        self.assertEqual([r[0] for r in self.inbox()], ["failed"])
        self.assertEqual(post_report.tick(self.conn, self.env, start + timedelta(days=30), client=graph), "reports 0")

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

    def test_a_new_post_is_compared_with_older_posts_at_the_same_checkpoint(self):
        for n, media_id in enumerate(("9001", "9002", "9003")):
            at = PUBLISHED - timedelta(days=10 - n)
            self.publish(media_id, at=at)
            graph = InsightsGraph({"reach": 100, "likes": 10, "saved": 5}, at=at)
            post_report.tick(self.conn, self.env, at + timedelta(hours=25), client=graph)
            post_report.tick(self.conn, self.env, at + timedelta(hours=73), client=graph)
        self.publish("9004")
        graph = InsightsGraph({"reach": 1000, "likes": 100, "saved": 50})
        post_report.tick(self.conn, self.env, PUBLISHED + timedelta(hours=25), client=graph)
        posts = {p["media_id"]: p for p in learnings.measured(self.conn, PUBLISHED + timedelta(hours=26))}
        self.assertEqual(posts["9004"]["checkpoint"], "24h")
        self.assertGreater(posts["9004"]["lift"], 2)
        self.assertEqual(posts["9001"]["checkpoint"], "72h")

    def test_owner_photo_hooks_come_from_the_caption_not_alt_text(self):
        post = {"owner": True, "kind": "carousel", "caption": "Pistachio croissant run\nMore below", "slides": '[{"text": "A croissant on a plate"}]'}  # fmt: skip
        self.assertEqual(learnings._hook(post), "Pistachio croissant run")
        self.assertEqual(learnings._hook({**post, "owner": False}), "A croissant on a plate")
        self.assertEqual(learnings._hook({**post, "kind": "reel"}), "A croissant on a plate")

    def test_old_posts_leave_the_window(self):
        self.measure("9001", 1000, days=200)
        self.assertEqual(learnings.summary(self.conn, PUBLISHED)["posts_measured"], 0)

    def test_growth_uses_all_checkpoints_as_peers_and_counts_each_post_once(self):
        for n in range(6):
            at = PUBLISHED - timedelta(days=30 - n)
            self.publish(str(9600 + n), at=at, tags=("#latte",) if n < 3 else ("#other",))
            graph24 = InsightsGraph({"reach": 100, "follows": 3 if n < 3 else 0}, at=at)
            graph72 = InsightsGraph({"reach": 200, **({"follows": 0} if n >= 3 else {})}, at=at)
            post_report.tick(self.conn, self.env, at + timedelta(hours=25), client=graph24)
            post_report.tick(self.conn, self.env, at + timedelta(hours=73), client=graph72)
        self.publish("9700")
        post_report.tick(
            self.conn, self.env, PUBLISHED + timedelta(hours=25), client=InsightsGraph({"reach": 100, "follows": 0})
        )
        findings = learnings.summary(self.conn, PUBLISHED + timedelta(hours=25))["growth_findings"]["follows"]
        latte = next(row for row in findings if row["value"] == "#latte")
        self.assertEqual((latte["posts"], latte["vs_peer_rate"]), (3, 2.33))

    def test_follows_rank_tag_caption_and_slot_above_reach_without_using_missing_values(self):
        for n in range(11):
            strong = n < 5
            local = (PUBLISHED - timedelta(days=20 - n)).astimezone(learnings.LONDON)
            at = local.replace(hour=15 if strong else 18).astimezone(timezone.utc)
            tag = "#latte" if strong else "#general"
            media_id = str(9100 + n)
            draft_id = self.publish(media_id, at=at, tags=(tag,))
            if strong:
                self.conn.execute(
                    "UPDATE drafts SET caption=? WHERE draft_id=?",
                    ("Coffee after lunch. Follow @coffee.club for more.", draft_id),
                )
            counts = {"reach": 100 if strong else 1000, "saved": 0 if strong else 400}
            if n != 10:
                counts["follows"] = 2 if strong else 0
            post_report.tick(self.conn, self.env, at + timedelta(hours=73), client=InsightsGraph(counts, at=at))
        ctx = drafts.context(self.conn, PUBLISHED)
        findings = ctx["performance_learnings"]["growth_findings"]
        follows = {(row["feature"], row["value"]): row for row in findings["follows"]}
        self.assertEqual(follows["hashtag", "#latte"]["vs_peer_rate"], 2)
        self.assertIn(("caption ask", "follow"), follows)
        self.assertIn(("caption mention", "@coffee.club"), follows)
        self.assertNotIn(("hashtag", "#general"), follows)
        self.assertIn("#general", [row["value"] for row in findings["saved"]])

        ai = mock.Mock()
        ai.chat_json.return_value = {"hashtags": [["#latte"]]}
        draft = {"topic": "Latte", "category": "coffee", "caption": "Follow for more coffee."}
        drafts.tag(ai, ctx, [draft])
        payload = json.loads(ai.chat_json.call_args.args[1])
        self.assertEqual(payload["drafts"][0]["growth_findings"]["follows"][0]["value"], "#latte")
        self.assertEqual(draft["hashtags"], ["#latte"])
        self.assertEqual(drafts.publish_slots(self.conn, PUBLISHED.replace(hour=9), 1), ["2026-11-12T15:00+00:00"])

    def test_hashtag_conversion_evidence_stays_with_the_drafts_format(self):
        posts = []
        for fmt in ("carousel", "reel"):
            for n in range(10):
                latte = n < (6 if fmt == "reel" else 3)
                posts.append(
                    {
                        "media_id": f"{fmt}{n}",
                        "checkpoint": "24h",
                        "posted": PUBLISHED,
                        "format": fmt,
                        "reach": 100,
                        "follows": 10 if latte == (fmt == "reel") else 0,
                        "hashtags": "#latte" if latte else "#other",
                    }
                )
        summary = learnings.summary(self.conn, PUBLISHED)
        summary["growth_findings"] = learnings.growth_findings(posts)
        ctx = {"performance_learnings": learnings.for_prompt(summary), "trending_hashtags": []}
        ai = mock.Mock()
        ai.chat_json.return_value = {}
        draft = {"topic": "Latte", "category": "coffee", "caption": "Follow for more coffee."}
        drafts.tag(ai, ctx, [{**draft, "kind": "reel"}, draft.copy(), {**draft, "slides": [{"text": "Photo"}]}])
        reel, carousel, photo = json.loads(ai.chat_json.call_args.args[1])["drafts"]
        self.assertEqual([f["value"] for f in reel["growth_findings"]["follows"]], ["#latte"])
        self.assertEqual([f["value"] for f in carousel["growth_findings"]["follows"]], ["#other"])
        self.assertEqual(photo["growth_findings"], {})

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
        at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)  # summary() uses the wall clock
        self.publish(at=at)
        post_report.tick(self.conn, self.env, at + timedelta(hours=25), client=InsightsGraph({"reach": 77}, at=at))
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
