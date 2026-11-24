import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from instagram_agent import content_plan, post_report, storage, weekly_performance
from instagram_agent.analysis import LONDON
from instagram_agent.pillar_config import RESERVE

MONDAY = datetime(2026, 3, 23, 12, tzinfo=LONDON)


class ContentPlanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = storage.connect(Path(directory.name) / "instagram.sqlite3")
        self.addCleanup(self.conn.close)

    def test_week_has_stable_london_slots_and_never_repeats_adjacent_pillars(self):
        with patch.object(content_plan.learnings, "growth_hours", side_effect=[[], [17], []]):
            for n in range(3):
                day = MONDAY + timedelta(weeks=n)
                content_plan.ensure_week(self.conn, day)
                content_plan.ensure_week(self.conn, day)
        entries = content_plan.rows(self.conn, MONDAY)
        timings = [datetime.fromisoformat(e["slot"]) for e in entries]
        pillars = [e["pillar"] for e in entries]
        self.assertEqual([t.weekday() for t in timings], [0, 2, 4] * 3)
        self.assertEqual([t.hour for t in timings], [16] * 3 + [17] * 3 + [16] * 3)
        self.assertNotEqual(timings[0].utcoffset(), timings[3].utcoffset())
        self.assertTrue(all(a != b for a, b in zip(pillars, pillars[1:], strict=False)))
        self.assertEqual(len(set(pillars[1::3])), 1)

    def add_draft(self, entry, draft_id, topic, render="done"):
        with self.conn:
            storage.insert_draft(
                self.conn,
                {
                    "draft_id": draft_id,
                    "plan_id": entry["id"],
                    "scheduled_for": entry["slot"],
                    "category": "food",
                    "topic": topic,
                    "slides": [],
                    "caption": "",
                    "hashtags": [],
                    "kind": "reel",
                    "reel": {"render": render},
                },
                entry["slot"][:10],
                entry["slot"],
            )

    def test_statuses_follow_manual_production_and_existing_publisher_states(self):
        content_plan.ensure_week(self.conn, MONDAY)
        entry = content_plan.slot_today(self.conn, MONDAY)
        self.add_draft(entry, "dish", "Soup", "pending")
        self.assertTrue(json.loads(self.conn.execute("SELECT reel FROM drafts").fetchone()[0])["hold"])
        self.assertIsNone(content_plan.slot_today(self.conn, MONDAY))
        self.assertEqual(content_plan.rows(self.conn, MONDAY)[0]["status"], "Scripted")
        self.assertEqual(content_plan.advance(self.conn, entry["id"], "Scripted"), "Filmed")
        self.assertEqual(content_plan.advance(self.conn, entry["id"], "Filmed"), "Edited")
        with self.conn:
            self.conn.execute("UPDATE drafts SET reel=? WHERE draft_id='dish'", (json.dumps({"render": "done"}),))
        with patch.object(content_plan, "notify") as notify:
            content_plan.notify_progress(self.conn, {}, MONDAY, True)
        self.assertEqual(
            [c.kwargs["key"] for c in notify.call_args_list],
            [f"instagram-plan-script:{entry['id']}", "instagram-plan-approval:dish"],
        )
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='approved' WHERE draft_id='dish'")
        self.assertEqual(content_plan.rows(self.conn, MONDAY)[0]["status"], "Approved")
        self.assertEqual(content_plan.rows(self.conn, MONDAY, True)[0]["status"], "Scheduled")
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='published' WHERE draft_id='dish'")
        self.assertEqual(content_plan.rows(self.conn, MONDAY, True)[0]["status"], "Posted")

    def test_sunday_report_uses_same_checkpoint_and_rotates_after_third_unique_loss(self):
        with patch.object(weekly_performance, "notify") as notify:
            for n in range(3):
                week = MONDAY + timedelta(weeks=n)
                content_plan.ensure_week(self.conn, week)
                best, worst, missing = [
                    e for e in content_plan.rows(self.conn, week) if e["week"] == week.date().isoformat()
                ]
                for entry, views, watch, name, hook in (
                    (best, 400, 5000, "Bowl A", "Which swirl?"),
                    (worst, 0, None, "Bowl B", "£3 soup"),
                    (missing, None, 25000, "Bowl C", "Attempt 1"),
                ):
                    ident = f"{n}-{entry['id']}"
                    self.add_draft(entry, ident, name)
                    with self.conn:
                        self.conn.execute(
                            "INSERT INTO post_reports(media_id,draft_id,checkpoint,format,posted_at,captured_at,"
                            "views,avg_watch_ms,saved,creative) VALUES(?,?,'24h','reel',?,?,?,?,?,?)",
                            (ident, ident, entry["slot"], entry["slot"], views, watch, 0, json.dumps({"hook": hook})),
                        )
                        self.conn.execute(
                            "INSERT INTO post_reports(media_id,draft_id,checkpoint,format,posted_at,captured_at,views) "
                            "VALUES(?,?,'72h','reel',?,?,9999)",
                            (ident, ident, entry["slot"], entry["slot"]),
                        )
                sunday = week + timedelta(days=6, hours=7)
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO account_followers VALUES(?,?,?)",
                        (sunday.date().isoformat(), sunday.isoformat(), 282 + n),
                    )
                weekly_performance.tick(self.conn, {}, sunday)
                weekly_performance.tick(self.conn, {}, sunday + timedelta(minutes=15))
            results, findings, advice = [c.args[2] for c in notify.call_args_list[-3:]]
            self.assertEqual(
                [c.kwargs["key"] for c in notify.call_args_list[-3:]],
                [c.kwargs["key"] for c in notify.call_args_list[-6:-3]],
            )
        self.assertIn("Bottom: Bowl B (0 views, n/a watch, n/a shares, 0 saves", results)
        self.assertNotIn("Bowl C", results)
        self.assertIn("choice/poll", findings)
        self.assertIn("follower change: +1", findings)
        self.assertIn("Replace", advice)
        change = self.conn.execute("SELECT * FROM content_pillar_replacements").fetchone()
        self.assertEqual((change["old_pillar"], change["new_pillar"]), (worst["pillar"], RESERVE[0]))
        self.assertEqual(change["effective_on"], (MONDAY + timedelta(weeks=3)).date().isoformat())

    def test_alternate_upload_counts_as_a_reel_but_not_as_its_slots_pillar(self):
        content_plan.ensure_week(self.conn, MONDAY)
        alternate, matched, _ = content_plan.rows(self.conn, MONDAY)
        for entry, ident, views in ((alternate, "alternate", 100), (matched, "matched", 40)):
            self.add_draft(entry, ident, ident)
            with self.conn:
                self.conn.execute(
                    "INSERT INTO post_reports(media_id,draft_id,checkpoint,format,posted_at,captured_at,views) "
                    "VALUES(?,?,'24h','reel',?,?,?)",
                    (ident, ident, entry["slot"], entry["slot"], views),
                )
        with self.conn:
            self.conn.execute(
                "UPDATE drafts SET reel=? WHERE draft_id='alternate'", (json.dumps({"pillar_match": False}),)
            )
        parts, worst = weekly_performance._make(self.conn, MONDAY.date())
        self.assertIn("Top: alternate (100 views", parts[0])
        self.assertIn(f"pillar {matched['pillar_name']}.", parts[1])
        self.assertIsNone(worst)

    def test_hook_labels(self):
        self.assertEqual(post_report.hook_style("Stop skipping this step"), "mistake-fix")
        self.assertEqual(post_report.hook_style("Attempt 1 was a crime"), "fail-to-win")
        self.assertEqual(post_report.hook_style(None), "none")
