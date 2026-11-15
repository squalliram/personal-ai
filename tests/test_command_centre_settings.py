import csv
import io
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from command_centre_fixtures import Client, Clock, make_app, make_settings

from command_centre.app import csv_cell, describe_device
from command_centre.security import Permission, SessionStore, SqliteSessionStore

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
MAC_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = Clock()
        self.app = make_app(make_settings(self.root), clock=self.clock)
        self.client = Client(self.app)
        self.client.login()

    def post(self, client, path, body):
        return client.request("POST", path, body=body, headers={"X-CSRF-Token": client.csrf})


class AuditPaginationTests(SettingsTestCase):
    def seed(self, count):
        for n in range(count):
            self.app.audit.record("probe", "success" if n % 2 else "failure", actor="owner", n=n)

    def test_pages_are_newest_first_with_totals(self):
        self.seed(30)
        first = self.client.request("GET", "/api/v1/audit", query="page=1&per_page=10").json()
        self.assertEqual((first["page"], first["per_page"], first["total"], first["pages"]), (1, 10, 31, 4))
        self.assertEqual(len(first["entries"]), 10)
        self.assertEqual(first["entries"][0]["detail"], {"n": 29})
        last = self.client.request("GET", "/api/v1/audit", query="page=4&per_page=10").json()
        self.assertEqual([e["action"] for e in last["entries"]], ["login"])
        ids = [e["id"] for e in first["entries"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_default_keeps_last_fifty(self):
        self.seed(60)
        data = self.client.request("GET", "/api/v1/audit").json()
        self.assertEqual(len(data["entries"]), 50)
        self.assertEqual(data["page_sizes"], [10, 25, 50, 100])

    def test_action_filter(self):
        self.seed(5)
        data = self.client.request("GET", "/api/v1/audit", query="action=login&per_page=10").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual({e["action"] for e in data["entries"]}, {"login"})
        self.assertEqual(data["actions"], ["login", "probe"])

    def test_parameters_are_validated(self):
        for query in ("page=0", "page=abc", "per_page=7", "per_page=1000", "action=DROP%20TABLE", "page=100001"):
            self.assertEqual(self.client.request("GET", "/api/v1/audit", query=query).status, 400, query)

    def test_requires_a_session(self):
        anonymous = Client(self.app)
        for path in ("/api/v1/audit", "/api/v1/audit/export", "/api/v1/sessions"):
            self.assertEqual(anonymous.request("GET", path, query="format=csv").status, 401)


class AuditExportTests(SettingsTestCase):
    def test_csv_export_is_an_attachment_and_neutralises_formulas(self):
        self.app.audit.record("probe", '=HYPERLINK("x")', actor="+owner", note="@SUM(A1)")
        response = self.client.request("GET", "/api/v1/audit/export", query="format=csv")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("Content-Type"), "text/csv; charset=utf-8")
        self.assertRegex(
            response.header("Content-Disposition"), r'^attachment; filename="jarvis-audit-\d{8}T\d{4}Z\.csv"$'
        )
        self.assertEqual(response.header("Cache-Control"), "no-store")
        rows = list(csv.reader(io.StringIO(response.body.decode())))
        self.assertEqual(rows[0], ["id", "at", "actor", "action", "outcome", "detail"])
        probe = next(row for row in rows[1:] if row[3] == "probe")
        self.assertEqual(probe[2], "'+owner")
        self.assertEqual(probe[4], '\'=HYPERLINK("x")')
        self.assertEqual(json.loads(probe[5]), {"note": "@SUM(A1)"})
        for row in rows[1:]:
            for cell in row:
                self.assertFalse(cell.lstrip(" ").startswith(("=", "+", "-", "@")), cell)

    def test_json_export_and_audit_of_the_export(self):
        response = self.client.request("GET", "/api/v1/audit/export", query="format=json&action=login")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.header("Content-Disposition").endswith('.json"'))
        data = response.json()
        self.assertEqual((data["action"], data["count"], data["truncated"]), ("login", 1, False))
        self.assertEqual(data["entries"][0]["action"], "login")
        latest = self.app.audit.recent(1)[0]
        self.assertEqual(
            (latest["action"], latest["detail"]["format"], latest["detail"]["rows"]), ("audit_export", "json", 1)
        )

    def test_export_format_is_validated(self):
        for query in ("", "format=xlsx", "format=csv&action=bad-action"):
            self.assertEqual(self.client.request("GET", "/api/v1/audit/export", query=query).status, 400)

    def test_csv_cell(self):
        for value in ("=1+1", "+1", "-1", "@x", "\t=1", "  =1", "\uff1d1"):
            self.assertTrue(csv_cell(value).startswith("'"), value)
        for value in ("login", "2026-01-01T00:00:00+00:00", 5, None):
            self.assertEqual(csv_cell(value), value)


