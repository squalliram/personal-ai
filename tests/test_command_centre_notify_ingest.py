import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from command_centre_fixtures import Client, Clock, make_settings

from command_centre.app import CommandCentreApp
from command_centre.config import load_settings

TOKEN = "0123456789abcdef" * 4
BOT = "123456:" + "A" * 30
PATH = "/api/v1/notifications"
BODY = {"source": "jarvis", "outcome": "ok", "title": "Created a calendar event", "link": "https://example.com/e/1"}


class FakeNotify:
    def __init__(self, result=7):
        self.calls, self.result = [], result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.clock = Clock()
        self.fake = FakeNotify()
        self.app = self.make(notify_ingest_token=TOKEN, notifications_db=self.root / "notifications.sqlite3")

    def make(self, notify=None, **overrides):
        return CommandCentreApp(
            make_settings(self.root, **overrides),
            allowed_hosts={"127.0.0.1:8770"},
            clock=self.clock,
            monotonic=self.clock,
            cron_reader=lambda: ("absent", ""),
            notify=notify or self.fake,
        )

    def post(self, body=BODY, *, token=TOKEN, app=None, raw=None, **kwargs):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        # Machine clients send no browser Origin header.
        return Client(app or self.app).request(
            "POST", PATH, body=body, raw=raw, headers=headers, origin=False, **kwargs
        )

    def audit(self, app=None):
        return [(e["action"], e["outcome"], e["actor"], e["detail"]) for e in (app or self.app).audit.recent(100)]


