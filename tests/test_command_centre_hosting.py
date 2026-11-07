import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from command_centre_fixtures import HOST, PASSWORD_HASH, Client, Clock, make_app, make_settings

from command_centre.config import load_settings
from command_centre.security import Permission, SqliteSessionStore

ROOT = Path(__file__).resolve().parents[1]


class HostingSettingsTests(unittest.TestCase):
    def test_allowed_hosts_and_sessions_db_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(
                Path(directory),
                {
                    "COMMAND_CENTRE_ALLOWED_HOSTS": "iramkhan.com, www.iramkhan.com ",
                    "COMMAND_CENTRE_SESSIONS_DB": "state/sessions.sqlite3",
                },
            )
            self.assertEqual(settings.allowed_hosts, ("iramkhan.com", "www.iramkhan.com"))
            self.assertEqual(settings.sessions_db, Path(directory) / "state/sessions.sqlite3")

    def test_hosting_settings_default_off(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(Path(directory), {})
            self.assertEqual(settings.allowed_hosts, ())
            self.assertIsNone(settings.sessions_db)


class HttpsRedirectTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = make_settings(Path(self.directory.name))
        self.app = make_app(self.settings, secure=True)

    def test_plain_http_request_redirects_to_https(self):
        client = Client(self.app)
        response = client.request("GET", "/api/v1/overview", query="limit=1", scheme="http")
        self.assertEqual(response.status, 308)
        self.assertEqual(response.header("Location"), f"https://{HOST}/api/v1/overview?limit=1")
        self.assertEqual(response.json(), {"error": "https_required"})

    def test_https_request_is_not_redirected(self):
        client = Client(self.app)
        self.assertEqual(client.request("GET", "/api/v1/health", scheme="https").status, 200)

    def test_forwarded_proto_https_is_not_redirected(self):
        # Guard against a redirect loop if the proxy only signals TLS via the header.
        client = Client(self.app)
        response = client.request("GET", "/api/v1/health", scheme="http", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.status, 200)

    def test_redirect_preserves_script_name_for_mounted_apps(self):
        client = Client(self.app)
        response = client.request("GET", "/", scheme="http", script_name="/command-centre")
        self.assertEqual(response.header("Location"), f"https://{HOST}/command-centre/")

    def test_insecure_app_never_redirects(self):
        app = make_app(self.settings)
        self.assertEqual(Client(app).request("GET", "/api/v1/health", scheme="http").status, 200)


class SqliteSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sessions.sqlite3"
        self.clock = Clock(start=1_000_000.0)
        self.store = SqliteSessionStore(self.path, idle_seconds=30, absolute_seconds=120, clock=self.clock)

    def test_file_is_owner_only(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_create_get_revoke_round_trip(self):
        token, session = self.store.create(Permission.RECOMMEND)
        self.assertEqual(self.store.get(token).csrf_token, session.csrf_token)
        self.assertEqual(self.store.get(token).permission, Permission.RECOMMEND)
        self.store.revoke(token)
        self.assertIsNone(self.store.get(token))

    def test_sessions_survive_a_new_store_instance(self):
        token, session = self.store.create(Permission.READ)
        reopened = SqliteSessionStore(self.path, idle_seconds=30, absolute_seconds=120, clock=self.clock)
        self.assertEqual(reopened.get(token).csrf_token, session.csrf_token)

    def test_idle_and_absolute_expiry(self):
        token, _ = self.store.create(Permission.READ)
        self.clock.now += 31
        self.assertIsNone(self.store.get(token))  # idle expiry
        token, _ = self.store.create(Permission.READ)
        self.clock.now += 121
        self.assertIsNone(self.store.get(token))  # absolute expiry

    def test_max_sessions_evicts_least_recently_used(self):
        store = SqliteSessionStore(self.path, idle_seconds=300, absolute_seconds=3600, clock=self.clock, max_sessions=2)
        first, _ = store.create(Permission.READ)
        store.create(Permission.READ)
        self.clock.now += 1
        store.get(first)  # make the first token the most recently used
        store.create(Permission.READ)  # evicts the middle token, not the first
        self.assertIsNotNone(store.get(first))

    def test_symlink_is_refused(self):
        link = Path(self.directory.name) / "linked.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            SqliteSessionStore(link, idle_seconds=30, absolute_seconds=120, clock=self.clock)


class HostedSessionTests(unittest.TestCase):
    def test_session_is_shared_across_app_instances(self):
        # Passenger can run several processes; each request may hit a different one.
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory), sessions_db=Path(directory) / "sessions.sqlite3")
            first = Client(make_app(settings))
            self.assertEqual(first.login().status, 200)
            second = Client(make_app(settings), cookies=dict(first.cookies))
            self.assertEqual(second.request("GET", "/api/v1/session").status, 200)
            first.request("DELETE", "/api/v1/session", headers={"X-CSRF-Token": first.csrf})
            self.assertEqual(second.request("GET", "/api/v1/session").status, 401)

    def test_default_sessions_stay_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory))
            first = Client(make_app(settings))
            self.assertEqual(first.login().status, 200)
            second = Client(make_app(settings), cookies=dict(first.cookies))
            self.assertEqual(second.request("GET", "/api/v1/session").status, 401)


