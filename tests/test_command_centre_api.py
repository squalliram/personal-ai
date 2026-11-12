import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from command_centre_fixtures import (
    HOST,
    PASSWORD,
    PASSWORD_HASH,
    Client,
    Clock,
    make_app,
    make_settings,
    seed_briefing,
    seed_energy,
)

from command_centre.app import Route
from command_centre.config import load_settings
from command_centre.security import Permission, RateLimiter

READ_ROUTES = (
    "/api/v1/session",
    "/api/v1/overview",
    "/api/v1/modules",
    "/api/v1/agents",
    "/api/v1/integrations",
    "/api/v1/automations",
    "/api/v1/energy",
    "/api/v1/briefing/runs",
    "/api/v1/audit",
)
SECURITY_HEADERS = {
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Cache-Control",
}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = Clock()
        self.settings = make_settings(self.root)
        self.app = make_app(self.settings, clock=self.clock)
        self.client = Client(self.app)

    def audit_outcomes(self):
        return [(e["action"], e["outcome"]) for e in self.app.audit.recent(100)]


class PublicSurfaceTests(ApiTestCase):
    def test_health_is_public_minimal_and_carries_security_headers(self):
        response = self.client.request("GET", "/api/v1/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertTrue(SECURITY_HEADERS <= {k for k, _ in response.headers})
        self.assertIn("frame-ancestors 'none'", response.header("Content-Security-Policy"))
        self.assertIn("script-src 'self'", response.header("Content-Security-Policy"))
        self.assertEqual(response.header("Cache-Control"), "no-store")
        self.assertIsNone(response.header("Strict-Transport-Security"))

    def test_every_data_route_requires_a_session(self):
        for path in READ_ROUTES:
            with self.subTest(path=path):
                response = self.client.request("GET", path)
                self.assertEqual(response.status, 401)
                self.assertEqual(response.json(), {"error": "authentication_required"})

    def test_unknown_host_is_refused_before_routing(self):
        for host in ("evil.example", "127.0.0.1:9999", None):
            with self.subTest(host=host):
                response = Client(self.app, host=host).request("GET", "/api/v1/health")
                self.assertEqual(response.status, 421)

    def test_shell_and_assets_are_served_from_a_fixed_allow_list(self):
        page = self.client.request("GET", "/")
        self.assertEqual(page.status, 200)
        self.assertIn(b'src="static/app.js"', page.body)  # relative URL: the app may be mounted on a subpath
        self.assertNotIn(b"<script>", page.body)  # no inline script, so the CSP needs no 'unsafe-inline'
        self.assertEqual(
            self.client.request("GET", "/static/app.js").header("Content-Type"), "text/javascript; charset=utf-8"
        )
        self.assertEqual(self.client.request("POST", "/").status, 405)
        (self.root / ".env").write_text("API_KEY=leak\n", encoding="utf-8")
        for path in ("/.env", "/static/../.env", "/static/app.py", "/data/command_centre.sqlite3", "//etc/passwd"):
            with self.subTest(path=path):
                response = self.client.request("GET", path)
                self.assertEqual(response.status, 404)
                self.assertNotIn(b"leak", response.body)

    def test_wrong_method_reports_allowed_methods(self):
        response = self.client.request("PUT", "/api/v1/session")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.header("Allow"), "DELETE, GET, POST")