class SessionManagementTests(SettingsTestCase):
    def sign_in(self, agent):
        client = Client(self.app)
        response = client.request(
            "POST", "/api/v1/session", body={"password": "correct horse battery staple"}, headers={"User-Agent": agent}
        )
        self.assertEqual(response.status, 200)
        client.csrf = response.json()["csrf_token"]
        return client

    def test_lists_sessions_with_coarse_devices_and_activity(self):
        phone = self.sign_in(IPHONE)
        self.app.audit.record("login", "failure", client="203.0.113.9")
        data = phone.request("GET", "/api/v1/sessions").json()
        self.assertEqual(len(data["sessions"]), 2)
        mine = [s for s in data["sessions"] if s["current"]]
        self.assertEqual([s["device"] for s in mine], ["Safari on iPhone"])
        self.assertEqual(data["sign_in_activity"]["failed_attempts"], 1)
        self.assertEqual(data["sign_in_activity"]["recent_sign_ins"][0]["device"], "Safari on iPhone")
        self.assertNotIn(IPHONE.encode(), phone.request("GET", "/api/v1/sessions").body)
        for item in data["sessions"]:
            self.assertRegex(item["id"], r"^[a-f0-9]{16}$")

    def test_sign_out_one_other_device(self):
        phone = self.sign_in(IPHONE)
        other = next(s for s in self.client.request("GET", "/api/v1/sessions").json()["sessions"] if not s["current"])
        response = self.post(self.client, "/api/v1/sessions/revoke", {"id": other["id"]})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.json()["sessions"]), 1)
        self.assertEqual(phone.request("GET", "/api/v1/session").status, 401)
        latest = self.app.audit.recent(1)[0]
        self.assertEqual(
            (latest["action"], latest["detail"]["scope"], latest["detail"]["revoked"]), ("session_revoke", "one", 1)
        )
        self.assertEqual(self.post(self.client, "/api/v1/sessions/revoke", {"id": other["id"]}).status, 404)

    def test_cannot_revoke_current_session_by_id(self):
        mine = self.client.request("GET", "/api/v1/sessions").json()["sessions"][0]
        self.assertEqual(self.post(self.client, "/api/v1/sessions/revoke", {"id": mine["id"]}).status, 400)
        self.assertEqual(self.client.request("GET", "/api/v1/session").status, 200)

    def test_sign_out_all_others_keeps_this_one(self):
        others = [self.sign_in(IPHONE), self.sign_in(MAC_CHROME)]
        response = self.post(self.client, "/api/v1/sessions/revoke", {"others": True})
        self.assertEqual(response.status, 200)
        self.assertEqual([s["current"] for s in response.json()["sessions"]], [True])
        for client in others:
            self.assertEqual(client.request("GET", "/api/v1/session").status, 401)
        self.assertEqual(self.client.request("GET", "/api/v1/session").status, 200)
        self.assertEqual(self.app.audit.recent(1)[0]["detail"]["revoked"], 2)

    def test_revoke_needs_csrf_same_origin_and_a_valid_body(self):
        path = "/api/v1/sessions/revoke"
        self.assertEqual(self.client.request("POST", path, body={"others": True}).status, 403)
        self.assertEqual(
            self.client.request(
                "POST",
                path,
                body={"others": True},
                origin="https://evil.example",
                headers={"X-CSRF-Token": self.client.csrf},
            ).status,
            403,
        )
        for body in ({}, {"others": False}, {"id": "nothex"}, {"id": "a" * 16, "others": True}, ["x"]):
            self.assertEqual(self.post(self.client, path, body).status, 400, body)

    def test_describe_device(self):
        self.assertEqual(describe_device(IPHONE), "Safari on iPhone")
        self.assertEqual(describe_device(MAC_CHROME), "Chrome on Mac")
        self.assertEqual(describe_device(""), "Unknown device")
        self.assertEqual(
            describe_device("Mozilla/5.0 (Linux; Android 14) Chrome/131.0 Mobile Safari/537.36"), "Chrome on Android"
        )


class SessionStoreDeviceTests(unittest.TestCase):
    def check(self, store):
        token, session = store.create(Permission.READ, device="Safari on iPhone", client="198.51.100.7")
        other, _ = store.create(Permission.READ)
        active = {s.token_hash: s for s in store.active()}
        self.assertEqual(len(active), 2)
        self.assertEqual(
            (active[session.token_hash].device, active[session.token_hash].client), ("Safari on iPhone", "198.51.100.7")
        )
        self.assertTrue(store.revoke_hash(session.token_hash))
        self.assertFalse(store.revoke_hash(session.token_hash))
        self.assertIsNone(store.get(token))
        self.assertIsNotNone(store.get(other))

    def test_memory_store(self):
        self.check(SessionStore(idle_seconds=60, absolute_seconds=600, clock=Clock(0)))

    def test_sqlite_store(self):
        with TemporaryDirectory() as directory:
            self.check(
                SqliteSessionStore(Path(directory) / "s.sqlite3", idle_seconds=60, absolute_seconds=600, clock=Clock(0))
            )

    def test_hosted_app_lists_shared_sessions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = make_app(replace(make_settings(root), sessions_db=root / "sessions.sqlite3"))
            client = Client(app)
            client.login()
            self.assertEqual(len(client.request("GET", "/api/v1/sessions").json()["sessions"]), 1)


if __name__ == "__main__":
    unittest.main()
