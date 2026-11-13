import base64
import io
import json
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from instagram_agent import cli, cron_install, drafts, storage
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

    def chat_json(self, system, user):
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
        self.assertEqual((row["draft_id"], row["status"], row["created_on"]), (folder.name, "pending", "2026-11-12"))
        self.assertEqual(json.loads(row["slides"])[0], {"text": "one 0", "file": "slide-1.png"})

    def test_decisions_only_move_pending_drafts_and_feed_memory(self):
        draft = {**drafts.normalise(raw_draft("flat white")), "draft_id": "a" * 16, "hashtags": ["#coffee"]}
        with self.conn:
            storage.insert_draft(self.conn, draft, "2026-11-12", "2026-11-12T10:00:00+00:00")
        self.assertTrue(storage.decide_draft(self.conn, "a" * 16, "approved", "t1"))
        self.assertFalse(storage.decide_draft(self.conn, "a" * 16, "rejected", "t2"))
        example = self.conn.execute("SELECT decision, topic, hashtags FROM examples").fetchone()
        self.assertEqual(tuple(example), ("approved", "flat white", "#coffee"))


class DailyTickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = storage.connect(self.root / "instagram.sqlite3")
        self.env = {"OPENAI_API_KEY": "sk-test-secret", "INSTAGRAM_VOICE_FILE": str(self.root / "voice.md")}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def tick(self, now, ai=None):
        ai = ai or FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]])
        with patch.object(cli, "model_client", return_value=ai):
            return cli.daily(self.conn, self.env, self.root / "drafts", now=now)

    def test_runs_once_a_day_from_nine(self):
        self.assertEqual(self.tick(datetime(2026, 11, 12, 8, 45, tzinfo=timezone.utc)), "skipped")
        self.assertEqual(self.tick(MORNING), "ok")
        self.assertEqual(storage.drafts_on(self.conn, "2026-11-12"), 3)
        self.assertEqual(self.tick(MORNING), "skipped")

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

    def test_voice_profile_reaches_the_create_agent(self):
        (self.root / "voice.md").write_text("Warm, short sentences, Manchester slang.", encoding="utf-8")
        ai = FakeAI([[raw_draft("a"), raw_draft("b"), raw_draft("c")]], [[90, 80, 70]])
        self.tick(MORNING, ai)
        self.assertIn("Manchester slang", ai.calls[0][0])


class SlidePromptTests(unittest.TestCase):
    def test_text_is_drawn_only_by_models_that_spell_it(self):
        slide = {"text": "Flat white 101", "image_prompt": "a flat white on marble"}
        self.assertIn('spelled exactly: "Flat white 101"', drafts.slide_prompt(slide, draws_text=True))
        clean = drafts.slide_prompt(slide, draws_text=False)
        self.assertNotIn("Flat white 101", clean)
        self.assertIn("No text", clean)
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


if __name__ == "__main__":
    unittest.main()
