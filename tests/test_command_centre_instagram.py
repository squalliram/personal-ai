import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from command_centre_fixtures import Client, Clock, make_app, make_settings

from command_centre.security import Permission
from instagram_agent import storage

DRAFT = "0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 16


class InstagramQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db, self.media = self.root / "instagram.sqlite3", self.root / "drafts"
        conn = storage.connect(self.db)
        with conn:
            storage.insert_draft(
                conn,
                {
                    "draft_id": DRAFT,
                    "category": "coffee",
                    "topic": "Flat white guide",
                    "slides": [{"text": "Slide one"}, {"text": "Slide two"}, {"text": "Slide three"}],
                    "caption": "Save this.",
                    "hashtags": ["#coffee", "#flatwhite"],
                    "review_score": 82,
                    "scheduled_for": "2026-11-12T19:00+00:00",
                },
                "2026-11-12",
                "2026-11-12T10:00:00+00:00",
            )
            storage.record_run(conn, "2026-11-12", "2026-11-12T10:00:00+00:00", "ok", 1)
        conn.close()
        (self.media / DRAFT).mkdir(parents=True)
        (self.media / DRAFT / "slide-1.png").write_bytes(PNG)
        (self.media / DRAFT / "slide-2.jpg").write_bytes(b"\xff\xd8\xff")

    def client(self, permission=Permission.RECOMMEND):
        settings = make_settings(
            self.root, permission_ceiling=permission, instagram_db=self.db, instagram_media=self.media
        )
        self.app = make_app(settings, clock=Clock())
        client = Client(self.app)
        client.login()
        return client

    def decide(self, client, decision, draft=DRAFT, csrf=True):
        headers = {"X-CSRF-Token": client.csrf} if csrf else {}
        return client.request(
            "POST", "/api/v1/instagram/decision", body={"draft_id": draft, "decision": decision}, headers=headers
        )

    def test_drafts_and_slides_need_a_session(self):
        anonymous = Client(make_app(make_settings(self.root, instagram_db=self.db, instagram_media=self.media)))
        for path in ("/api/v1/instagram/drafts", "/api/v1/instagram/slide"):
            self.assertEqual(anonymous.request("GET", path).status, 401)

    def test_lists_drafts_with_slides_tags_and_last_run(self):
        data = self.client(Permission.READ).request("GET", "/api/v1/instagram/drafts").json()
        self.assertEqual(data["state"], "available")
        (draft,) = data["drafts"]
        self.assertEqual(draft["hashtags"], ["#coffee", "#flatwhite"])
        self.assertEqual([s["text"] for s in draft["slides"]], ["Slide one", "Slide two", "Slide three"])
        self.assertEqual((draft["status"], data["runs"][0]["outcome"]), ("pending", "ok"))

    def test_old_pending_drafts_stay_listed_past_the_decision_cap(self):
        conn = storage.connect(self.db)
        with conn:
            for n in range(35):
                draft = {
                    "draft_id": f"{n:016x}",
                    "category": "food",
                    "topic": f"t{n}",
                    "slides": [],
                    "caption": "c",
                    "hashtags": [],
                }
                storage.insert_draft(conn, draft, "2026-11-13", f"2026-11-13T10:{n:02d}:00+00:00")
                storage.decide_draft(conn, draft["draft_id"], "rejected", "t")
        conn.close()
        data = self.client().request("GET", "/api/v1/instagram/drafts").json()
        self.assertEqual(len(data["drafts"]), 31)
        self.assertIn(DRAFT, [d["draft_id"] for d in data["drafts"]])

    def test_not_configured_without_a_database(self):
        self.db.unlink()
        data = self.client().request("GET", "/api/v1/instagram/drafts").json()
        self.assertEqual((data["state"], data["drafts"]), ("not_configured", []))

    def test_slides_are_served_by_id_only(self):
        client = self.client(Permission.READ)
        ok = client.request("GET", "/api/v1/instagram/slide", query=f"draft={DRAFT}&n=1")
        self.assertEqual((ok.status, ok.header("Content-Type"), ok.body), (200, "image/png", PNG))
        jpeg = client.request("GET", "/api/v1/instagram/slide", query=f"draft={DRAFT}&n=2")
        self.assertEqual((jpeg.status, jpeg.header("Content-Type")), (200, "image/jpeg"))
        for query in (f"draft={DRAFT}&n=3", f"draft={DRAFT}&n=11", "draft=../../etc&n=1", f"draft={DRAFT}&n=-1"):
            with self.subTest(query=query):
                self.assertIn(client.request("GET", "/api/v1/instagram/slide", query=query).status, (400, 404))

    def test_approve_records_the_decision_and_memory_once(self):
        client = self.client()
        response = self.decide(client, "approve")
        self.assertEqual((response.status, response.json()), (200, {"draft_id": DRAFT, "status": "approved"}))
        self.assertEqual(self.decide(client, "reject").status, 409)
        conn = storage.connect(self.db)
        self.assertEqual(conn.execute("SELECT decision FROM examples").fetchone()[0], "approved")
        conn.close()
        outcomes = [(e["action"], e["outcome"]) for e in self.app.audit.recent(10)]
        self.assertIn(("instagram_decision", "approved"), outcomes)
        self.assertIn(("instagram_decision", "already_decided"), outcomes)

    def test_decision_is_refused_while_publishing(self):
        conn = storage.connect(self.db)
        with conn:
            conn.execute(
                "INSERT INTO publish_runs(draft_id, started_at, outcome) VALUES(?, ?, 'started')",
                (DRAFT, "2026-11-12T19:00:00+00:00"),
            )
        conn.close()
        client = self.client()
        draft = client.request("GET", "/api/v1/instagram/drafts").json()["drafts"][0]
        self.assertTrue(draft["publishing"])
        response = self.decide(client, "reject")
        self.assertEqual((response.status, response.json()["error"]), (409, "publishing"))
        outcomes = [(e["action"], e["outcome"]) for e in self.app.audit.recent(10)]
        self.assertIn(("instagram_decision", "publishing"), outcomes)

    def test_decisions_need_recommend_csrf_and_a_valid_body(self):
        self.assertEqual(self.decide(self.client(Permission.READ), "approve").status, 403)
        client = self.client()
        self.assertEqual(self.decide(client, "approve", csrf=False).status, 403)
        self.assertEqual(self.decide(client, "publish").status, 400)
        self.assertEqual(self.decide(client, "approve", draft="ffffffffffffffff").status, 404)
        self.assertEqual(self.decide(client, "approve", draft="../x").status, 400)
        extra = client.request(
            "POST",
            "/api/v1/instagram/decision",
            body={"draft_id": DRAFT, "decision": "approve", "post": True},
            headers={"X-CSRF-Token": client.csrf},
        )
        self.assertEqual(extra.status, 400)
        cross = client.request(
            "POST",
            "/api/v1/instagram/decision",
            body={"draft_id": DRAFT, "decision": "approve"},
            headers={"X-CSRF-Token": client.csrf},
            origin="https://evil.example",
        )
        self.assertEqual(cross.status, 403)