class LoginTests(ApiTestCase):
    def test_successful_login_sets_hardened_cookie_and_audits(self):
        response = self.client.login()
        self.assertEqual(response.status, 200)
        body = response.json()
        self.assertEqual(body["permission"], "recommend")
        self.assertEqual(len(body["csrf_token"]), 43)
        cookie = response.header("Set-Cookie")
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=43200"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(self.client.request("GET", "/api/v1/session").status, 200)
        self.assertIn(("login", "success"), self.audit_outcomes())

    def test_tls_mode_uses_host_prefixed_secure_cookie_and_hsts(self):
        app = make_app(self.settings, clock=self.clock, secure=True)
        client = Client(app)
        response = client.request("POST", "/api/v1/session", body={"password": PASSWORD}, origin=f"https://{HOST}")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.header("Set-Cookie").startswith("__Host-cc_session="))
        self.assertIn("; Secure", response.header("Set-Cookie"))
        self.assertIsNotNone(response.header("Strict-Transport-Security"))
        self.assertEqual(client.request("POST", "/api/v1/session", body={"password": PASSWORD}).status, 403)

    def test_wrong_passphrase_is_rejected_and_never_logged(self):
        response = self.client.login("wrong passphrase value")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.json(), {"error": "invalid_credentials"})
        self.assertIsNone(response.header("Set-Cookie"))
        self.assertIn(("login", "failure"), self.audit_outcomes())
        self.assertNotIn("wrong passphrase", json.dumps(self.app.audit.recent(100)))

    def test_cross_site_and_malformed_logins_are_refused(self):
        cases = [
            ({"origin": False}, 403),
            ({"origin": "http://evil.example"}, 403),
            ({"headers": {"Sec-Fetch-Site": "cross-site"}}, 403),
            ({"content_type": "application/x-www-form-urlencoded", "raw": b"password=x"}, 415),
            ({"raw": b"{not json"}, 400),
            ({"body": {"password": PASSWORD, "admin": True}}, 400),
            ({"body": {"password": 123}}, 400),
            ({"raw": b"{" + b" " * 5000 + b"}"}, 413),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=str(kwargs)[:60]):
                kwargs.setdefault("body", None if "raw" in kwargs else {"password": PASSWORD})
                response = self.client.request("POST", "/api/v1/session", **kwargs)
                self.assertEqual(response.status, expected)
                self.assertIsNone(response.header("Set-Cookie"))

    def test_repeated_failures_lock_out_even_the_correct_passphrase_then_recover(self):
        for _ in range(5):
            self.assertEqual(self.client.login("wrong passphrase value").status, 401)
        locked = self.client.login()
        self.assertEqual(locked.status, 429)
        self.assertEqual(locked.header("Retry-After"), "900")
        self.assertIn(("login", "rate_limited"), self.audit_outcomes())
        self.clock.now += 15 * 60 + 1
        self.assertEqual(self.client.login().status, 200)

    def test_missing_configuration_fails_closed(self):
        app = make_app(make_settings(self.root, password_hash=None), clock=self.clock)
        response = Client(app).login()
        self.assertEqual(response.status, 503)
        self.assertEqual(response.json(), {"error": "authentication_not_configured"})


class SessionLifecycleTests(ApiTestCase):
    def test_logout_requires_csrf_token_and_revokes_session(self):
        self.client.login()
        self.assertEqual(self.client.request("DELETE", "/api/v1/session").status, 403)
        wrong = self.client.request("DELETE", "/api/v1/session", headers={"X-CSRF-Token": "x" * 43})
        self.assertEqual(wrong.status, 403)
        self.assertIn(("request", "csrf_rejected"), self.audit_outcomes())
        cookie = dict(self.client.cookies)
        response = self.client.request("DELETE", "/api/v1/session", headers={"X-CSRF-Token": self.client.csrf})
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", response.header("Set-Cookie"))
        self.client.cookies = cookie  # a replayed cookie must stay dead
        self.assertEqual(self.client.request("GET", "/api/v1/overview").status, 401)

    def test_cross_origin_state_change_is_refused_even_with_csrf_token(self):
        self.client.login()
        response = self.client.request(
            "DELETE", "/api/v1/session", headers={"X-CSRF-Token": self.client.csrf}, origin="http://evil.example"
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.client.request("GET", "/api/v1/session").status, 200)

    def test_idle_and_absolute_timeouts(self):
        self.client.login()
        self.clock.now += self.settings.session_idle_seconds + 1
        self.assertEqual(self.client.request("GET", "/api/v1/session").status, 401)
        self.client.login()
        for _ in range(30):
            self.clock.now += self.settings.session_idle_seconds - 60
            self.client.request("GET", "/api/v1/session")
        self.assertEqual(self.client.request("GET", "/api/v1/session").status, 401)

    def test_forged_and_malformed_cookies_are_ignored(self):
        for value in ("x" * 43, "short", "a;b", '"' + "x" * 43 + '"'):
            with self.subTest(value=value):
                self.client.cookies = {"cc_session": value}
                self.assertEqual(self.client.request("GET", "/api/v1/overview").status, 401)

    def test_api_rate_limit(self):
        self.app.api_limiter = RateLimiter(3, 60, clock=self.clock)
        self.client.login()
        statuses = [self.client.request("GET", "/api/v1/session").status for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 200, 429])


