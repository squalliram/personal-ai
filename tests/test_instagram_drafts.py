import base64
import io
import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from instagram_agent import cli, cron_install, drafts, image_review, storage
from instagram_agent.cloudflare_client import CloudflareAI, CloudflareError
from instagram_agent.openai_client import OpenAIClient, OpenAIError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MORNING = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)  # 10:00 London (GMT)


def raw_draft(topic, category="coffee", slides=4):
    return {
        "category": category,
        "topic": topic,
        "angle": "why it works",
        "slides": [{"text": f"{topic} {n}", "image_prompt": f"photo of {topic} {n}"} for n in range(slides)],
        "caption": f"{topic}. Save this for your next coffee run.",
    }


class FakeAI(OpenAIClient):
    def __init__(self, creates, scores, *, fail_images_after=None):
        super().__init__("sk-test-secret")
        self.creates, self.scores, self.calls, self.images = list(creates), list(scores), [], 0
        self.fail_images_after = fail_images_after

    def chat_json(self, system, user, *, images=()):
        self.calls.append((system, json.loads(user)))
        if system.startswith("You are the Create agent"):
            return {"drafts": self.creates.pop(0)}
        if system.startswith("You are the Tags agent"):
            count = len(json.loads(user)["drafts"])
            return {"hashtags": [["#Coffee", "#coffee", "#latteart", "not a tag", "#two words"] for _ in range(count)]}
        return {"reviews": [{"score": s, "notes": f"note {s}"} for s in self.scores.pop(0)]}

    def image(self, prompt):
        self.images += 1
        if self.fail_images_after is not None and self.images > self.fail_images_after:
            raise OpenAIError("OpenAI request failed (HTTP 500)")
        return PNG


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = storage.connect(self.root / "instagram.sqlite3")
        vision = patch.object(image_review, "check", return_value={"status": "passed", "reasons": []})
        vision.start()
        self.addCleanup(vision.stop)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_weak_drafts_go_back_to_create_with_review_notes(self):
        ai = FakeAI(
            creates=[
                [raw_draft("flat white"), raw_draft("sourdough", "food"), raw_draft("shoes", "fashion")],
                [raw_draft("ramen spots", "restaurant"), raw_draft("cortado")],
            ],
            scores=[[80, 50], [90, 75]],
        )
        found = drafts.generate(self.conn, ai, MORNING, count=3)
        self.assertEqual([d["topic"] for d in found], ["flat white", "ramen spots", "cortado"])
        second_create = [payload for system, payload in ai.calls if system.startswith("You are the Create")][1]
        self.assertEqual(second_create["feedback_on_weak_drafts"], ["sourdough: note 50"])
        self.assertIn("sourdough", second_create["recent_topics"])
        self.assertEqual(second_create["today"], "Thursday 12 November 2026 in London (autumn)")
        self.assertEqual(found[0]["hashtags"], ["#coffee", "#latteart"])
        self.assertIn(drafts.UNTRUSTED, ai.calls[0][0])

    def test_a_recent_topic_is_sent_back_to_create_and_never_returned(self):
        with self.conn:
            storage.insert_draft(
                self.conn,
                {**drafts.normalise(raw_draft("Cozy autumn soup", "food")), "draft_id": "0" * 16, "hashtags": []},
                "2026-11-11",
                "2026-11-11T10:00:00+00:00",
            )
        ai = FakeAI(
            creates=[
                [raw_draft("Cosy Autumn Soups", "food"), raw_draft("Pear crumble", "food"), raw_draft("Pear crumbles")],
                [raw_draft("Leek and potato soup", "food"), raw_draft("Mushroom toast", "food")],
            ],
            scores=[[90], [85, 80]],
        )
        found = drafts.generate(self.conn, ai, MORNING, count=3)
        self.assertEqual([d["topic"] for d in found], ["Pear crumble", "Leek and potato soup", "Mushroom toast"])
        second_create = [payload for system, payload in ai.calls if system.startswith("You are the Create")][1]
        self.assertEqual(
            second_create["feedback_on_weak_drafts"],
            [
                "Cosy Autumn Soups: repeats the recent topic 'Cozy autumn soup'; choose a different dish or drink",
                "Pear crumbles: repeats the recent topic 'Pear crumble'; choose a different dish or drink",
            ],
        )
        reviewed = [payload for system, payload in ai.calls if system.startswith("You are the Review")][0]
        self.assertEqual([d["topic"] for d in reviewed["drafts"]], ["Pear crumble"])

    def test_only_repeats_leave_the_day_short_rather_than_reuse_a_topic(self):
        ai = FakeAI(
            creates=[[raw_draft("flat white")], [raw_draft("Flat whites")], [raw_draft("flat white")]], scores=[[40]]
        )
        self.assertEqual([d["topic"] for d in drafts.generate(self.conn, ai, MORNING, count=2)], ["flat white"])

    def test_repeated_topic_ignores_pillar_names_but_not_the_dish(self):
        cases = [
            ("Cozy autumn soup", "Cosy autumn soup", True),
            ("Spiced butternut squash soup", "Butternut squash soup", True),
            ("Fridge raid soup", "Fridge raid soup", True),
            ("Fridge raid soup with leeks", "Fridge raid soup with carrots", False),
            ("One pot, 5 days: chilli", "One pot, 5 days: chickpea curry", False),
            ("Cosy leek soup", "Cosy pumpkin soup", False),
            ("Leek and potato soup", "Roasted tomato soup", False),
            ("Weeknight mushroom risotto", "Creamy mushroom risotto", True),
            ("Creme brulee", "Crème brûlée", True),
            ("Crispy roast potato", "Crispy roast potatoes", True),
            ("Pumpkin tart", "Pumpkin soup", False),
            ("Cosy autumn pumpkin soup", "Cozy autumn soup", False),
            ("Tomato–basil soup", "Tomato basil soup", True),
            ("Winter squash soup", "Summer squash soup", False),
        ]
        for topic, earlier, repeats in cases:
            with self.subTest(topic=topic, earlier=earlier):
                self.assertEqual(drafts.repeated_topic(topic, [earlier]), earlier if repeats else None)

    def test_best_weak_drafts_still_fill_the_day_after_three_rounds(self):
        ai = FakeAI(creates=[[raw_draft("a")], [raw_draft("b")], [raw_draft("c")]], scores=[[40], [60], [10]])
        found = drafts.generate(self.conn, ai, MORNING, count=2)
        self.assertEqual([(d["topic"], d["review_score"]) for d in found], [("b", 60), ("a", 40)])

    def test_invalid_drafts_and_hashtags_are_dropped(self):
        self.assertIsNone(drafts.normalise(raw_draft("x", slides=2)))
        self.assertIsNone(drafts.normalise({**raw_draft("x"), "caption": "Nice #tag"}))
        tags = drafts.clean_hashtags([f"#tag{n}" for n in range(30)] + ["#Tag1"], "caption")
        self.assertEqual(len(tags), 20)
        self.assertEqual(drafts.clean_hashtags(["#a" * 1, "#b"], "x" * 2197), [])

    def test_ai_slide_count_is_three_or_four_and_meets_minimum_duration(self):
        for count in (0, 2, 3, 4, 5, 8):
            with self.subTest(count=count):
                draft = drafts.normalise(raw_draft("coffee", slides=count))
                if count in (3, 4):
                    self.assertEqual(len(draft["slides"]), count)
                    self.assertEqual(drafts.render.photo_duration(count), count * 3)
                    self.assertGreaterEqual(drafts.render.photo_duration(count), 7)
                else:
                    self.assertIsNone(draft)

    def test_publish_slots_start_at_noon_and_use_audience_hours(self):
        storage.replace_audience_hours(self.conn, {19: 500, 8: 400, 13: 300}, "now")
        self.assertEqual(
            drafts.publish_slots(self.conn, MORNING, 3),
            ["2026-11-12T19:00+00:00", "2026-11-13T08:00+00:00", "2026-11-12T13:00+00:00"],
        )
        self.conn.execute("DELETE FROM audience_hours")
        storage.replace_audience_hours(self.conn, {19: 500}, "now")
        self.assertEqual(
            drafts.publish_slots(self.conn, MORNING, 3),
            ["2026-11-12T19:00+00:00", "2026-11-13T19:00+00:00", "2026-11-14T19:00+00:00"],
        )
        storage.replace_audience_hours(self.conn, {19: 500, 8: 400, 13: 300}, "now")
        evening = datetime(2026, 11, 12, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(drafts.publish_slots(self.conn, evening, 1), ["2026-11-13T19:00+00:00"])
        self.conn.execute("DELETE FROM audience_hours")
        self.assertEqual(
            drafts.publish_slots(self.conn, MORNING, 3),
            ["2026-11-12T18:00+00:00", "2026-11-12T12:00+00:00", "2026-11-12T20:00+00:00"],
        )

    def test_images_are_private_and_a_failed_draft_is_dropped(self):
        media = self.root / "drafts"
        items = [drafts.normalise(raw_draft("one")), drafts.normalise(raw_draft("two"))]
        stored = drafts.render_and_store(self.conn, FakeAI([], [], fail_images_after=5), items, media, MORNING)
        self.assertEqual(stored, 1)
        (folder,) = [p for p in media.iterdir() if p.is_dir()]
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [f"slide-{n}.png" for n in range(1, 5)])
        self.assertEqual(stat.S_IMODE((folder / "slide-1.png").stat().st_mode), 0o600)
        row = self.conn.execute("SELECT * FROM drafts").fetchone()
        self.assertEqual(row["kind"], "reel")
        self.assertEqual(json.loads(row["reel"])["render"], "pending")
        self.assertEqual(json.loads(row["reel"])["framing"], "cover")
        self.assertNotIn("music", json.loads(row["reel"]))
        self.assertEqual((row["draft_id"], row["status"], row["created_on"]), (folder.name, "pending", "2026-11-12"))
        self.assertEqual(json.loads(row["slides"])[0], {"text": "one 0", "file": "slide-1.png"})

    def test_ai_slide_reel_uses_agents_matched_licensed_music(self):
        track = {"track_id": "a" * 16, "title": "Cafe", "licence": "CC0", "performance": {}}
        ai = FakeAI([], [])
        with (
            patch.object(drafts.music, "for_reel", return_value=[track]),
            patch.object(ai, "chat_json", return_value={"music_track": track["track_id"]}) as pick,
        ):
            self.assertEqual(
                drafts.render_and_store(
                    self.conn, ai, [drafts.normalise(raw_draft("coffee"))], self.root / "drafts", MORNING
                ),
                1,
            )
        reel = json.loads(self.conn.execute("SELECT reel FROM drafts").fetchone()[0])
        self.assertEqual((reel["source"], reel["photos"], reel["music"]["mix"]), ("photos", 4, "replace"))
        self.assertEqual(reel["music"]["track_id"], track["track_id"])
        self.assertIn("music_tracks", json.loads(pick.call_args.args[1]))

    def test_textless_image_provider_gets_one_timed_beat_per_slide(self):
        ai = FakeAI([], [])
        ai.draws_text = False
        item = drafts.normalise(raw_draft("coffee"))
        drafts.render_and_store(self.conn, ai, [item], self.root / "drafts", MORNING)
        reel = json.loads(self.conn.execute("SELECT reel FROM drafts").fetchone()[0])
        self.assertEqual([beat["text"] for beat in reel["on_screen"]], [s["text"] for s in item["slides"]])
        self.assertEqual([(b["start"], b["end"]) for b in reel["on_screen"]], [(0, 2), (3, 6), (6, 9), (9, 12)])
        self.assertEqual(reel["on_screen"][-1]["end"], reel["seconds"])

    def test_music_selection_failure_does_not_leave_a_partial_draft(self):
        ai = FakeAI([], [])
        media = self.root / "drafts"
        with (
            patch.object(drafts.music, "for_reel", return_value=[{"track_id": "a" * 16}]),
            patch.object(ai, "chat_json", side_effect=OpenAIError("unavailable")),
        ):
            with self.assertRaises(OpenAIError):
                drafts.render_and_store(self.conn, ai, [drafts.normalise(raw_draft("coffee"))], media, MORNING)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)
        self.assertEqual(list(media.iterdir()), [])

    def test_decisions_remember_withdrawn_approval(self):
        draft = {**drafts.normalise(raw_draft("flat white")), "draft_id": "a" * 16, "hashtags": ["#coffee"]}
        with self.conn:
            storage.insert_draft(self.conn, draft, "2026-11-12", "2026-11-12T10:00:00+00:00")
        self.assertTrue(storage.decide_draft(self.conn, "a" * 16, "approved", "t1"))
        self.assertTrue(storage.decide_draft(self.conn, "a" * 16, "rejected", "t2"))
        self.assertFalse(storage.decide_draft(self.conn, "a" * 16, "approved", "t3"))
        example = self.conn.execute("SELECT decision, topic, hashtags FROM examples").fetchone()
        self.assertEqual(tuple(example), ("rejected", "flat white", "#coffee"))


