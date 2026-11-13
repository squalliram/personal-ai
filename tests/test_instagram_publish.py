import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from instagram_agent import cli, env_install, publish, storage
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


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = storage.connect(self.root / "instagram.sqlite3")
        self.addCleanup(self.conn.close)
        self.media, self.public = self.root / "drafts", self.root / "public"
        self.env = {**ENV, "INSTAGRAM_PUBLIC_DIR": str(self.public)}
        self.checked = []

    def add(self, draft_id="a" * 16, *, score=80, slot=SLOT, made=MADE, slides=3, data=JPEG):
        with self.conn:
            storage.insert_draft(
                self.conn,
                {
                    "draft_id": draft_id,
                    "category": "coffee",
                    "topic": "Flat whites",
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

    def test_publishes_a_passing_pending_draft_as_a_carousel(self):
        self.add()
        graph = FakeGraph(statuses=("IN_PROGRESS", "FINISHED"))
        self.assertEqual(self.tick(graph), "published 1, failed 0")
        self.assertEqual(tuple(self.status()), ("published", "9001"))
        children = graph.posts[:3]
        self.assertTrue(all(p[1]["is_carousel_item"] == "true" for p in children))
        self.assertEqual([p[1]["image_url"] for p in children], self.checked)
        self.assertTrue(all(u.startswith("https://example.test/ig-media/") for u in self.checked))
        parent = graph.posts[3][1]
        self.assertEqual((parent["media_type"], parent["children"]), ("CAROUSEL", "c1,c2,c3"))
        self.assertEqual(parent["caption"], "Save this.\n\n#coffee #london")
        self.assertEqual(graph.posts[4], ("17841408862128347/media_publish", {"creation_id": "c4"}, False))
        self.assertEqual([p.name for p in self.public.iterdir()], ["index.html"])
        self.assertEqual(self.tick(FakeGraph()), "published 0, failed 0")

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
        self.assertEqual(error, "Instagram only accepts JPEG slides")

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
        self.assertEqual(result, "published 0, failed 1")
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
        for n in range(1, 4):
            (self.media / draft_id / f"slide-{n}.jpg").rename(self.media / draft_id / f"slide-{n}.png")
        self.assertEqual(self.tick(FakeGraph()), "published 0, failed 1")
        error = self.conn.execute("SELECT error FROM publish_runs").fetchone()[0]
        self.assertEqual(error, "Instagram only accepts JPEG slides")

    def test_a_crash_mid_publish_is_never_retried(self):
        draft_id = self.add()
        with self.conn:
            publish._record(self.conn, draft_id, "2026-11-12T18:00:00+00:00", "started")
        graph = FakeGraph()
        self.assertEqual(self.tick(graph), "published 0, failed 0")
        self.assertEqual((self.status()["status"], graph.posts), ("failed", []))


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