if __name__ == "__main__":
    unittest.main()


class InstagramAcrossJarvisTests(unittest.TestCase):
    NOW = datetime.fromisoformat("2026-11-12T12:00:00+00:00").timestamp()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "instagram.sqlite3"

    def seed(self, *runs, pending=1):
        conn = storage.connect(self.db)
        with conn:
            for n in range(pending):
                draft = {
                    "draft_id": f"{n:016x}",
                    "category": "coffee",
                    "topic": "Latte",
                    "slides": [],
                    "caption": "",
                    "hashtags": [],
                }
                storage.insert_draft(conn, draft, "2026-11-12", "2026-11-12T10:00:00+00:00")
            for started_at, outcome in runs:
                storage.record_run(conn, started_at[:10], started_at, outcome, 3 if outcome == "ok" else 0)
        conn.close()

    def client(self, clock=None):
        settings = make_settings(self.root, instagram_db=self.db)
        client = Client(make_app(settings, clock=clock or Clock(self.NOW)))
        client.login()
        return client

    def get(self, client, path):
        response = client.request("GET", path)
        self.assertEqual(response.status, 200)
        return json.loads(response.body)

    def instagram_health(self, client):
        found = self.get(client, "/api/v1/integrations")["integrations"]
        return next(item for item in found if item["id"] == "instagram")

    def test_pending_drafts_become_an_action_on_the_overview(self):
        self.seed(("2026-11-12T10:00:00+00:00", "ok"), ("2026-11-12T10:15:00+00:00", "skipped"), pending=2)
        client = self.client()
        overview = self.get(client, "/api/v1/overview")
        action = next(a for a in overview["attention"] if a["kind"] == "action")
        self.assertEqual(action["severity"], "action")
        self.assertEqual(action["href"], "#instagram")
        self.assertIn("2 Instagram drafts", action["fact"])
        self.assertEqual(overview["instagram"]["pending_drafts"], 2)
        # The latest real attempt is used, not the skipped tick after it.
        self.assertEqual(overview["instagram"]["latest_attempt"]["outcome"], "ok")
        self.assertEqual(overview["instagram"]["status"], "operational")
        self.assertIn("instagram-daily", [job["id"] for job in overview["automations"]])

    def test_health_follows_the_latest_drafting_attempt(self):
        cases = [
            ((("2026-11-12T10:00:00+00:00", "failed"),), "degraded"),
            ((("2026-11-12T10:00:00+00:00", "partial"),), "degraded"),
            ((("2026-11-12T10:00:00+00:00", "not_configured"),), "not_configured"),
            ((("2026-11-09T10:00:00+00:00", "ok"),), "stale"),
            ((("2026-11-12T10:00:00+00:00", "skipped"),), "unverified"),
        ]
        for runs, expected in cases:
            with self.subTest(expected=expected, runs=runs):
                self.db.unlink(missing_ok=True)
                self.seed(*runs, pending=0)
                self.assertEqual(self.instagram_health(self.client())["status"], expected)

    def test_missing_database_is_not_configured_and_raises_no_action(self):
        client = self.client()
        self.assertEqual(self.instagram_health(client)["status"], "not_configured")
        overview = self.get(client, "/api/v1/overview")
        self.assertFalse([a for a in overview["attention"] if a["kind"] == "action"])
        job = next(j for j in self.get(client, "/api/v1/automations")["automations"] if j["id"] == "instagram-daily")
        self.assertEqual(job["status"], "not_configured")

    def test_agents_and_automations_show_live_instagram_activity(self):
        self.seed(("2026-11-12T10:00:00+00:00", "ok"))
        client = self.client()
        agents = {a["id"]: a for a in self.get(client, "/api/v1/agents")["agents"]}
        self.assertEqual(agents["instagram"]["activity"]["pending_drafts"], 1)
        self.assertEqual(agents["instagram"]["activity"]["latest_run"]["outcome"], "ok")
        self.assertIsNone(agents["energy-analyst"]["activity"])
        job = next(j for j in self.get(client, "/api/v1/automations")["automations"] if j["id"] == "instagram-daily")
        self.assertEqual(job["status"], "operational")
        self.assertEqual(job["execution_history"][0]["status"], "ok")

    def test_malformed_slides_do_not_break_the_overview(self):
        self.seed(("2026-11-12T10:00:00+00:00", "ok"))
        conn = storage.connect(self.db)
        with conn:
            conn.execute("UPDATE drafts SET slides='1'")
        conn.close()
        client = self.client()
        self.assertEqual(self.get(client, "/api/v1/overview")["instagram"]["pending_drafts"], 1)