class CliStartupTests(unittest.TestCase):
    def test_daily_cli_starts_in_a_fresh_process(self):
        result = subprocess.run(
            [sys.executable, "-m", "instagram_agent", "daily", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--media", result.stdout)


class DailyTickTests(unittest.TestCase):
    def setUp(self):
        self.rendering = patch.object(cli.render, "can_render", return_value=True)
        self.rendering.start()
        self.addCleanup(self.rendering.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = storage.connect(self.root / "instagram.sqlite3")
        self.notes = self.root / "notifications.sqlite3"
        self.env = {
            "OPENAI_API_KEY": "sk-test-secret",
            "INSTAGRAM_VOICE_FILE": str(self.root / "voice.md"),
            "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.notes),
        }

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def tick(self, now, ai=None):
        ai = ai or FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]])
        with (
            patch.object(cli, "model_client", return_value=ai),
            patch.object(
                image_review, "client", return_value=Mock(chat_json=Mock(return_value={"pass": True, "reasons": []}))
            ),
        ):
            return cli.daily(self.conn, self.env, self.root / "drafts", now=now)

    def test_runs_once_a_day_from_nine(self):
        self.assertEqual(self.tick(datetime(2026, 11, 12, 8, 45, tzinfo=timezone.utc)), "skipped")
        self.assertEqual(self.tick(MORNING), "ok")
        self.assertEqual(storage.drafts_on(self.conn, "2026-11-12"), 1)
        self.assertEqual(self.tick(MORNING), "skipped")

    def test_without_photo_rendering_no_ai_draft_is_generated_or_stored(self):
        ai = FakeAI([], [])
        with patch.object(cli.render, "can_render", return_value=False):
            self.assertEqual(self.tick(MORNING, ai), "failed")
        self.assertEqual(ai.calls, [])
        self.assertEqual(storage.drafts_on(self.conn, "2026-11-12"), 0)
        self.assertIn(
            "photo reel rendering unavailable", self.conn.execute("SELECT error FROM daily_runs").fetchone()[0]
        )

    def test_back_to_back_days_get_different_topics(self):
        first = FakeAI([[raw_draft("Cozy autumn soup", "food")]], [[90]])
        self.assertEqual(self.tick(datetime(2026, 11, 19, 10, tzinfo=timezone.utc), first), "ok")
        second = FakeAI(
            [[raw_draft("Cosy autumn soup", "food")], [raw_draft("Parsnip and apple soup", "food")]], [[95]]
        )
        self.assertEqual(self.tick(datetime(2026, 11, 20, 10, tzinfo=timezone.utc), second), "ok")
        topics = [row[0] for row in self.conn.execute("SELECT topic FROM drafts ORDER BY created_at")]
        self.assertEqual(topics, ["Cozy autumn soup", "Parsnip and apple soup"])
        creates = [payload for system, payload in second.calls if system.startswith("You are the Create")]
        self.assertEqual(creates[0]["recent_topics"], ["Cozy autumn soup"])
        self.assertIn("repeats the recent topic 'Cozy autumn soup'", creates[1]["feedback_on_weak_drafts"][0])

    def test_one_draft_a_day_at_the_best_audience_hour(self):
        storage.replace_audience_hours(self.conn, {19: 500, 8: 400, 13: 300}, "now")
        ai = FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]])
        self.assertEqual(self.tick(MORNING, ai), "ok")
        create = next(system for system, _ in ai.calls if system.startswith("You are the Create"))
        self.assertIn("Write 1 slide reel", create)
        [row] = self.conn.execute("SELECT topic, scheduled_for FROM drafts").fetchall()
        self.assertEqual(tuple(row), ("a", "2026-11-12T19:00+00:00"))

    def test_a_failed_run_is_retried_before_noon(self):
        self.assertEqual(self.tick(MORNING, FakeAI([[], [], []], [])), "failed")
        self.assertEqual(self.tick(MORNING), "ok")
        self.assertEqual(storage.drafts_on(self.conn, "2026-11-12"), 1)

    def test_image_review_rejection_is_replaced_in_the_same_slot(self):
        with patch.object(image_review, "check", return_value={"status": "failed", "reasons": ["hand"]}):
            self.assertEqual(self.tick(MORNING), "failed")
        slot = self.conn.execute("SELECT scheduled_for FROM drafts").fetchone()[0]
        self.assertEqual(storage.drafts_on(self.conn, "2026-11-12"), 0)
        self.assertEqual(storage.review_rejections_on(self.conn, "2026-11-12"), 1)
        self.assertEqual(self.tick(MORNING), "ok")
        rows = self.conn.execute("SELECT status,scheduled_for FROM drafts ORDER BY rowid").fetchall()
        self.assertEqual([row[0] for row in rows], ["rejected", "pending"])
        self.assertEqual(rows[1][1], slot)

    def test_two_review_rejections_stop_further_generation_that_day(self):
        with patch.object(
            image_review, "check", return_value={"status": "unavailable", "reasons": ["image review unavailable"]}
        ):
            for _ in range(2):
                self.assertEqual(self.tick(MORNING), "failed")
        with patch.object(cli, "model_client") as model:
            self.assertEqual(cli.make_drafts(self.conn, self.env, self.root / "drafts", MORNING), "gave_up")
        model.assert_not_called()
        self.assertEqual(storage.review_rejections_on(self.conn, "2026-11-12"), 2)

    def test_british_summer_time_is_respected(self):
        self.assertEqual(self.tick(datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc)), "ok")  # 09:30 BST

    def test_failures_stop_after_three_attempts(self):
        for _ in range(3):
            self.assertEqual(self.tick(MORNING, FakeAI([[], [], []], [])), "failed")
        self.assertEqual(self.tick(MORNING), "gave_up")

    def test_missing_key_is_recorded_without_calling_openai(self):
        self.env.pop("OPENAI_API_KEY")
        with patch.object(cli, "OpenAIClient") as openai, patch.object(cli, "CloudflareAI") as cloudflare:
            self.assertEqual(cli.daily(self.conn, self.env, self.root / "drafts", now=MORNING), "not_configured")
        openai.assert_not_called()
        cloudflare.assert_not_called()

    def test_cloudflare_is_used_when_its_token_is_set(self):
        env = {"CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_API_TOKEN": "cf-secret", "OPENAI_API_KEY": "sk-x"}
        self.assertIsInstance(cli.model_client(env), CloudflareAI)
        self.assertIsInstance(cli.model_client({**env, "INSTAGRAM_AI_PROVIDER": "openai"}), OpenAIClient)
        self.assertIsNone(cli.model_client({"CLOUDFLARE_API_TOKEN": "cf-secret"}))
        partial = {"CLOUDFLARE_API_TOKEN": "cf-secret", "OPENAI_API_KEY": "sk-x"}
        self.assertIsInstance(cli.model_client(partial), OpenAIClient)

    def test_not_configured_runs_do_not_use_up_attempts(self):
        with self.conn:
            for _ in range(3):
                storage.record_run(self.conn, "2026-11-12", MORNING.isoformat(), "not_configured")
        self.assertEqual(storage.runs_on(self.conn, "2026-11-12"), 0)

    @patch.object(cli, "DAILY_DRAFTS", 3)
    def test_a_dropped_draft_is_recorded_and_topped_up_before_noon(self):
        first = FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]], fail_images_after=9)
        self.assertEqual(self.tick(MORNING, first), "partial")
        row = self.conn.execute("SELECT outcome, drafts, error FROM daily_runs").fetchone()
        self.assertEqual(
            tuple(row),
            ("partial", 2, "1 of 3 drafts not made: slide image failed (OpenAI request failed (HTTP 500))"),
        )
        topped_up = FakeAI([[raw_draft("d")]], [[85]])
        self.assertEqual(self.tick(MORNING, topped_up), "ok")
        self.assertIn("a", topped_up.calls[0][1]["recent_topics"])
        rows = self.conn.execute("SELECT topic, scheduled_for FROM drafts ORDER BY created_at, topic").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["scheduled_for"] for r in rows}), 3)
        self.assertEqual(self.tick(MORNING), "skipped")

    @patch.object(cli, "DAILY_DRAFTS", 3)
    def test_a_short_day_is_left_alone_after_noon(self):
        self.tick(MORNING, FakeAI([[raw_draft("a"), raw_draft("b")], [], []], [[90, 80]]))
        self.assertEqual(storage.drafts_on(self.conn, "2026-11-12"), 2)
        self.assertEqual(self.tick(datetime(2026, 11, 12, 12, 15, tzinfo=timezone.utc)), "skipped")

    def test_a_failed_slide_image_gets_one_more_try(self):
        ai = FakeAI([], [])
        calls = iter([OpenAIError("OpenAI request failed (HTTP 500)")])

        def flaky(prompt):
            error = next(calls, None)
            if error:
                raise error
            return PNG

        ai.image = flaky
        dropped = []
        stored = drafts.render_and_store(
            self.conn,
            ai,
            [drafts.normalise(raw_draft("one"))],
            self.root / "drafts",
            MORNING,
            dropped,
            reviewer=Mock(chat_json=Mock(return_value={"pass": True, "reasons": []})),
        )
        self.assertEqual((stored, dropped), (1, []))

    def test_image_write_errors_are_recorded_as_failed_attempts(self):
        with patch.object(drafts, "render_and_store", side_effect=PermissionError("/secret/path")):
            self.assertEqual(self.tick(MORNING), "failed")
        row = self.conn.execute("SELECT outcome, error FROM daily_runs").fetchone()
        self.assertEqual(tuple(row), ("failed", "PermissionError"))

    def test_failed_insert_removes_written_images(self):
        ai = FakeAI([[raw_draft("a")]], [[90]])
        found = drafts.generate(self.conn, ai, MORNING, count=1)
        with patch.object(storage, "insert_draft", side_effect=RuntimeError("db")), self.assertRaises(RuntimeError):
            drafts.render_and_store(self.conn, ai, found, self.root / "drafts", MORNING)
        self.assertEqual(list((self.root / "drafts").iterdir()), [])

    def test_non_finite_review_scores_count_as_zero(self):
        found = drafts.generate(self.conn, FakeAI([[raw_draft(t) for t in "abc"]], [[90, 90, 90]]), MORNING)
        ctx = drafts.context(self.conn, MORNING)
        drafts.review(FakeAI([], [[float("nan"), float("inf"), True]]), ctx, found, rules="")
        self.assertEqual([d["review_score"] for d in found], [0, 0, 0])
        drafts.review(FakeAI([], [[10**400, -(10**400), 75]]), ctx, found, rules="")
        self.assertEqual([d["review_score"] for d in found], [100, 0, 75])

    def test_voice_profile_reaches_the_create_agent(self):
        (self.root / "voice.md").write_text("Warm, short sentences, Manchester slang.", encoding="utf-8")
        ai = FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]])
        self.tick(MORNING, ai)
        self.assertIn("Manchester slang", ai.calls[0][0])

    def test_create_and_review_forbid_invented_codes_and_prices(self):
        ai = FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]])
        self.tick(MORNING, ai)
        prompts = {system.split(" agent")[0]: system for system, _ in ai.calls}
        self.assertIn("promo codes", prompts["You are the Create"])
        self.assertIn("promo code", prompts["You are the Review"])
        self.assertIn("captions and slides are not data", prompts["You are the Review"])
        review_payload = next(payload for system, payload in ai.calls if system.startswith("You are the Review"))
        self.assertIn("approved_examples", review_payload)


