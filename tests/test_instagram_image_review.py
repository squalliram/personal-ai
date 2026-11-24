import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from command_centre_fixtures import make_settings
from test_instagram_drafts import MORNING, PNG, FakeAI, raw_draft
from test_instagram_publish import DUE, FakeGraph, PublishCase

from command_centre import sources
from instagram_agent import cli, drafts, image_review, publish, render, storage
from instagram_agent.cloudflare_client import CloudflareAI
from instagram_agent.openai_client import AIError, OpenAIClient, jpeg_data_url


def reviewer(*verdicts):
    ai = Mock(text_model="vision-test")
    if verdicts:
        ai.chat_json.side_effect = list(verdicts)
    else:
        ai.chat_json.return_value = {"pass": True, "reasons": []}
    return ai


class VisionTests(unittest.TestCase):
    def test_provider_selection_uses_existing_multimodal_clients(self):
        self.assertIsNone(image_review.client({}))
        self.assertIsNone(image_review.client({"CLOUDFLARE_API_TOKEN": "test"}))
        cf = {"CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_API_TOKEN": "test"}
        ai = image_review.client(cf)
        self.assertIsInstance(ai, CloudflareAI)
        self.assertEqual(ai.text_model, image_review.VISION_MODEL)
        self.assertIsInstance(image_review.client({**cf, "OPENAI_API_KEY": "test"}), CloudflareAI)
        self.assertIsNone(image_review.client({**cf, "CLOUDFLARE_ACCOUNT_ID": "invalid"}))
        ai = image_review.client({**cf, "OPENAI_API_KEY": "test", "INSTAGRAM_AI_PROVIDER": "openai"})
        self.assertIsInstance(ai, OpenAIClient)
        self.assertEqual(ai.text_model, "gpt-4o-mini")
        self.assertEqual(image_review.client({**cf, "CLOUDFLARE_VISION_MODEL": "override"}).text_model, "override")
        self.assertEqual(
            image_review.client({"OPENAI_API_KEY": "test", "OPENAI_VISION_MODEL": "override"}).text_model, "override"
        )

    def test_verdict_uses_actual_image_and_strict_json(self):
        ai = reviewer()
        self.assertEqual(image_review.check(ai, PNG)["status"], "passed")
        self.assertEqual(ai.chat_json.call_args.kwargs, {"images": [PNG]})
        self.assertIn("generated slide photo", ai.chat_json.call_args.args[1])
        ai = reviewer({"pass": False, "reasons": ["floating spoon"]})
        result = image_review.check(ai, PNG, frame=True)
        self.assertEqual((result["status"], result["reasons"]), ("failed", ["floating spoon"]))
        self.assertIn("reel frame", ai.chat_json.call_args.args[1])
        for problem in (
            "hands",
            "fingers",
            "reflections",
            "pouring",
            "levitating",
            "warped",
            "garbled",
            "plastic-looking",
            "unappetising",
            "instructions",
        ):
            self.assertIn(problem, image_review.PROMPT)

    def test_missing_empty_malformed_and_error_results_fail_closed(self):
        for data in (b"", b"not an image"):
            ai = reviewer()
            self.assertEqual(image_review.check(ai, data)["status"], "unavailable")
            ai.chat_json.assert_not_called()
        self.assertEqual(image_review.check(None, PNG)["reasons"], ["image review unavailable"])
        for result in (
            None,
            [],
            {},
            {"pass": "true", "reasons": []},
            {"pass": True},
            {"pass": True, "reasons": [1]},
            {"pass": True, "reasons": ["hand"]},
        ):
            self.assertEqual(image_review.check(reviewer(result), PNG)["status"], "unavailable")
        ai = reviewer()
        ai.chat_json.side_effect = AIError("provider unavailable")
        self.assertEqual(image_review.check(ai, PNG)["status"], "unavailable")
        self.assertTrue(jpeg_data_url(PNG).startswith("data:image/png;base64,"))
        self.assertTrue(jpeg_data_url(b"\xff\xd8\xffdata").startswith("data:image/jpeg;base64,"))

    def test_owner_frames_allow_people_but_still_check_sanity(self):
        ai = reviewer()
        self.assertEqual(image_review.check(ai, PNG, frame=True, owner_media=True)["status"], "passed")
        self.assertEqual(ai.chat_json.call_args.args[0], image_review.OWNER_PROMPT)
        self.assertNotIn("Reject any person or body part", image_review.OWNER_PROMPT)
        for problem in ("warped", "garbled", "floating", "impossible geometry", "unappetising"):
            self.assertIn(problem, image_review.OWNER_PROMPT)
        image_review.check(ai, PNG, frame=True)
        self.assertEqual(ai.chat_json.call_args.args[0], image_review.PROMPT)
        image_review.check(ai, PNG, owner_media=True)
        self.assertEqual(ai.chat_json.call_args.args[0], image_review.PROMPT)

    def test_first_middle_last_frames_are_all_reviewed_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reel.mp4"
            path.write_bytes(b"rendered video")
            calls = []

            def extract(argv):
                calls.append(argv)
                Path(argv[-1]).write_bytes(PNG)

            ai = reviewer(
                {"pass": True, "reasons": []}, {"pass": False, "reasons": ["hand"]}, {"pass": True, "reasons": []}
            )
            result = image_review.frames(path, 9.0, ai, "ffmpeg", extract)
            self.assertEqual(result["status"], "failed")
            self.assertEqual([v["second"] for v in result["frames"]], [0, 4.5, 8.9])
            self.assertEqual(ai.chat_json.call_count, 3)
            self.assertEqual([float(argv[4]) for argv in calls], [0, 4.5, 8.9])
            self.assertEqual(list(Path(folder).iterdir()), [path])
            self.assertEqual(image_review.frames(path, 9.0, reviewer(), "ffmpeg", extract)["status"], "passed")
            owner = reviewer()
            self.assertEqual(
                image_review.frames(path, 9.0, owner, "ffmpeg", extract, owner_media=True)["status"], "passed"
            )
            self.assertTrue(all(c.args[0] == image_review.OWNER_PROMPT for c in owner.chat_json.call_args_list))
            self.assertEqual(image_review.frames(path, 9.0, None, "ffmpeg", extract)["status"], "unavailable")
            self.assertEqual(image_review.frames(path, 9.0, reviewer(), None, extract)["status"], "unavailable")
            self.assertEqual(
                image_review.frames(path, 9.0, reviewer(), "ffmpeg", Mock(side_effect=RuntimeError()))["status"],
                "unavailable",
            )


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / "drafts"
        self.conn = storage.connect(self.root / "instagram.sqlite3")
        self.addCleanup(self.conn.close)
        self.ai = FakeAI([], [])

    def create(self, vision):
        return drafts.render_and_store(
            self.conn, self.ai, [drafts.normalise(raw_draft("Soup", slides=3))], self.media, MORNING, reviewer=vision
        )

    def row(self):
        return self.conn.execute("SELECT * FROM drafts").fetchone()

    def test_every_slide_is_reviewed_and_retries_are_stored(self):
        vision = reviewer({"pass": False, "reasons": ["floating ladle"]}, *[{"pass": True, "reasons": []}] * 3)
        self.assertEqual(self.create(vision), 1)
        row = self.row()
        reel = json.loads(row["reel"])
        self.assertEqual((self.ai.images, vision.chat_json.call_count), (4, 4))
        self.assertEqual([len(v["attempts"]) for v in reel["image_review"]["slides"]], [2, 1, 1])
        self.assertEqual((row["status"], reel["hold"], reel["image_review"]["status"]), ("pending", True, "pending"))
        self.assertFalse(storage.decide_draft(self.conn, row["draft_id"], "approved", MORNING.isoformat()))
        self.assertFalse(publish.rendered(row))

    def test_three_failed_attempts_reject_with_visible_verdicts_and_notify(self):
        vision = reviewer(*[{"pass": False, "reasons": ["floating ladle"]}] * 3)
        self.assertEqual(self.create(vision), 1)
        row = self.row()
        self.assertEqual((self.ai.images, row["status"]), (3, "rejected"))
        self.assertIn("floating ladle", row["review_notes"])
        settings = make_settings(self.root, instagram_db=self.root / "instagram.sqlite3", instagram_media=self.media)
        [shown] = sources.instagram_drafts(settings)["drafts"]
        review = shown["reel"]["image_review"]
        self.assertEqual(review["status"], "failed")
        self.assertEqual(len(review["slides"][0]["attempts"]), 3)
        with patch.object(cli, "notify") as notify:
            cli._notify_visual_review(self.conn, {}, MORNING)
        self.assertIn("rejected", notify.call_args.args[2])
        self.assertEqual(notify.call_args.args[1], "failed")

    def test_no_vision_rejects_and_marks_unavailable_without_retrying(self):
        self.assertEqual(self.create(None), 1)
        row = self.row()
        self.assertEqual((self.ai.images, row["status"]), (1, "rejected"))
        self.assertIn("image review unavailable", row["review_notes"])
        self.assertEqual(json.loads(row["reel"])["image_review"]["status"], "unavailable")

    def test_render_checks_frames_before_approval_and_notifies_ready(self):
        self.create(reviewer())
        result = {"status": "passed", "reasons": [], "frames": [{"second": 0, "status": "passed", "reasons": []}]}
        with (
            patch.object(render, "render", side_effect=lambda c, m, d, r: {**r, "render": "done", "file": "reel.mp4"}),
            patch.object(image_review, "frames", return_value=result) as frames,
        ):
            self.assertEqual(
                render.render_due(self.conn, self.media, now=MORNING, reviewer=reviewer()), "rendered 1, failed 0"
            )
        frames.assert_called_once()
        self.assertFalse(frames.call_args.kwargs["owner_media"])
        row = self.row()
        review = json.loads(row["reel"])["image_review"]
        self.assertEqual((review["status"], len(review["slides"]), len(review["frames"])), ("passed", 3, 1))
        with patch.object(cli, "notify") as notify:
            cli._notify_visual_review(self.conn, {}, MORNING)
        self.assertIn("ready for your approval", notify.call_args.args[2])
        self.assertTrue(storage.decide_draft(self.conn, row["draft_id"], "approved", MORNING.isoformat()))

    def test_legacy_reel_requires_frame_review_before_approval_or_publishing(self):
        self.create(reviewer())
        with self.conn:
            self.conn.execute(
                "UPDATE drafts SET reel=json_set(json_remove(reel,'$.image_review'),'$.render','done','$.file','reel.mp4')"
            )
        row = self.row()
        self.assertFalse(publish.rendered(row))
        self.assertFalse(storage.decide_draft(self.conn, row["draft_id"], "approved", MORNING.isoformat()))
        with patch.object(
            image_review, "frames", return_value={"status": "passed", "frames": [], "reasons": []}
        ) as frames:
            render.render_due(self.conn, self.media, now=MORNING, reviewer=reviewer())
        frames.assert_called_once()
        self.assertTrue(publish.rendered(self.row()))
        self.assertTrue(storage.decide_draft(self.conn, row["draft_id"], "approved", MORNING.isoformat()))

    def test_real_upload_and_clip_provenance_selects_owner_frame_review(self):
        self.create(reviewer())
        draft_id = self.row()["draft_id"]
        for table in ("uploads", "clips"):
            with self.subTest(table=table):
                with self.conn:
                    self.conn.execute(
                        "UPDATE drafts SET reel=json_set(reel,'$.render','pending','$.image_review.status','pending')"
                    )
                    self.conn.execute("DELETE FROM uploads")
                    self.conn.execute("DELETE FROM clips")
                    if table == "uploads":
                        self.conn.execute(
                            "INSERT INTO uploads(upload_id,created_at,note,photos,status,draft_id) VALUES('owner','now','Food',1,'used',?)",
                            (draft_id,),
                        )
                    else:
                        self.conn.execute(
                            "INSERT INTO clips(clip_id,created_at,note,size,status,draft_id) VALUES('owner','now','Food',100,'used',?)",
                            (draft_id,),
                        )
                with (
                    patch.object(
                        render, "render", side_effect=lambda c, m, d, r: {**r, "render": "done", "file": "reel.mp4"}
                    ),
                    patch.object(
                        image_review, "frames", return_value={"status": "passed", "frames": [], "reasons": []}
                    ) as frames,
                ):
                    render.render_due(self.conn, self.media, now=MORNING, reviewer=reviewer())
                self.assertTrue(frames.call_args.kwargs["owner_media"])
                self.assertEqual(self.row()["status"], "pending")

    def test_bad_rendered_frame_rejects_even_when_slides_passed(self):
        for status in ("failed", "unavailable"):
            with self.subTest(status=status):
                self.conn.execute("DELETE FROM drafts")
                self.conn.commit()
                self.create(reviewer())
                result = {
                    "status": status,
                    "reasons": ["hand" if status == "failed" else "image review unavailable"],
                    "frames": [],
                }
                with (
                    patch.object(
                        render, "render", side_effect=lambda c, m, d, r: {**r, "render": "done", "file": "reel.mp4"}
                    ),
                    patch.object(image_review, "frames", return_value=result),
                    patch.object(cli, "notify") as notify,
                ):
                    render.render_due(
                        self.conn, self.media, cli._notify_render({}, MORNING), MORNING, reviewer=reviewer()
                    )
                row = self.row()
                self.assertEqual(row["status"], "rejected")
                self.assertFalse(publish.rendered(row))
                self.assertFalse(storage.decide_draft(self.conn, row["draft_id"], "approved", MORNING.isoformat()))
                self.assertEqual(notify.call_args.args[1], "failed")


