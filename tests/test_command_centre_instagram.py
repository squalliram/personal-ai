import tempfile
import unittest
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

    def test_not_configured_without_a_database(self):
        self.db.unlink()
        data = self.client().request("GET", "/api/v1/instagram/drafts").json()
        self.assertEqual((data["state"], data["drafts"]), ("not_configured", []))

    def test_slides_are_served_by_id_only(self):
        client = self.client(Permission.READ)
        ok = client.request("GET", "/api/v1/instagram/slide", query=f"draft={DRAFT}&n=1")
        self.assertEqual((ok.status, ok.header("Content-Type"), ok.body), (200, "image/png", PNG))
        for query in (f"draft={DRAFT}&n=2", f"draft={DRAFT}&n=11", "draft=../../etc&n=1", f"draft={DRAFT}&n=-1"):
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
