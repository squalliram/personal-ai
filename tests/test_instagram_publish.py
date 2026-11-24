import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import notifications
from instagram_agent import cli, env_install, publish, recipe_series, storage
from instagram_agent.graph import GraphAPIError, GraphClient

JPEG = b"\xff\xd8\xff" + b"1" * 32
MADE = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
SLOT = "2026-11-12T18:00+00:00"
DUE = datetime(2026, 11, 12, 18, 5, tzinfo=timezone.utc)
ENV = {
    "INSTAGRAM_AUTO_PUBLISH": "1",
    "INSTAGRAM_PUBLIC_URL": "https://example.test/ig-media",
}


class FakeGraph(GraphClient):
    def __init__(self, *, statuses=("FINISHED",), fail_publish=None):
        super().__init__("IGtoken-secret", "17841408862128347")
        self.posts, self.statuses, self.fail_publish = [], list(statuses), fail_publish

    def post(self, path, params, *, retry=True):
        self.posts.append((path, dict(params), retry))
        if path.endswith("media_publish"):
            if self.fail_publish:
                raise self.fail_publish
            return {"id": "9001"}
        return {"id": f"c{len(self.posts)}"}

    def get(self, path_or_url, params=None):
        return {"status_code": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}


class PublishCase(unittest.TestCase):
    def setUp(self):
        duration = patch.object(publish.video, "read", return_value=({"seconds": 9.0}, []))
        self.duration_mock = duration.start()
        self.addCleanup(duration.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = storage.connect(self.root / "instagram.sqlite3")
        self.addCleanup(self.conn.close)
        self.media, self.public = self.root / "drafts", self.root / "public"
        self.notes = self.root / "notifications.sqlite3"
        self.env = {**ENV, "INSTAGRAM_PUBLIC_DIR": str(self.public), "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.notes)}
        self.checked = []

    def add(self, draft_id="a" * 16, *, score=80, slot=SLOT, made=MADE, slides=3, data=JPEG):
        with self.conn:
            storage.insert_draft(
                self.conn,
                {
                    "draft_id": draft_id,
                    "category": "coffee",
                    "topic": "Flat whites",
                    "kind": "reel",
                    "reel": {"file": "reel.mp4"},
                    "slides": [{"text": f"s{n}", "file": f"slide-{n}.jpg"} for n in range(1, slides + 1)],
                    "caption": "Save this.",
                    "hashtags": ["#coffee", "#london"],
                    "review_score": score,
                    "scheduled_for": slot,
                },
                made.date().isoformat(),
                made.isoformat(timespec="seconds"),
            )
        (self.media / draft_id).mkdir(parents=True)
        if data == JPEG:
            (self.media / draft_id / "reel.mp4").write_bytes(b"video")
        for n in range(1, slides + 1):
            (self.media / draft_id / f"slide-{n}.jpg").write_bytes(data)
        return draft_id

    def check(self, url, size):
        self.assertTrue((self.public / url.split("/ig-media/")[1]).is_file())
        self.checked.append(url)

    def tick(self, graph, now=DUE):
        return publish.tick(self.conn, self.env, self.media, now, graph, check=self.check, sleep=lambda s: None)

    def status(self, draft_id="a" * 16):
        return self.conn.execute("SELECT status, media_id FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()


class PublishTests(PublishCase):
    def test_prepared_reel_is_not_sent_after_midnight_on_a_series_off_day(self):
        friday = (DUE + timedelta(days=1)).replace(hour=23, minute=59, second=30)
        reel = self.add(slot=friday.isoformat())
        recipe_series.start(self.conn, MADE)
        graph = FakeGraph()
        elapsed = 0

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        with patch.object(publish.time, "monotonic", side_effect=lambda: elapsed):
            self.assertEqual(
                publish.tick(self.conn, self.env, self.media, friday, graph, check=self.check, sleep=sleep),
                "published 0, failed 0",
            )
        self.assertFalse(any(path.endswith("/media_publish") for path, _, _ in graph.posts))
        self.assertEqual(self.status(reel)[0], "pending")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM publish_runs").fetchone()[0], 0)
        self.assertEqual(self.tick(FakeGraph(), friday + timedelta(minutes=1)), "published 0, failed 0")
        self.assertEqual(self.tick(FakeGraph(), friday + timedelta(days=3)), "published 1, failed 0")

    def test_publish_run_uses_time_after_reel_preparation(self):
        self.add()
        elapsed = iter((0, publish.REEL_WAIT))
        with patch.object(publish.time, "monotonic", side_effect=lambda: next(elapsed, publish.REEL_WAIT)):
            self.assertEqual(self.tick(FakeGraph()), "published 1, failed 0")
        expected = (DUE + timedelta(seconds=publish.REEL_WAIT)).isoformat(timespec="seconds")
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT started_at FROM publish_runs ORDER BY id")],
            [expected, expected],
        )

    def test_late_series_launch_caps_the_number_of_due_reels(self):
        monday = (DUE + timedelta(days=4)).replace(hour=20)
        self.add("a" * 16, slot=monday.isoformat())
        self.add("b" * 16, slot=monday.isoformat())
        graph = FakeGraph()
        tick = publish.tick
        with (
            patch.dict(cli.os.environ, self.env, clear=True),
            patch.object(cli, "_now", return_value=monday),
            patch.object(cli.token_refresh, "tick", return_value="not_due"),
            patch.object(publish, "configured", return_value=True),
            patch.object(
                publish, "tick", side_effect=lambda *args: tick(*args, graph, check=self.check, sleep=lambda _: None)
            ),
            patch.object(cli.render, "render_due"),
            patch.object(cli, "_capture_followers", return_value="followers_off"),
            patch.object(cli, "_capture_stories", return_value="stories_off"),
            patch.object(cli, "_post_reports", return_value="reports_off"),
            patch("builtins.print"),
        ):
            self.assertEqual(
                cli.main(["--database", str(self.root / "instagram.sqlite3"), "daily", "--media", str(self.media)]), 0
            )
        self.assertEqual([self.status(d)[0] for d in ("a" * 16, "b" * 16)], ["published", "pending"])
        self.assertEqual(self.tick(FakeGraph(), monday + timedelta(days=2)), "published 1, failed 0")

    def test_series_carries_an_off_day_reel_to_the_next_posting_day(self):
        self.add()
        recipe_series.start(self.conn, MADE)
        self.assertEqual(self.tick(FakeGraph()), "published 0, failed 0")
        graph = FakeGraph()
        self.assertEqual(self.tick(graph, DUE + timedelta(days=1)), "published 1, failed 0")
        self.assertEqual(tuple(self.status()), ("published", "9001"))

    def test_series_carries_an_off_day_reel_when_the_series_ends(self):
        first = MADE + timedelta(days=4)
        sunday = first + timedelta(days=27, hours=8)
        self.add(slot=sunday.isoformat(), made=sunday - timedelta(hours=2))
        recipe_series.start(self.conn, first)
        self.assertEqual(self.tick(FakeGraph(), sunday), "published 0, failed 0")
        self.assertEqual(self.tick(FakeGraph(), sunday + timedelta(days=1)), "published 1, failed 0")

    def test_series_recovers_its_final_off_day_reel_after_a_delayed_restart(self):
        first = MADE + timedelta(days=4)
        sunday = first + timedelta(days=27, hours=8)
        self.add(slot=sunday.isoformat(), made=sunday - timedelta(hours=2))
        recipe_series.start(self.conn, first)
        self.assertEqual(self.tick(FakeGraph(), sunday), "published 0, failed 0")
        self.assertEqual(self.tick(FakeGraph(), sunday + timedelta(days=2)), "published 1, failed 0")

    def test_approving_a_carryover_does_not_strand_the_other_reel_in_its_slot(self):
        carryover = self.add("a" * 16)
        recipe_series.start(self.conn, MADE)
        friday = DUE + timedelta(days=1)
        with self.conn:
            self.conn.execute(
                "UPDATE drafts SET reel=? WHERE draft_id=?", ('{"file":"reel.mp4","hold":true}', carryover)
            )
        self.assertEqual(self.tick(FakeGraph(), friday - timedelta(hours=7)), "published 0, failed 0")
        slot = self.conn.execute("SELECT scheduled_for FROM drafts WHERE draft_id=?", (carryover,)).fetchone()[0]
        fresh = self.add("b" * 16, slot=slot, made=friday - timedelta(hours=7))
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='approved' WHERE draft_id=?", (carryover,))
        self.assertEqual(self.tick(FakeGraph(), friday), "published 1, failed 0")
        self.assertEqual(self.tick(FakeGraph(), friday + timedelta(days=3)), "published 1, failed 0")
        self.assertEqual([self.status(d)[0] for d in (carryover, fresh)], ["published", "published"])

    def test_late_approval_does_not_reschedule_an_expired_reel(self):
        friday = DUE + timedelta(days=1)
        slot = (friday - timedelta(minutes=5)).isoformat()
        late = self.add("a" * 16, slot=slot)
        self.add("b" * 16, slot=slot)
        recipe_series.start(self.conn, MADE)
        with self.conn:
            self.conn.execute("UPDATE drafts SET reel=? WHERE draft_id=?", ('{"file":"reel.mp4","hold":true}', late))
        self.assertEqual(self.tick(FakeGraph(), friday), "published 1, failed 0")
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='approved' WHERE draft_id=?", (late,))
        self.assertEqual(self.tick(FakeGraph(), friday + publish.STALE_AFTER), "published 0, failed 0")
        self.assertEqual(self.tick(FakeGraph(), friday + timedelta(days=3)), "published 0, failed 0")
        self.assertEqual(self.status(late)[0], "approved")

    def test_after_midnight_approval_within_grace_moves_to_a_posting_day(self):
        friday = (DUE + timedelta(days=1)).replace(hour=23, minute=30)
        reel = self.add(slot=friday.isoformat())
        recipe_series.start(self.conn, MADE)
        with self.conn:
            self.conn.execute("UPDATE drafts SET reel=? WHERE draft_id=?", ('{"file":"reel.mp4","hold":true}', reel))
        self.assertEqual(self.tick(FakeGraph(), friday), "published 0, failed 0")
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='approved' WHERE draft_id=?", (reel,))
        self.assertEqual(self.tick(FakeGraph(), friday + timedelta(hours=1)), "published 0, failed 0")
        self.assertEqual(self.tick(FakeGraph(), friday + timedelta(days=3)), "published 1, failed 0")

    def test_recipe_series_enforces_posting_days_and_one_reel_story_pair_per_day(self):
        self.add()
        recipe_series.start(self.conn, MADE)
        graph = FakeGraph()
        self.assertEqual(self.tick(graph), "published 0, failed 0")
        self.assertEqual(graph.posts, [])
        self.env["INSTAGRAM_RECIPE_SERIES_DAYS"] = "tue,thu,sat"
        self.add("b" * 16)
        self.assertEqual(self.tick(graph), "published 1, failed 0")
        self.assertEqual(
            [params["media_type"] for path, params, _ in graph.posts if path.endswith("/media")],
            ["REELS", "STORIES"],
        )
        self.assertEqual(tuple(self.status("b" * 16)), ("pending", None))
        self.assertEqual(self.tick(FakeGraph()), "published 0, failed 0")

    def test_publishes_reel_then_same_video_story_once(self):
        self.add()
        graph = FakeGraph(statuses=("IN_PROGRESS", "FINISHED"))
        self.assertEqual(self.tick(graph), "published 1, failed 0")
        containers = [params for path, params, _ in graph.posts if path.endswith("/media")]
        self.assertEqual([c["media_type"] for c in containers], ["REELS", "STORIES"])
        self.assertEqual(set(containers[1]), {"media_type", "video_url"})
        self.assertEqual(len(self.checked), 2)
        self.assertTrue(all(not retry for path, _, retry in graph.posts if path.endswith("media_publish")))
        story = json.loads(self.conn.execute("SELECT reel FROM drafts").fetchone()[0])["story"]
        self.assertEqual(story["state"], "published")
        self.assertEqual(
            [c.args for c in self.duration_mock.call_args_list], [(self.media / ("a" * 16) / "reel.mp4",)] * 2
        )
        self.assertEqual(tuple(self.status()), ("published", "9001"))
        self.assertEqual([p.name for p in self.public.iterdir()], ["index.html"])
        self.assertEqual(self.tick(FakeGraph()), "published 0, failed 0")
        calls = len(graph.posts)
        row = self.conn.execute("SELECT * FROM drafts").fetchone()
        publish.publish_story_once(
            self.conn, graph, row, self.media, self.public, ENV["INSTAGRAM_PUBLIC_URL"], self.env, DUE
        )
        self.assertEqual(len(graph.posts), calls)

    def test_legacy_photo_and_carousel_are_skipped_and_notified(self):
        for slides in (1, 3):
            draft_id = self.add(str(slides) * 16, slides=slides)
            with self.conn:
                self.conn.execute("UPDATE drafts SET kind='carousel',reel=NULL WHERE draft_id=?", (draft_id,))
                if slides == 1:
                    self.conn.execute("UPDATE drafts SET status='approved' WHERE draft_id=?", (draft_id,))
        graph = FakeGraph()
        with patch.object(publish, "notify") as notify:
            self.assertEqual(self.tick(graph), "published 0, failed 0")
            self.assertEqual(notify.call_count, 2)
            self.assertEqual(
                {call.kwargs["key"] for call in notify.call_args_list},
                {f"instagram-legacy-skipped:{str(n) * 16}" for n in (1, 3)},
            )
        self.tick(graph)
        self.tick(graph)
        with sqlite3.connect(self.notes) as notes:
            self.assertEqual(notes.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 2)
        self.assertEqual(graph.posts, [])
        self.assertEqual(publish.due(self.conn, DUE), [])
        with self.assertRaises(publish.PublishError):
            publish.publish_draft(
                graph,
                self.conn.execute("SELECT * FROM drafts").fetchone(),
                self.media,
                self.public,
                ENV["INSTAGRAM_PUBLIC_URL"],
            )

    def test_story_publish_failure_does_not_fail_reel_or_retry(self):
        self.add()
        graph = FakeGraph()
        post = graph.post

        def fail_story(path, params, **kwargs):
            if path.endswith("media_publish") and any(p[1].get("media_type") == "STORIES" for p in graph.posts):
                raise GraphAPIError("uncertain Story result")
            return post(path, params, **kwargs)

        with patch.object(graph, "post", side_effect=fail_story):
            self.assertEqual(self.tick(graph), "published 1, failed 0")
        self.assertEqual(self.status()["status"], "published")
        self.assertEqual(
            json.loads(self.conn.execute("SELECT reel FROM drafts").fetchone()[0])["story"]["state"], "failed"
        )
        self.assertEqual(self.tick(graph), "published 0, failed 0")

    def test_long_reels_publish_but_their_story_is_skipped_and_notified_once(self):
        for number, seconds in enumerate((61.0, 75.0, 90.0), 1):
            with self.subTest(seconds=seconds):
                draft_id = self.add(str(number) * 16)
                graph = FakeGraph()
                with patch.object(publish.video, "read", return_value=({"seconds": seconds}, [])) as duration:
                    self.assertEqual(self.tick(graph), "published 1, failed 0")
                self.assertEqual([c.args for c in duration.call_args_list], [(self.media / draft_id / "reel.mp4",)] * 2)
                self.assertEqual(sum(path.endswith("media_publish") for path, _, _ in graph.posts), 1)
                self.assertEqual(graph.posts[0][1]["media_type"], "REELS")
                self.assertEqual(self.status(draft_id)["status"], "published")
                row = self.conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
                self.assertEqual(json.loads(row["reel"])["story"]["state"], "skipped")
                calls = len(graph.posts)
                publish.publish_story_once(
                    self.conn, graph, row, self.media, self.public, ENV["INSTAGRAM_PUBLIC_URL"], self.env, DUE
                )
                self.assertEqual(self.tick(graph), "published 0, failed 0")
                self.assertEqual(len(graph.posts), calls)
                with sqlite3.connect(self.notes) as notes:
                    self.assertEqual(
                        notes.execute(
                            "SELECT COUNT(*) FROM notifications WHERE dedupe_key=?",
                            (f"instagram-story-skipped:{draft_id}",),
                        ).fetchone()[0],
                        1,
                    )

    def test_sixty_second_clip_remains_compatible(self):
        self.add()
        graph = FakeGraph()
        with patch.object(publish.video, "read", return_value=({"seconds": 60.0}, [])) as duration:
            self.assertEqual(self.tick(graph), "published 1, failed 0")
        self.assertEqual([c.args for c in duration.call_args_list], [(self.media / ("a" * 16) / "reel.mp4",)] * 2)
        self.assertEqual(sum(path.endswith("media_publish") for path, _, _ in graph.posts), 2)

    def test_claimed_story_is_not_repeated_after_crash(self):
        self.add()
        with self.conn:
            self.conn.execute(
                "UPDATE drafts SET status='published',reel=?",
                (json.dumps({"file": "reel.mp4", "story": {"state": "started"}}),),
            )
        graph = FakeGraph()
        row = self.conn.execute("SELECT * FROM drafts").fetchone()
        publish.publish_story_once(
            self.conn, graph, row, self.media, self.public, ENV["INSTAGRAM_PUBLIC_URL"], self.env, DUE
        )
        self.assertEqual(graph.posts, [])

    def test_story_hosting_failure_is_recorded_without_changing_reel(self):
        draft_id = self.add()
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='published' WHERE draft_id=?", (draft_id,))
        (self.media / draft_id / "reel.mp4").unlink()
        graph = FakeGraph()
        row = self.conn.execute("SELECT * FROM drafts").fetchone()
        publish.publish_story_once(
            self.conn, graph, row, self.media, self.public, ENV["INSTAGRAM_PUBLIC_URL"], self.env, DUE
        )
        self.assertEqual(graph.posts, [])
        self.assertEqual(self.status()["status"], "published")
        self.assertEqual(
            json.loads(self.conn.execute("SELECT reel FROM drafts").fetchone()[0])["story"]["state"], "failed"
        )

    def test_rejected_weak_and_future_drafts_are_not_posted(self):
        self.add("a" * 16)
        self.add("b" * 16, score=60)
        self.add("c" * 16, slot="2026-11-12T20:00+00:00")
        with self.conn:
            storage.decide_draft(self.conn, "a" * 16, "rejected", "2026-11-12T17:00:00+00:00")
        graph = FakeGraph()
        self.assertEqual(self.tick(graph), "published 0, failed 0")
        self.assertEqual(graph.posts, [])

    def test_an_approved_weak_draft_is_posted(self):
        self.add(score=40)
        with self.conn:
            storage.decide_draft(self.conn, "a" * 16, "approved", "2026-11-12T12:00:00+00:00")
        self.assertEqual(self.tick(FakeGraph()), "published 1, failed 0")
        linked = self.conn.execute("SELECT media_id FROM examples").fetchone()[0]
        self.assertIsNone(linked)  # Linked once sync stores the post.

    def test_veto_window_is_at_least_an_hour_after_creation(self):
        late = datetime(2026, 11, 12, 17, 50, tzinfo=timezone.utc)
        self.add(made=late)
        self.assertEqual(publish.posts_at(SLOT, late.isoformat()), late + publish.VETO_LEAD)
        self.assertEqual(self.tick(FakeGraph(), now=DUE), "published 0, failed 0")
        self.assertEqual(self.tick(FakeGraph(), now=late + timedelta(hours=1)), "published 1, failed 0")

    def test_missed_slots_are_not_posted_late(self):
        self.add()
        self.assertEqual(self.tick(FakeGraph(), now=DUE + publish.STALE_AFTER), "published 0, failed 0")

    def test_off_without_the_switch_token_or_public_folder(self):
        self.add()
        for env in ({**self.env, "INSTAGRAM_AUTO_PUBLISH": "0"}, {**self.env, "INSTAGRAM_PUBLIC_URL": "http://x"}):
            with self.subTest(env=env):
                self.assertEqual(publish.tick(self.conn, env, self.media, DUE, FakeGraph()), "publish_off")
        self.assertEqual(publish.tick(self.conn, self.env, self.media, DUE), "publish_off")
        state = self.conn.execute("SELECT value FROM publisher_state").fetchone()[0]
        self.assertEqual(state, "off")

    def test_failures_retry_then_stop_and_never_post_png(self):
        self.add(data=b"\x89PNG\r\n\x1a\n")
        for _ in range(publish.MAX_ATTEMPTS):
            self.assertEqual(self.tick(FakeGraph()), "published 0, failed 1")
        self.assertEqual(self.status()["status"], "failed")
        error = self.conn.execute("SELECT error FROM publish_runs ORDER BY id DESC").fetchone()[0]
        self.assertEqual(error, "the reel's video is missing")

    def test_processing_error_is_recorded_and_folder_removed(self):
        self.add()
        self.assertEqual(self.tick(FakeGraph(statuses=("ERROR",))), "published 0, failed 1")
        self.assertEqual(self.status()["status"], "pending")
        self.assertEqual([p.name for p in self.public.iterdir()], ["index.html"])

    def test_failed_publish_call_is_not_retried(self):
        self.add()
        graph = FakeGraph(fail_publish=GraphAPIError("Graph API request failed (HTTP 500)"))
        self.assertEqual(self.tick(graph), "published 0, failed 1")
        self.assertEqual(self.status()["status"], "failed")
        last = self.conn.execute("SELECT outcome, error FROM publish_runs ORDER BY id DESC").fetchone()
        self.assertEqual(last["outcome"], "failed")
        self.assertIn("outcome unknown", last["error"])
        self.assertEqual(self.tick(FakeGraph()), "published 0, failed 0")

    def test_a_reject_during_preparation_stops_the_post(self):
        draft_id = self.add()
        other = storage.connect(self.root / "instagram.sqlite3")
        self.addCleanup(other.close)

        def reject_while_checking(url, size):
            if not self.checked:
                storage.decide_draft(other, draft_id, "rejected", "2026-11-12T18:04:00+00:00")
            self.checked.append(url)

        graph = FakeGraph()
        result = publish.tick(
            self.conn, self.env, self.media, DUE, graph, check=reject_while_checking, sleep=lambda s: None
        )
        self.assertEqual(result, "published 0, failed 0")  # withdrawn, not a failed attempt
        self.assertEqual(self.status()["status"], "rejected")
        self.assertFalse(any(p[0].endswith("media_publish") for p in graph.posts))

    def test_a_reject_on_the_last_attempt_stays_rejected(self):
        draft_id = self.add()
        with self.conn:
            for _ in range(publish.MAX_ATTEMPTS - 1):
                self.conn.execute(
                    "INSERT INTO publish_runs(draft_id, started_at, outcome, error) VALUES(?, ?, 'failed', 'x')",
                    (draft_id, "2026-11-12T18:00:00+00:00"),
                )
        other = storage.connect(self.root / "instagram.sqlite3")
        self.addCleanup(other.close)

        def reject_while_checking(url, size):
            if not self.checked:
                storage.decide_draft(other, draft_id, "rejected", "2026-11-12T18:04:00+00:00")
            self.checked.append(url)

        publish.tick(
            self.conn, self.env, self.media, DUE, FakeGraph(), check=reject_while_checking, sleep=lambda s: None
        )
        self.assertEqual(self.status()["status"], "rejected")

    def test_no_decision_once_publishing_has_started(self):
        draft_id = self.add()
        with self.conn:
            publish._record(self.conn, draft_id, "2026-11-12T18:00:00+00:00", "started")
        self.assertFalse(storage.decide_draft(self.conn, draft_id, "rejected", "2026-11-12T18:01:00+00:00"))

    def test_sync_links_the_published_post_to_its_approved_example(self):
        self.test_an_approved_weak_draft_is_posted()
        with self.conn:
            storage.upsert_post(
                self.conn,
                {"id": "9001", "timestamp": "2026-11-12T18:05:00+0000", "media_type": "CAROUSEL_ALBUM"},
                "2026-11-13T09:00:00Z",
            )
            storage.link_published_drafts(self.conn)
        self.assertEqual(self.conn.execute("SELECT media_id FROM examples").fetchone()[0], "9001")

    def test_png_slides_fail_with_a_clear_reason(self):
        draft_id = self.add()
        (self.media / draft_id / "reel.mp4").unlink()
        for n in range(1, 4):
            (self.media / draft_id / f"slide-{n}.jpg").rename(self.media / draft_id / f"slide-{n}.png")
        self.assertEqual(self.tick(FakeGraph()), "published 0, failed 1")
        error = self.conn.execute("SELECT error FROM publish_runs").fetchone()[0]
        self.assertEqual(error, "the reel's video is missing")

    def test_a_crash_mid_publish_is_never_retried(self):
        draft_id = self.add()
        with self.conn:
            publish._record(self.conn, draft_id, "2026-11-12T18:00:00+00:00", "started")
        graph = FakeGraph()
        self.assertEqual(self.tick(graph), "published 0, failed 0")
        self.assertEqual((self.status()["status"], graph.posts), ("failed", []))


class PermalinkGraph(FakeGraph):
    def __init__(self, permalink=None, **kwargs):
        super().__init__(**kwargs)
        self.permalink = permalink

    def get(self, path_or_url, params=None):
        if params == {"fields": "permalink"}:
            if isinstance(self.permalink, Exception):
                raise self.permalink
            return {"permalink": self.permalink}
        return super().get(path_or_url, params)


class PublishNotificationTests(PublishCase):
    """`tick()` tells the owner about each live post and failed attempt."""

    def notifications(self):
        with sqlite3.connect(self.notes) as conn:
            return conn.execute("SELECT outcome, title, link FROM notifications ORDER BY id").fetchall()

    def test_a_live_post_links_its_permalink(self):
        self.add()
        self.tick(PermalinkGraph("https://www.instagram.com/p/abc123/"))
        self.assertEqual(
            self.notifications(), [("ok", "Instagram post is live: Flat whites", "https://www.instagram.com/p/abc123/")]
        )

    def test_a_missing_permalink_falls_back_to_the_command_centre(self):
        self.add()
        self.env["COMMAND_CENTRE_URL"] = "https://example.test/cc/"
        self.tick(PermalinkGraph(GraphAPIError("Graph API request failed")))
        self.assertEqual(
            self.notifications(), [("ok", "Instagram post is live: Flat whites", "https://example.test/cc/#instagram")]
        )

    def test_a_broken_permalink_read_does_not_stop_the_tick(self):
        from http.client import IncompleteRead

        self.add("a" * 16)
        self.add("b" * 16)
        self.assertEqual(self.tick(PermalinkGraph(IncompleteRead(b""))), "published 2, failed 0")
        self.assertEqual([n[0] for n in self.notifications()], ["ok", "ok"])

    def test_each_failed_attempt_is_reported(self):
        self.add(data=b"not a jpeg")
        for _ in range(3):
            self.tick(FakeGraph())
        notes = self.notifications()
        self.assertEqual(len(notes), 3)
        self.assertTrue(
            all(n[0] == "failed" and n[2] == "https://iramkhan.com/command-centre/#instagram" for n in notes)
        )
        self.assertIn("Instagram post failed: Flat whites (", notes[0][1])
        self.assertTrue(notes[0][1].endswith("attempt 1 of 3)"))
        self.assertTrue(notes[2][1].endswith("attempt 3 of 3)"))

    def test_a_failed_publish_call_says_it_is_not_retried(self):
        self.add()
        self.tick(FakeGraph(fail_publish=GraphAPIError("Graph API request failed (HTTP 500)")))
        self.assertTrue(self.notifications()[0][1].endswith("outcome unknown, check Instagram; not retried)"))

    def test_a_notifier_failure_does_not_stop_publishing(self):
        self.add()
        with patch.object(notifications, "connect", side_effect=sqlite3.OperationalError("locked")):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(self.tick(FakeGraph()), "published 1, failed 0")
        self.assertEqual(self.status()["status"], "published")

    def test_publishing_off_sends_nothing(self):
        self.add()
        self.env.pop("INSTAGRAM_AUTO_PUBLISH")
        self.tick(FakeGraph())
        self.assertFalse(self.notes.exists())


class GraphPostTests(unittest.TestCase):
    def test_post_sends_the_token_in_the_body_and_hides_meta_messages(self):
        client = GraphClient("IGsecret-token", "123", host="https://graph.instagram.com/", retries=2)
        body = json.dumps({"error": {"message": "IGsecret-token bad", "code": 190}}).encode()
        error = HTTPError("https://graph.instagram.com/x", 500, "x", {}, io.BytesIO(body))
        with (
            patch.object(client._opener, "open", side_effect=error) as opened,
            self.assertRaises(GraphAPIError) as caught,
        ):
            client.post("123/media_publish", {"creation_id": "1"}, retry=False)
        self.assertEqual(opened.call_count, 1)
        request = opened.call_args[0][0]
        self.assertNotIn("token", request.full_url)
        self.assertIn(b"access_token=IGsecret-token", request.data)
        self.assertEqual(str(caught.exception), "Graph API request failed (HTTP 500, code 190)")

    def test_post_cannot_leave_the_graph_origin(self):
        client = GraphClient("tok", "123")
        with self.assertRaises(GraphAPIError):
            client.post("https://evil.test/x", {})

    def test_instagram_login_tokens_use_graph_instagram_com(self):
        env = {"INSTAGRAM_ACCESS_TOKEN": "IGAAx", "INSTAGRAM_USER_ID": "1"}
        self.assertTrue(cli.graph_client(env).base_url.startswith("https://graph.instagram.com/"))
        env["INSTAGRAM_ACCESS_TOKEN"] = "EAAx"
        self.assertTrue(cli.graph_client(env).base_url.startswith("https://graph.facebook.com/"))


class PublishEnvTests(unittest.TestCase):
    def test_publishing_settings_are_accepted_and_urls_must_be_https(self):
        values = env_install.parse(
            "INSTAGRAM_ACCESS_TOKEN=IGAA_x-1\nINSTAGRAM_USER_ID=17841408862128347\nINSTAGRAM_AUTO_PUBLISH=1\n"
            "INSTAGRAM_PUBLIC_DIR=/home/u/public_html/ig-media\nINSTAGRAM_PUBLIC_URL=https://iramkhan.com/ig-media\n"
        )
        self.assertEqual(values["INSTAGRAM_PUBLIC_URL"], "https://iramkhan.com/ig-media")
        for text in ("INSTAGRAM_PUBLIC_URL=http://x.test/a:b\n", "INSTAGRAM_PUBLIC_URL=https://x.test/?a=$(id)\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                env_install.parse(text)


if __name__ == "__main__":
    unittest.main()