class PassengerEntryPointTests(unittest.TestCase):
    def test_passenger_wsgi_serves_the_app(self):
        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory)
            (app_dir / "command-centre.env").write_text(
                "\n".join(
                    [
                        f"COMMAND_CENTRE_PASSWORD_HASH='{PASSWORD_HASH}'",
                        f"COMMAND_CENTRE_CHECKOUT={ROOT}",
                        "COMMAND_CENTRE_ALLOWED_HOSTS=iramkhan.com",
                        f"COMMAND_CENTRE_AUDIT_DB={app_dir}/data/audit.sqlite3",
                        f"COMMAND_CENTRE_SESSIONS_DB={app_dir}/data/sessions.sqlite3",
                        f"COMMAND_CENTRE_ENERGY_DB={app_dir}/missing-energy.sqlite3",
                        f"COMMAND_CENTRE_BRIEFING_DB={app_dir}/missing-briefing.sqlite3",
                        "OCTOPUS_ENERGY_API_KEY=must-not-be-read",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            # The deploy copies passenger_wsgi.py into the app dir; do the same so
            # it resolves command-centre.env beside itself.
            shutil.copy(ROOT / "deploy/command-centre/passenger_wsgi.py", app_dir / "passenger_wsgi.py")
            script = "\n".join(
                [
                    "import io, os, runpy",
                    f"ns = runpy.run_path({str(app_dir / 'passenger_wsgi.py')!r})",
                    "captured = {}",
                    "def start(s, h): captured.update(status=int(s.split()[0]), headers=h)",
                    "environ = {'REQUEST_METHOD': 'GET', 'PATH_INFO': '/api/v1/health', 'SCRIPT_NAME': '/command-centre',",
                    "           'HTTP_HOST': 'iramkhan.com', 'wsgi.url_scheme': 'https', 'QUERY_STRING': '',",
                    "           'wsgi.input': io.BytesIO(b'')}",
                    "body = b''.join(ns['application'](environ, start))",
                    "print(captured['status'], body.decode())",
                    "environ['HTTP_HOST'] = 'evil.example'",
                    "b''.join(ns['application'](environ, start))",
                    "print('evil-host:', captured['status'])",
                    "print('secret-leak:', 'OCTOPUS_ENERGY_API_KEY' in os.environ)",
                ]
            )
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=app_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('200 {"status":"ok"}', result.stdout)
            self.assertIn("evil-host: 421", result.stdout)
            self.assertIn("secret-leak: False", result.stdout)
            self.assertTrue((app_dir / "data" / "audit.sqlite3").exists())
            self.assertTrue((app_dir / "data" / "sessions.sqlite3").exists())


class StaticAssetTests(unittest.TestCase):
    def test_no_absolute_urls_so_subpath_hosting_works(self):
        html = (ROOT / "command_centre/static/index.html").read_text(encoding="utf-8")
        script = (ROOT / "command_centre/static/app.js").read_text(encoding="utf-8")
        self.assertNotIn('href="/', html)
        self.assertNotIn('src="/', html)
        self.assertNotIn('"/api/', script)
        self.assertNotIn('fetch("/', script)


if __name__ == "__main__":
    unittest.main()