class SlidePromptTests(unittest.TestCase):
    def test_negated_people_preserve_the_scene_but_affirmative_mentions_do_not(self):
        for scene in (
            "café interior with no people in frame",
            "café interior without people",
            "café interior without any humans visible",
            "people-free café interior",
            "person-free café interior",
        ):
            with self.subTest(scene=scene):
                prompt = drafts.slide_prompt({"text": "Coffee", "image_prompt": scene}, draws_text=False)
                self.assertTrue(prompt.startswith(scene + "\n"))
        for scene in (
            "a barista pouring latte",
            "a barista free-pouring milk",
            "no hands except the one pouring espresso",
            "no people except a chef",
            "no people but a chef",
            "no people apart from a chef",
            "café interior free of any people",
        ):
            with self.subTest(scene=scene), self.assertLogs("instagram_agent.drafts", level="WARNING"):
                prompt = drafts.slide_prompt({"text": "Coffee", "image_prompt": scene}, draws_text=False)
                self.assertNotIn(scene, prompt)
                self.assertIn("photorealistic close-up", prompt)

    def test_no_people_and_insight_instructions(self):
        self.assertIn("Each has 3-4 slides", drafts.CREATE)
        self.assertIn("slide count within 3-4", drafts.CREATE)
        self.assertIn("finished, plated dish", drafts.CREATE)
        self.assertIn("first second", drafts.CREATE)
        self.assertIn("outside 3-4 slides", drafts.REVIEW)
        self.assertIn("finished, plated dish", drafts.REVIEW)
        self.assertIn("For AI-generated slides only, score under 50 if the draft indicates people", drafts.REVIEW)
        self.assertIn("people and hands are allowed", drafts.REVIEW)
        self.assertIn(drafts.NO_PEOPLE, drafts.CREATE)
        self.assertIn("watch-time lift, reach, follows", drafts.CREATE)
        self.assertIn("reel duration findings", drafts.CREATE_REEL_FROM_PHOTOS)
        self.assertIn("upbeat, energetic, catchy", drafts.PICK_MUSIC)
        for draws_text in (True, False):
            with self.assertLogs("instagram_agent.drafts", level="WARNING") as logs:
                prompt = drafts.slide_prompt(
                    {"text": "Coffee", "image_prompt": "A barista hand pouring coffee"}, draws_text=draws_text
                )
            self.assertTrue(prompt.endswith(drafts.NO_PEOPLE))
            self.assertNotIn("A barista hand pouring coffee", prompt)
            self.assertIn("photorealistic close-up food or drink photograph illustrating: Coffee", prompt)
            self.assertNotIn("pouring", logs.output[0])
        self.assertIsNone(drafts.PEOPLE.search("handcrafted mango coffee"))

    def test_negated_motion_clauses_preserve_the_dish(self):
        for scene, expected in (
            ("A cherry crumble on a brown plate, no pouring", "A cherry crumble on a brown plate"),
            ("without drizzling, a bowl of tomato soup", "a bowl of tomato soup"),
            ("A baked potato; no stirring or dipping", "A baked potato"),
        ):
            prompt = drafts.slide_prompt({"text": "Weeknight supper", "image_prompt": scene}, draws_text=False)
            self.assertEqual(prompt.split("\n\n")[0], expected)
        prompt = drafts.slide_prompt(
            {"text": "Supper", "image_prompt": "cream pouring over a cherry crumble"}, draws_text=False
        )
        self.assertTrue(prompt.startswith("A finished, plated dish"))

    def test_all_models_generate_textless_static_brand_photos(self):
        slide = {"text": "Flat white 101", "image_prompt": "a flat white on marble"}
        for draws_text in (True, False):
            clean = drafts.slide_prompt(slide, draws_text=draws_text)
            self.assertNotIn("Flat white 101", clean)
            for phrase in (
                "No text",
                "35mm",
                "home kitchen",
                "warm amber and brown",
                "natural window light",
                "rustic wooden",
                "crumbs, drips and linen",
                "Static still-life",
                "No pouring, stirring, dipping or drizzling",
            ):
                self.assertIn(phrase, clean)
        self.assertIn("cosy seasonal home cooking", drafts.CREATE)
        self.assertIn("easy enough for weeknights", drafts.CREATE)
        self.assertIn("UK ingredients", drafts.CREATE)
        self.assertIn("£", drafts.CREATE)
        self.assertNotIn(
            "liquid pouring into a cup",
            drafts.slide_prompt({**slide, "image_prompt": "liquid pouring into a cup"}, draws_text=True),
        )
        self.assertFalse(CloudflareAI.draws_text)