class ApprovalTests(PublishCase):
    def test_approval_can_be_withdrawn_until_publication_starts(self):
        draft_id = self.add()
        self.assertTrue(storage.decide_draft(self.conn, draft_id, "rejected", DUE.isoformat()))
        self.assertEqual(self.tick(FakeGraph()), "published 0, failed 0")
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='approved' WHERE draft_id=?", (draft_id,))
            self.conn.execute(
                "INSERT INTO publish_runs(draft_id,started_at,outcome) VALUES(?,?,'started')",
                (draft_id, DUE.isoformat()),
            )
        self.assertFalse(storage.decide_draft(self.conn, draft_id, "rejected", DUE.isoformat()))
        with self.conn:
            self.conn.execute("UPDATE drafts SET status='published' WHERE draft_id=?", (draft_id,))
        self.assertFalse(storage.decide_draft(self.conn, draft_id, "rejected", DUE.isoformat()))

    def test_unapproved_drafts_never_publish_with_any_switch_or_source(self):
        draft_id = self.add(approved=False)
        for flag in (None, "0", "1", "on"):
            for state in (None, "on", "off"):
                for source in ("photos", "clip", "ai"):
                    with self.subTest(flag=flag, state=state, source=source):
                        env = {**self.env, "INSTAGRAM_AUTO_PUBLISH": flag}
                        if state is None:
                            with self.conn:
                                self.conn.execute("DELETE FROM publisher_state WHERE key='auto_publish'")
                        else:
                            publish.set_state(self.conn, state, DUE)
                        with self.conn:
                            self.conn.execute(
                                "UPDATE drafts SET reel=json_set(reel,'$.hold',0,'$.source',?)", (source,)
                            )
                        graph = FakeGraph()
                        self.assertEqual(
                            publish.tick(self.conn, env, self.media, DUE, graph),
                            "published 0, failed 0" if flag == "1" else "publish_off",
                        )
                        self.assertEqual(publish.due(self.conn, DUE), [])
                        self.assertEqual(graph.posts, [])
        row = self.conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        with self.assertRaises(publish.Withdrawn):
            publish.publish_reel(FakeGraph(), row, self.media, self.public, self.env["INSTAGRAM_PUBLIC_URL"])

    def test_approved_status_without_explicit_decision_cannot_publish_or_story(self):
        draft_id = self.add(approved=False)
        for status in ("approved", "published"):
            with self.conn:
                self.conn.execute("UPDATE drafts SET status=? WHERE draft_id=?", (status, draft_id))
            graph = FakeGraph()
            self.assertEqual(self.tick(graph), "published 0, failed 0")
            row = self.conn.execute("SELECT * FROM drafts").fetchone()
            publish.publish_story_once(
                self.conn, graph, row, self.media, self.public, self.env["INSTAGRAM_PUBLIC_URL"], self.env, DUE
            )
            self.assertEqual(graph.posts, [])

    def test_late_or_unscheduled_approval_publishes_at_next_tick(self):
        draft_id = self.add(approved=False)
        late = DUE + timedelta(days=2)
        self.assertTrue(storage.decide_draft(self.conn, draft_id, "approved", late.isoformat()))
        row = self.conn.execute("SELECT scheduled_for FROM drafts").fetchone()
        self.assertEqual(row[0], late.isoformat())
        self.assertEqual(self.tick(FakeGraph(), late + timedelta(minutes=1)), "published 1, failed 0")
        draft_id = self.add("b" * 16, approved=False, slot=None)
        self.assertTrue(storage.decide_draft(self.conn, draft_id, "approved", late.isoformat()))
        self.assertEqual(len(publish.due(self.conn, late)), 1)

    def test_holds_default_true_for_all_new_reel_sources(self):
        for source in ("ai", "photos", "clip"):
            draft = {
                "draft_id": source,
                "kind": "reel",
                "category": "food",
                "topic": "Soup",
                "caption": "Soup",
                "hashtags": [],
                "slides": [],
                "reel": {"source": source, "hold": False},
            }
            with self.conn:
                storage.insert_draft(self.conn, draft, "2026-11-12", MORNING.isoformat())
            row = self.conn.execute("SELECT reel FROM drafts WHERE draft_id=?", (source,)).fetchone()
            self.assertTrue(json.loads(row[0])["hold"])
            self.assertEqual(json.loads(row[0])["image_review"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