class IngestTests(IngestTestCase):
    def test_happy_path_stores_through_notify_and_returns_the_id(self):
        response = self.post()
        self.assertEqual(response.status, 201)
        self.assertEqual(response.json(), {"id": 7})
        ((args, kwargs),) = self.fake.calls
        self.assertEqual(args, ("jarvis", "ok", "Created a calendar event", "https://example.com/e/1"))
        self.assertEqual(kwargs["env"], {"COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.root / "notifications.sqlite3")})
        self.assertEqual(self.audit()[0][:3], ("notification_ingest", "success", "jarvis"))

    def test_link_is_optional_and_failed_is_accepted(self):
        for body in ({"source": "jarvis", "outcome": "failed", "title": "x"}, {**BODY, "link": None}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status, 201)
        self.assertEqual(self.fake.calls[0][0], ("jarvis", "failed", "x", None))

    def test_telegram_settings_are_passed_to_notify(self):
        app = self.make(
            notify_ingest_token=TOKEN,
            notifications_db=self.root / "n.sqlite3",
            telegram_bot_token=BOT,
            telegram_chat_id="-1001",
        )
        self.assertEqual(self.post(app=app).status, 201)
        env = self.fake.calls[0][1]["env"]
        self.assertEqual((env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]), (BOT, "-1001"))

    def test_missing_or_wrong_token_is_401(self):
        for headers in (
            {},
            {"Authorization": "Bearer " + "f" * 64},
            {"Authorization": TOKEN},
            {"Authorization": "Basic " + TOKEN},
        ):
            with self.subTest(headers=headers):
                response = Client(self.app).request("POST", PATH, body=BODY, headers=headers, origin=False)
                self.assertEqual(response.status, 401)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.audit()[0][:3], ("notification_ingest", "unauthorized", "anonymous"))

    def test_a_signed_in_cookie_session_is_not_a_credential(self):
        client = Client(self.app)
        client.login()
        response = client.request("POST", PATH, body=BODY, headers={"X-CSRF-Token": client.csrf})
        self.assertEqual(response.status, 401)

    def test_invalid_bodies_are_400(self):
        bad = [
            {**BODY, "extra": 1},
            {k: v for k, v in BODY.items() if k != "title"},
            {**BODY, "source": "instagram"},
            {**BODY, "source": ["jarvis"]},
            {**BODY, "outcome": "info"},
            {**BODY, "title": ""},
            {**BODY, "title": "   "},
            {**BODY, "title": "x" * 201},
            {**BODY, "title": 5},
            {**BODY, "link": "http://example.com/"},
            {**BODY, "link": "javascript:alert(1)"},
            {**BODY, "link": "https://example.com/" + "a" * 500},
            [BODY],
        ]
        for body in bad:
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.status, 400)
                self.assertEqual(response.json(), {"error": "invalid_parameters"})
        for raw, content_type in ((b"{not json", None), (json.dumps(BODY).encode(), "text/plain")):
            with self.subTest(raw=raw):
                self.assertEqual(self.post(raw=raw, content_type=content_type).status, 400)
        self.assertEqual(self.post({**BODY, "title": "x" * 200}).status, 201)
        self.assertEqual(len(self.fake.calls), 1)

    def test_body_over_4kb_is_413(self):
        response = self.post({**BODY, "title": "x" * 5000})
        self.assertEqual(response.status, 413)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.audit()[0][1], "too_large")

    def test_more_than_30_requests_in_10_minutes_is_429(self):
        statuses = [self.post().status for _ in range(31)]
        self.assertEqual(statuses, [201] * 30 + [429])
        self.assertEqual(self.post(token=None).status, 429)
        self.clock.now += 601
        self.assertEqual(self.post().status, 201)

    def test_shared_rate_limit_counts_across_workers(self):
        settings = {
            "notify_ingest_token": TOKEN,
            "sessions_db": self.root / "s.sqlite3",
            "notifications_db": self.root / "n.sqlite3",
        }
        first, second = self.make(**settings), self.make(**settings)
        for _ in range(15):
            self.post(app=first), self.post(app=second)
        self.assertEqual(self.post(app=first).status, 429)

    def test_not_configured_is_503(self):
        app = self.make()
        response = self.post(app=app)
        self.assertEqual(response.status, 503)
        self.assertEqual(response.json(), {"error": "not_configured"})
        self.assertEqual(self.audit(app)[0][:2], ("notification_ingest", "not_configured"))

    def test_storage_failure_is_500(self):
        app = self.make(FakeNotify(None), notify_ingest_token=TOKEN, notifications_db=self.root / "n.sqlite3")
        self.assertEqual(self.post(app=app).status, 500)
        self.assertEqual(self.audit(app)[0][1], "failure")

    def test_audit_never_holds_the_title_link_or_token(self):
        self.post()
        self.post(token="f" * 64)
        self.post({**BODY, "extra": 1})
        dump = json.dumps(self.app.audit.recent(100))
        for secret in (TOKEN, "f" * 64, BODY["title"], BODY["link"]):
            self.assertNotIn(secret, dump)
        self.assertEqual([o for _, o, _, _ in self.audit()], ["invalid", "unauthorized", "success"])

    def test_inbox_get_still_needs_a_session_and_lists_ingested_rows(self):
        import notifications

        app = self.make(notifications.notify, notify_ingest_token=TOKEN, notifications_db=self.root / "inbox.sqlite3")
        self.assertEqual(Client(app).request("GET", PATH).status, 401)
        self.assertEqual(Client(app).request("GET", PATH, headers={"Authorization": f"Bearer {TOKEN}"}).status, 401)
        response = self.post(app=app)
        self.assertEqual(response.status, 201)
        with closing(sqlite3.connect(self.root / "inbox.sqlite3")) as conn:
            self.assertEqual(
                conn.execute("SELECT id, delivery FROM notifications").fetchall(), [(response.json()["id"], "none")]
            )
        client = Client(app)
        client.login()
        entries = client.request("GET", PATH).json()["entries"]
        self.assertEqual([(e["source"], e["title"]) for e in entries], [("jarvis", BODY["title"])])

    def test_other_methods_are_405(self):
        response = Client(self.app).request("PUT", PATH, body=BODY, origin=False)
        self.assertEqual(response.status, 405)
        self.assertEqual(response.header("Allow"), "GET, POST")


class SettingsTests(unittest.TestCase):
    def test_token_must_be_64_hex(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for value, expected in ((TOKEN, TOKEN), ("short", None), ("g" * 64, None), ("", None)):
                with self.subTest(value=value):
                    settings = load_settings(root, {"COMMAND_CENTRE_NOTIFY_INGEST_TOKEN": value})
                    self.assertEqual(settings.notify_ingest_token, expected)
            settings = load_settings(
                root, {"COMMAND_CENTRE_TELEGRAM_BOT_TOKEN": BOT, "COMMAND_CENTRE_TELEGRAM_CHAT_ID": "42"}
            )
            self.assertEqual((settings.telegram_bot_token, settings.telegram_chat_id), (BOT, "42"))
            self.assertNotIn(BOT, repr(settings))
            self.assertNotIn(TOKEN, repr(load_settings(root, {"COMMAND_CENTRE_NOTIFY_INGEST_TOKEN": TOKEN})))


if __name__ == "__main__":
    unittest.main()