class OpenAIClientTests(unittest.TestCase):
    def error(self, status, body):
        return HTTPError("https://api.openai.com/v1/chat/completions", status, "x", {}, io.BytesIO(body))

    def test_no_credit_fails_fast_with_the_code_but_not_the_message(self):
        body = json.dumps({"error": {"code": "insufficient_quota", "message": "You have no credits sk-test"}}).encode()
        client = OpenAIClient("sk-test-secret", retries=2)
        with (
            patch("instagram_agent.openai_client.urlopen", side_effect=self.error(429, body)) as call,
            patch("instagram_agent.openai_client.time.sleep") as sleep,
            self.assertRaises(OpenAIError) as caught,
        ):
            client.chat_json("s", "u")
        self.assertEqual(str(caught.exception), "OpenAI request failed (HTTP 429, insufficient_quota)")
        self.assertEqual((call.call_count, sleep.call_count), (1, 0))

    def test_rate_limits_are_retried_and_odd_codes_dropped(self):
        body = json.dumps({"error": {"code": "Rate limit <script>"}}).encode()
        client = OpenAIClient("sk-test-secret", retries=1)
        with (
            patch("instagram_agent.openai_client.urlopen", side_effect=self.error(429, body)) as call,
            patch("instagram_agent.openai_client.time.sleep"),
            self.assertRaises(OpenAIError) as caught,
        ):
            client.chat_json("s", "u")
        self.assertEqual(str(caught.exception), "OpenAI request failed (HTTP 429)")
        self.assertEqual(call.call_count, 2)

    def test_images_request_and_require_jpeg(self):
        client = OpenAIClient("sk-test-secret")
        jpeg = b"\xff\xd8\xff" + b"0" * 16
        reply = {"data": [{"b64_json": base64.b64encode(jpeg).decode()}]}
        with patch.object(client, "_post", return_value=reply) as post:
            self.assertEqual(client.image("a flat white"), jpeg)
        payload = post.call_args.args[1]
        self.assertEqual(payload["output_format"], "jpeg")
        self.assertEqual((payload["size"], payload["quality"]), ("1024x1280", "medium"))

    def test_images_reject_png_instead_of_passing_it_to_publisher(self):
        client = OpenAIClient("sk-test-secret")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
        reply = {"data": [{"b64_json": base64.b64encode(png).decode()}]}
        with patch.object(client, "_post", return_value=reply), self.assertRaises(OpenAIError) as caught:
            client.image("a flat white")
        self.assertEqual(str(caught.exception), "OpenAI returned an image that is not a JPEG")


