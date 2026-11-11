import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from command_centre_fixtures import PASSWORD, PASSWORD_HASH, ROOT


class ServerSmokeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.env = {
            **os.environ,
            "COMMAND_CENTRE_PASSWORD_HASH": PASSWORD_HASH,
            "COMMAND_CENTRE_ENERGY_DB": str(root / "energy.sqlite3"),
            "COMMAND_CENTRE_BRIEFING_DB": str(root / "briefing.sqlite3"),
            "COMMAND_CENTRE_BRIEFING_REPORTS": str(root / "daily"),
            "COMMAND_CENTRE_ENERGY_STATUS": str(root / "status.json"),
            "COMMAND_CENTRE_AUDIT_DB": str(root / "audit.sqlite3"),
            "COMMAND_CENTRE_INSPECT_CRONTAB": "0",
        }

    def run_cli(self, *args, input_text=None, env=None):
        return subprocess.run(
            [sys.executable, "-m", "command_centre", *args],
            cwd=ROOT,
            env=env or self.env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_real_server_login_overview_logout(self):
        process = subprocess.Popen(
            [sys.executable, "-m", "command_centre", "serve", "--port", "0"],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.terminate)
        line = process.stdout.readline()
        self.assertIn("listening on http://127.0.0.1:", line)
        port = int(line.split(":")[2].split("/")[0])
        host = f"127.0.0.1:{port}"

        def request(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            payload = json.dumps(body) if body is not None else None
            base = {"Host": host, "Origin": f"http://{host}"}
            if payload:
                base["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers={**base, **(headers or {})})
            response = conn.getresponse()
            data = response.read()
            conn.close()
            return response, data

        response, _ = request("GET", "/api/v1/health")
        self.assertEqual(response.status, 200)
        self.assertNotIn("CPython", response.getheader("Server") or "")
        self.assertEqual(request("GET", "/api/v1/overview")[0].status, 401)
        self.assertEqual(request("GET", "/api/v1/health", headers={"Host": "attacker.example"})[0].status, 421)
        response, data = request("POST", "/api/v1/session", {"password": PASSWORD})
        self.assertEqual(response.status, 200)
        cookie = response.getheader("Set-Cookie").split(";")[0]
        csrf = json.loads(data)["csrf_token"]
        response, data = request("GET", "/api/v1/overview", headers={"Cookie": cookie})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["timezone"], "Europe/London")
        page, html = request("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(page.status, 200)
        self.assertIn(b"Command Centre", html)
        response, _ = request("DELETE", "/api/v1/session", headers={"Cookie": cookie, "X-CSRF-Token": csrf})
        self.assertEqual(response.status, 200)
        self.assertEqual(request("GET", "/api/v1/overview", headers={"Cookie": cookie})[0].status, 401)
        process.terminate()
        _, stderr = process.communicate(timeout=10)
        self.assertNotIn(PASSWORD, stderr)
        self.assertNotIn(cookie.split("=", 1)[1], stderr)
        self.assertNotIn("Traceback", stderr)

    def test_refuses_unsafe_or_unconfigured_start(self):
        result = self.run_cli("serve", "--host", "0.0.0.0", "--port", "0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("non-loopback", result.stderr)
        # An empty value, not a deleted key: a developer's own .env must not make the CLI "configured".
        env = {**self.env, "COMMAND_CENTRE_PASSWORD_HASH": ""}
        result = self.run_cli("serve", "--port", "0", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("COMMAND_CENTRE_PASSWORD_HASH", result.stderr)

    def test_hash_password_outputs_a_verifiable_env_line(self):
        from command_centre.security import verify_password

        result = self.run_cli("hash-password", "--stdin", input_text="a long enough passphrase\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        line = result.stdout.strip()
        self.assertTrue(line.startswith("COMMAND_CENTRE_PASSWORD_HASH='pbkdf2_sha256$600000$"))
        self.assertTrue(verify_password("a long enough passphrase", line.split("=", 1)[1].strip("'")))
        self.assertNotIn("a long enough passphrase", result.stdout)
        self.assertEqual(self.run_cli("hash-password", "--stdin", input_text="short\n").returncode, 1)


class FrontendTests(unittest.TestCase):
    def test_frontend_never_injects_html(self):
        script = (ROOT / "command_centre/static/app.js").read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, script)
        page = (ROOT / "command_centre/static/index.html").read_text(encoding="utf-8")
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript syntax checks")
    def test_frontend_script_parses(self):
        result = subprocess.run(
            ["node", "--check", str(ROOT / "command_centre/static/app.js")], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