class PermissionTests(ApiTestCase):
    def add_recommend_route(self, app):
        app.routes[("POST", "/api/v1/test-draft")] = Route(
            lambda request: (200, {"drafted": True}, []), Permission.RECOMMEND, csrf=True
        )

    def test_read_ceiling_cannot_use_recommend_routes(self):
        app = make_app(make_settings(self.root, permission_ceiling=Permission.READ), clock=self.clock)
        self.add_recommend_route(app)
        client = Client(app)
        self.assertEqual(client.login().json()["permission"], "read")
        response = client.request("POST", "/api/v1/test-draft", headers={"X-CSRF-Token": client.csrf})
        self.assertEqual(response.status, 403)
        self.assertEqual(response.json(), {"error": "insufficient_permission"})
        self.assertIn(("request", "permission_denied"), [(e["action"], e["outcome"]) for e in app.audit.recent()])
        self.assertEqual(client.request("GET", "/api/v1/overview").status, 200)

    def test_recommend_ceiling_can_use_recommend_routes_with_csrf(self):
        self.add_recommend_route(self.app)
        self.client.login()
        self.assertEqual(self.client.request("POST", "/api/v1/test-draft").status, 403)
        response = self.client.request("POST", "/api/v1/test-draft", headers={"X-CSRF-Token": self.client.csrf})
        self.assertEqual(response.status, 200)

    def test_default_ceiling_is_recommend_not_execute(self):
        self.assertEqual(load_settings(self.root, {}).permission_ceiling, Permission.RECOMMEND)


class SecretExposureTests(ApiTestCase):
    def test_no_route_exposes_credentials_or_password_hash(self):
        (self.root / ".env").write_text(
            "ACCOUNT_NUMBER=A-SECRET-ACCOUNT\nAPI_KEY=sk_live_SECRET\nCOMMAND_CENTRE_PASSWORD_HASH='"
            + PASSWORD_HASH
            + "'\n",
            encoding="utf-8",
        )
        settings = load_settings(self.root, {})
        self.assertEqual(settings.password_hash, PASSWORD_HASH)
        self.assertFalse(hasattr(settings, "api_key"))
        seed_energy(settings.energy_db)
        seed_briefing(settings.briefing_db, [{"report_date": "2026-01-01", "errors": ["token=sk_live_SECRET"]}])
        client = Client(
            make_app(settings, clock=self.clock, cron=("available", "* * * * * run API_KEY=sk_live_SECRET\n"))
        )
        bodies = [client.login().body]
        for path in ("/", "/static/app.js", *READ_ROUTES):
            bodies.append(client.request("GET", path).body)
        blob = b"\n".join(bodies)
        for secret in (
            b"sk_live_SECRET",
            b"A-SECRET-ACCOUNT",
            PASSWORD_HASH.encode(),
            PASSWORD.encode(),
            b"MPAN-SECRET-POINT",
            b"METER-SECRET-SERIAL",
        ):
            self.assertNotIn(secret, blob)

    def test_briefing_limit_is_validated(self):
        self.client.login()
        for query in ("limit=0", "limit=101", "limit=abc", "limit=-1"):
            self.assertEqual(self.client.request("GET", "/api/v1/briefing/runs", query=query).status, 400)
        self.assertEqual(self.client.request("GET", "/api/v1/briefing/runs", query="limit=5").status, 200)


if __name__ == "__main__":
    unittest.main()