class CloudflareClientTests(unittest.TestCase):
    def reply(self, body):
        response = io.BytesIO(json.dumps(body).encode())
        response.__enter__ = lambda *a: response
        response.__exit__ = lambda *a: None
        return response

    def client(self):
        return CloudflareAI("0123456789abcdef0123456789abcdef", "cf-secret", retries=0)

    def test_chat_reads_object_or_text_replies(self):
        for result in ({"response": {"drafts": []}}, {"response": 'Sure! ```json\n{"drafts": []}\n```'}):
            with patch(
                "instagram_agent.cloudflare_client.urlopen",
                return_value=self.reply({"success": True, "result": result}),
            ) as call:
                self.assertEqual(self.client().chat_json("s", "u"), {"drafts": []})
            request = call.call_args.args[0]
            self.assertTrue(request.full_url.endswith("/ai/run/@cf/meta/llama-4-scout-17b-16e-instruct"))
            self.assertEqual(json.loads(request.data)["max_tokens"], 4096)
        with patch(
            "instagram_agent.cloudflare_client.urlopen", return_value=self.reply({"result": {"response": "no"}})
        ):
            with self.assertRaises(CloudflareError):
                self.client().chat_json("s", "u")

    def test_flux_2_images_use_multipart_and_keep_their_format(self):
        jpeg = b"\xff\xd8\xff" + b"0" * 16
        body = {"success": True, "result": {"image": base64.b64encode(jpeg).decode()}}
        with patch("instagram_agent.cloudflare_client.urlopen", return_value=self.reply(body)) as call:
            self.assertEqual(self.client().image("a flat white"), jpeg)
        request = call.call_args.args[0]
        self.assertTrue(request.get_header("Content-type").startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="width"\r\n\r\n1024', request.data)
        bad = {"success": True, "result": {"image": base64.b64encode(b"GIF89a").decode()}}
        with patch("instagram_agent.cloudflare_client.urlopen", return_value=self.reply(bad)):
            with self.assertRaises(CloudflareError):
                self.client().image("x")

    def test_errors_hide_the_token(self):
        error = HTTPError("https://api.cloudflare.com/x", 401, "x", {}, io.BytesIO(b"cf-secret"))
        with (
            patch("instagram_agent.cloudflare_client.urlopen", side_effect=error),
            self.assertRaises(CloudflareError) as caught,
        ):
            self.client().chat_json("s", "u")
        self.assertEqual(str(caught.exception), "Workers AI request failed (HTTP 401)")


class ProviderTransportTests(unittest.TestCase):
    def test_redirects_are_refused_so_the_token_stays_put(self):
        from urllib.request import Request

        from instagram_agent.openai_client import NoRedirects

        request = Request("https://api.openai.com/v1/x", headers={"Authorization": "Bearer sk-test"})
        with self.assertRaises(HTTPError):
            NoRedirects().redirect_request(request, io.BytesIO(), 302, "Found", {}, "https://evil.example/")

    def test_truncated_replies_become_provider_errors(self):
        from http.client import IncompleteRead

        with (
            patch("instagram_agent.openai_client.urlopen", side_effect=IncompleteRead(b"")),
            self.assertRaises(OpenAIError),
        ):
            OpenAIClient("sk-test-secret", retries=0).chat_json("s", "u")
        with (
            patch("instagram_agent.cloudflare_client.urlopen", side_effect=IncompleteRead(b"")),
            self.assertRaises(CloudflareError),
        ):
            CloudflareAI("0123456789abcdef0123456789abcdef", "cf", retries=0).chat_json("s", "u")


class CronInstallTests(unittest.TestCase):
    def test_keeps_unrelated_entries_and_replaces_only_its_own(self):
        previous = (
            b"* * * * * cd /app && ./scripts/refresh-energy.sh >/dev/null 2>&1 # personal-ai-energy-refresh\n"
            b"*/15 * * * * cd /old && ./scripts/instagram-daily.sh # personal-ai-instagram-daily\n"
            b"# */15 * * * * commented # personal-ai-instagram-daily\n"
        )
        result = cron_install.marked_crontab(previous, Path("/srv/my app"))
        lines = result.decode().splitlines()
        self.assertEqual(lines[0], previous.decode().splitlines()[0])
        self.assertEqual(lines[1], "# */15 * * * * commented # personal-ai-instagram-daily")
        self.assertEqual(
            lines[2],
            "*/15 * * * * cd '/srv/my app' && ./scripts/instagram-daily.sh >/dev/null 2>&1 # personal-ai-instagram-daily",
        )


class DailyNotificationTests(unittest.TestCase):
    def setUp(self):
        rendering = patch.object(cli.render, "can_render", return_value=True)
        rendering.start()
        self.addCleanup(rendering.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = storage.connect(self.root / "instagram.sqlite3")
        self.addCleanup(self.conn.close)
        self.db = self.root / "notifications.sqlite3"
        self.env = {
            "OPENAI_API_KEY": "sk-test-secret",
            "INSTAGRAM_VOICE_FILE": str(self.root / "voice.md"),
            "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.db),
            "INSTAGRAM_AUTO_PUBLISH": "1",
            "INSTAGRAM_ACCESS_TOKEN": "IGtoken-secret",
            "INSTAGRAM_USER_ID": "17841408862128347",
            "INSTAGRAM_PUBLIC_DIR": str(self.root / "public"),
            "INSTAGRAM_PUBLIC_URL": "https://example.test/ig-media",
        }

    def tick(self, ai=None, now=MORNING, env=None):
        ai = ai or FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]])
        with (
            patch.object(cli, "model_client", return_value=ai if ai != "none" else None),
            patch.object(
                image_review, "client", return_value=Mock(chat_json=Mock(return_value={"pass": True, "reasons": []}))
            ),
        ):
            return cli.daily(self.conn, env or self.env, self.root / "drafts", now=now)

    def notes(self):
        if not self.db.exists():
            return []
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT outcome, title, link FROM notifications ORDER BY id").fetchall()

    def test_creation_does_not_claim_ready_before_visual_review(self):
        self.assertEqual(self.tick(), "ok")
        [(outcome, title, link)] = self.notes()
        self.assertEqual((outcome, link), ("ok", "https://iramkhan.com/command-centre/#instagram"))
        self.assertEqual(title, "1 Instagram draft created; awaiting image review and your approval")
        self.assertEqual(self.tick(), "skipped")
        self.assertEqual(len(self.notes()), 1)

    def test_rejected_drafts_are_not_included_in_the_awaiting_approval_notice(self):
        self.assertEqual(self.tick(), "ok")
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='rejected'")
        self.db.unlink()
        cli._notify_daily(self.conn, self.env, "ok", MORNING)
        self.assertEqual(self.notes(), [])

    def test_drafts_ready_without_auto_posting(self):
        for key in ("INSTAGRAM_AUTO_PUBLISH", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_PUBLIC_URL"):
            with self.subTest(missing=key):
                self.conn.execute("DELETE FROM drafts")
                self.conn.execute("DELETE FROM daily_runs")
                self.conn.commit()
                self.db.unlink(missing_ok=True)
                self.tick(env={k: v for k, v in self.env.items() if k != key})
                self.assertEqual(
                    self.notes()[0][1], "1 Instagram draft created; awaiting image review and your approval"
                )

    def test_no_automatic_posting_promise_before_approval(self):
        self.tick()
        title = self.notes()[0][1]
        self.assertTrue(title.endswith("awaiting image review and your approval"))

    def test_failures_partial_and_gave_up_are_reported(self):
        for _ in range(3):
            self.assertEqual(self.tick(FakeAI([[], [], []], [])), "failed")
        self.assertEqual(self.tick(), "gave_up")
        self.assertEqual(self.tick(), "gave_up")
        notes = self.notes()
        self.assertEqual([n[0] for n in notes], ["failed"] * 4)
        self.assertTrue(notes[0][1].startswith("Instagram drafts failed: "))
        self.assertEqual(notes[3][1], "Instagram drafts gave up for today after repeated failures")

    @patch.object(cli, "DAILY_DRAFTS", 3)
    def test_a_partial_run_is_reported(self):
        self.assertEqual(
            self.tick(FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]], fail_images_after=9)),
            "partial",
        )
        self.assertEqual(
            self.notes()[0][:2],
            (
                "failed",
                "Instagram drafts partial: 1 of 3 drafts not made: slide image failed (OpenAI request failed (HTTP 500))",
            ),
        )

    def test_not_configured_is_reported_once_a_day(self):
        for _ in range(2):
            self.assertEqual(self.tick("none"), "not_configured")
        self.assertEqual(
            self.notes(),
            [
                (
                    "failed",
                    "Instagram drafts not made: no AI provider key is set",
                    "https://iramkhan.com/command-centre/#instagram",
                )
            ],
        )

    def test_skipped_sends_nothing(self):
        self.assertEqual(self.tick(now=datetime(2026, 11, 12, 8, 45, tzinfo=timezone.utc)), "skipped")
        self.assertEqual(self.notes(), [])


if __name__ == "__main__":
    unittest.main()
