import fcntl
import importlib.util
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from command_centre_fixtures import make_app, make_settings

from command_centre.security import Permission, hash_password

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/command-centre"
SHA = "a" * 40
DEPLOYMENT_ID = "d" * 32


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOST = load("deploy_host")
TRANSPORT = load("run_deploy")


def registry(app=None):
    applications = (
        {}
        if app is None
        else {app: {"domain": "iramkhan.com", "app_uri": "command-centre", "startup_file": "passenger_wsgi.py"}}
    )
    return {
        "result": "success",
        "selector_enabled": True,
        "available_versions": {"3.12": {"status": "enabled", "users": {"owner": {"applications": applications}}}},
    }


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password("a deployment test passphrase")
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for path in ("command_centre", "octopus_energy", "deploy/command-centre/passenger_wsgi.py"):
                archive.add(ROOT / path, arcname=path, filter=lambda info: None if "__pycache__" in info.name else info)
        cls.archive = output.getvalue()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.checkout = self.home / "personal-ai"
        (self.checkout / ".git").mkdir(parents=True)
        (self.checkout / "data").mkdir()
        self.energy = self.checkout / "data/octopus_energy.sqlite3"
        self.energy.write_bytes(b"owner database must remain untouched")
        self.app = self.home / "command-centre-app"
        self.current = SHA
        self.original_run = HOST.run
        self.commands = []
        self.original_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.original_umask)
        self.run = self.enterContext(patch.object(HOST, "run", side_effect=self.command))
        self.selector = self.enterContext(patch.object(HOST, "selector", side_effect=self.selection))
        self.verify = self.enterContext(patch.object(HOST, "verify", side_effect=self.verification))
        self.vendor = self.enterContext(patch.object(HOST, "vendor", return_value=True))
        self.registered = False

    def locked(self):
        with (self.checkout / ".git/energy-dashboard.deploy.lock").open("r") as contender:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def command(self, args, **kwargs):
        self.commands.append(args)
        self.locked()
        if args[:2] == ["git", "fetch"]:
            return b""
        if args[:2] == ["git", "rev-parse"]:
            return self.current.encode() + b"\n"
        if args[:2] == ["git", "archive"]:
            return self.archive
        return self.original_run(args, **kwargs)

    def selection(self, action, *args):
        self.locked()
        if action == "get":
            return registry("command-centre-app" if self.registered else None)
        if action in ("create", "start"):
            mount = self.home / "public_html/command-centre"
            mount.mkdir(parents=True, exist_ok=True)
            (mount / ".htaccess").write_text(
                f'PassengerAppRoot "{self.app}"\nPassengerBaseURI "/command-centre"\nPassengerPython "/private/python"\n'
            )
        return {"result": "success"}

    def verification(self, app, sha, deployment_id, *, revoke_sessions=False):
        self.locked()
        self.assertEqual(app, self.app)
        self.assertIn(f"COMMAND_CENTRE_RELEASE_SHA={sha}", (app / "command-centre.env").read_text())
        self.assertRegex(deployment_id, r"^[a-f0-9]{32}$")
        self.assertIn(f"COMMAND_CENTRE_DEPLOYMENT_ID={deployment_id}", (app / "command-centre.env").read_text())
        self.assertEqual(stat.S_IMODE((app / "command-centre.env").stat().st_mode), 0o600)
        self.assertTrue((app / "tmp/restart.txt").exists())
        return {"authenticated_read_smoke": True, "existing_sessions_revoked": revoke_sessions}

    def install(self, sha=SHA):
        return HOST.install(self.checkout, sha, self.password_hash, home=self.home)

    def test_isolated_release_holds_shared_lock_and_preserves_source(self):
        result = self.install()
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(self.energy.read_bytes(), b"owner database must remain untouched")
        self.assertEqual(stat.S_IMODE(self.app.stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE((self.app / "tmp").stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE((self.app / "data").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.app / "releases").stat().st_mode), 0o700)
        self.assertTrue((self.app / "releases" / SHA / ".complete").is_file())
        self.assertTrue((self.app / "data/sessions.sqlite3").is_file())
        config = (self.app / "command-centre.env").read_text()
        self.assertIn(f"COMMAND_CENTRE_ENERGY_DB={self.energy}", config)
        self.assertIn(f"COMMAND_CENTRE_CHECKOUT={self.app}/releases/{SHA}", config)
        self.assertFalse(any("merge" in args or "checkout" in args or "reset" in args for args in self.commands))
        self.assertEqual(self.selector.call_args_list[1].args[0], "create")
        mapping = self.home / "public_html/command-centre/.htaccess"
        self.assertIn("PassengerAppType wsgi", mapping.read_text())
        self.assertIn("PassengerStartupFile passenger_wsgi.py", mapping.read_text())
        self.assertEqual(stat.S_IMODE(mapping.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(mapping.parent.stat().st_mode), 0o755)
        with (self.checkout / ".git/energy-dashboard.deploy.lock").open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_stale_run_cannot_write_app_env_entrypoint_or_restart(self):
        self.app.mkdir()
        sentinel = self.app / "command-centre.env"
        sentinel.write_bytes(b"keep this existing configuration")
        self.current = "b" * 40
        self.assertEqual(self.install()["status"], "skipped_stale")
        self.assertEqual(list(self.app.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"keep this existing configuration")
        self.selector.assert_not_called()
        self.verify.assert_not_called()
        self.assertFalse(any("archive" in args for args in self.commands))

    def test_failed_replacement_restores_previous_config_and_entrypoint(self):
        self.install()
        original_env = (self.app / "command-centre.env").read_bytes()
        original_entry = (self.app / "passenger_wsgi.py").read_bytes()
        self.registered = True
        self.current = "b" * 40
        self.verify.side_effect = HOST.ReleaseError("synthetic smoke failure")
        with self.assertRaises(HOST.ReleaseError):
            self.install(self.current)
        self.assertEqual((self.app / "command-centre.env").read_bytes(), original_env)
        self.assertEqual((self.app / "passenger_wsgi.py").read_bytes(), original_entry)
        self.assertEqual(stat.S_IMODE((self.app / "command-centre.env.previous").stat().st_mode), 0o600)
        self.assertNotIn("stop", [call.args[0] for call in self.selector.call_args_list])

    def test_new_registration_is_stopped_when_smoke_fails(self):
        self.verify.side_effect = HOST.ReleaseError("synthetic smoke failure")
        with self.assertRaises(HOST.ReleaseError):
            self.install()
        self.assertEqual(self.selector.call_args.args, ("stop", "--app-root", "command-centre-app"))

    def test_repeated_release_does_not_rebuild_immutable_code(self):
        self.install()
        self.registered = True
        self.commands.clear()
        self.install()
        self.assertFalse(any("archive" in args for args in self.commands))
        mapping = (self.home / "public_html/command-centre/.htaccess").read_text()
        self.assertEqual(mapping.count("PassengerAppType"), 1)
        self.assertEqual(mapping.count("# Command Centre HTTPS BEGIN"), 1)
        self.assertIn("RewriteCond %{HTTPS} !=on", mapping)
        self.assertIn("RewriteRule ^ https://iramkhan.com%{REQUEST_URI} [R=308,L,NE]", mapping)
        self.assertNotIn("HTTP:X-Forwarded-Proto", mapping)

    def test_password_reset_on_same_release_changes_identity_and_requests_revocation(self):
        self.install()
        first_id = self.verify.call_args.args[2]
        self.registered = True
        new_hash = hash_password("a new deployment test passphrase")
        result = HOST.install(self.checkout, SHA, new_hash, home=self.home)
        self.assertNotEqual(self.verify.call_args.args[2], first_id)
        self.assertTrue(self.verify.call_args.kwargs["revoke_sessions"])
        self.assertTrue(result["existing_sessions_revoked"])
        self.assertEqual(result["release"], SHA)
        self.assertEqual(self.energy.read_bytes(), b"owner database must remain untouched")

    def test_same_password_redeployment_keeps_sessions_but_changes_deployment_identity(self):
        self.install()
        first_id = self.verify.call_args.args[2]
        self.registered = True
        result = self.install()
        self.assertNotEqual(self.verify.call_args.args[2], first_id)
        self.assertFalse(self.verify.call_args.kwargs["revoke_sessions"])
        self.assertFalse(result["existing_sessions_revoked"])

    def test_failed_reset_restores_previous_password_configuration(self):
        self.install()
        previous = (self.app / "command-centre.env").read_bytes()
        self.registered = True
        self.verify.side_effect = HOST.ReleaseError("synthetic reset verification failure")
        with self.assertRaises(HOST.ReleaseError):
            HOST.install(self.checkout, SHA, hash_password("a failed reset test passphrase"), home=self.home)
        self.assertEqual((self.app / "command-centre.env").read_bytes(), previous)

    def test_other_app_mapping_is_not_overwritten(self):
        self.install()
        mapping = self.home / "public_html/command-centre/.htaccess"
        mapping.write_text('PassengerAppRoot "/another/app"\nPassengerBaseURI "/command-centre"\n')
        original = mapping.read_bytes()
        with self.assertRaises(HOST.ReleaseError):
            HOST.configure_wsgi_mapping(self.app, self.home)
        self.assertEqual(mapping.read_bytes(), original)

    def test_existing_private_public_mount_is_repaired_without_exposing_app(self):
        self.install()
        mount = self.home / "public_html/command-centre"
        mount.chmod(0o700)
        (mount / ".htaccess").chmod(0o600)
        HOST.configure_wsgi_mapping(self.app, self.home)
        self.assertEqual(stat.S_IMODE(mount.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((mount / ".htaccess").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.app.stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE((self.app / "command-centre.env").stat().st_mode), 0o600)

    def test_redeploy_keeps_previous_distinct_verified_release_for_rollback(self):
        self.install()
        first_config = (self.app / "command-centre.env").read_bytes()
        self.registered = True
        self.current = "b" * 40
        self.install(self.current)
        self.install(self.current)
        self.assertEqual((self.app / "command-centre.env.previous").read_bytes(), first_config)
        self.assertEqual((self.app / ".verified-release").read_text(), self.current)

    def test_retry_starts_an_app_stopped_by_a_failed_first_release(self):
        self.install()
        config = registry("command-centre-app")
        details = config["available_versions"]["3.12"]["users"]["owner"]["applications"]["command-centre-app"]
        details["app_status"] = "stopped"
        self.selector.side_effect = lambda action, *args: config if action == "get" else {"result": "success"}
        self.install()
        self.assertIn(
            ("start", "--app-root", "command-centre-app"), [call.args for call in self.selector.call_args_list]
        )

    def test_unmanaged_directory_is_refused(self):
        self.app.mkdir()
        (self.app / "owner-file").write_text("do not replace")
        with self.assertRaises(HOST.ReleaseError):
            self.install()
        self.assertFalse((self.app / ".command-centre-managed").exists())

    def test_symlinked_app_is_refused(self):
        target = self.home / "owner-directory"
        target.mkdir()
        self.app.symlink_to(target, target_is_directory=True)
        with self.assertRaises(HOST.ReleaseError):
            self.install()
        self.assertEqual(list(target.iterdir()), [])

    def test_conflicting_url_does_not_modify_app_or_registration(self):
        self.selector.side_effect = lambda *args: registry("another-app")
        with self.assertRaises(HOST.ReleaseError):
            self.install()
        self.assertFalse(self.app.exists())
        self.assertEqual(self.selector.call_count, 1)

    def test_bad_hash_is_rejected_before_host_mutation(self):
        with self.assertRaises(HOST.ReleaseError):
            HOST.install(self.checkout, SHA, "not-a-hash", home=self.home)
        self.assertFalse(self.app.exists())
        self.run.assert_not_called()


class SelectorUmaskTests(unittest.TestCase):
    def test_selector_creates_web_mapping_with_public_read_permissions(self):
        with patch.object(HOST.shutil, "which", return_value="/bin/selector"), patch.object(HOST, "run") as run:
            run.return_value = b'{"result":"success"}'
            HOST.selector("create")
        self.assertEqual(run.call_args.kwargs["umask"], 0o022)


class TransportTests(unittest.TestCase):
    def test_secret_is_only_on_stdin_not_command_line(self):
        env = {
            "CPANEL_APP_PATH": "/home/owner/personal-ai",
            "DEPLOY_SHA": SHA,
            "CPANEL_SSH_HOST": "hosting.example",
            "CPANEL_SSH_USERNAME": "owner",
            "COMMAND_CENTRE_PASSWORD_HASH": "private-test-sentinel",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(TRANSPORT.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(TRANSPORT.main(), 0)
        argv = run.call_args.args[0]
        self.assertNotIn("private-test-sentinel", " ".join(argv))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["password_hash"], "private-test-sentinel")
        remote = shlex.split(argv[-1])
        self.assertEqual(remote[-2:], [env["CPANEL_APP_PATH"], SHA])
        self.assertEqual(remote[2], (DEPLOY / "deploy_host.py").read_text())


class WorkflowGateTests(unittest.TestCase):
    def gate(self, current, successes):
        workflow = (ROOT / ".github/workflows/deploy-command-centre.yml").read_text()
        script = textwrap.dedent(workflow.split("run: |\n", 1)[1].split("      - name:", 1)[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gh = root / "gh"
            gh.write_text(
                f"#!{sys.executable}\nimport sys\n"
                f"print({current!r} if '/branches/main' in sys.argv[2] else {str(successes)!r})\n"
            )
            gh.chmod(0o700)
            output = root / "output"
            env = {
                **os.environ,
                "PATH": f"{root}:" + os.environ["PATH"],
                "DEPLOY_SHA": SHA,
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_OUTPUT": str(output),
            }
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            return result, output.read_text() if output.exists() else ""

    def test_manual_release_requires_successful_main_push_tests(self):
        result, output = self.gate(SHA, 0)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ready=true", output)
        result, output = self.gate(SHA, 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ready=true", output)

    def test_stale_workflow_gates_all_remote_work(self):
        result, output = self.gate("b" * 40, 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ready=false", output)
        workflow = (ROOT / ".github/workflows/deploy-command-centre.yml").read_text()
        self.assertIn("if: steps.tested.outputs.ready == 'true'", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)


class SmokeTests(unittest.TestCase):
    def test_failed_authenticated_check_still_revokes_smoke_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = make_app(make_settings(Path(directory)))
            response_headers = {
                "X-Command-Centre-Release": SHA,
                "X-Command-Centre-Deployment": DEPLOYMENT_ID,
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'",
                "Strict-Transport-Security": "max-age=31536000",
            }
            responses = [
                (200, response_headers, b'{"status":"ok"}'),
                *[(200, {}, b"<title>Jarvis api/v1/ {")] * 3,
                *[(401, {}, b"{}")] * 5,
                (308, {"Location": "https://iramkhan.com/command-centre/api/v1/health"}, b""),
                (308, {"Location": "https://iramkhan.com/command-centre/api/v1/health"}, b""),
                (500, {}, b"{}"),
            ]
            with (
                patch.object(HOST, "fetch", side_effect=responses),
                patch.object(HOST.runpy, "run_path", return_value={"app": app}),
                self.assertRaises(HOST.ReleaseError),
            ):
                HOST.verify(Path(directory), SHA, DEPLOYMENT_ID)
            self.assertEqual(app.sessions._sessions, {})

    def test_public_and_authenticated_paths_use_revoked_read_only_session(self):
        self.smoke({"state": "available", "daily": [{}]})

    def test_reset_revokes_existing_sessions_and_verifies_live_cookie_rejection(self):
        result = self.smoke({"state": "available", "daily": [{}]}, revoke_sessions=True)
        self.assertTrue(result["existing_sessions_revoked"])

    def test_same_release_stale_configuration_is_not_accepted(self):
        self.smoke({"state": "available", "daily": [{}]}, stale_deployment=True)

    def test_missing_deployment_identity_never_passes_health(self):
        with tempfile.TemporaryDirectory() as directory:
            headers = {"X-Command-Centre-Release": SHA}
            with (
                patch.object(HOST, "fetch", return_value=(200, headers, b'{"status":"ok"}')) as fetch,
                patch.object(HOST.time, "sleep"),
                patch.object(HOST.runpy, "run_path") as load,
                self.assertRaises(HOST.ReleaseError),
            ):
                HOST.verify(Path(directory), SHA, DEPLOYMENT_ID, revoke_sessions=True)
            self.assertEqual(fetch.call_count, 24)
            load.assert_not_called()

    def test_unreadable_or_empty_source_cannot_pass_release_validation(self):
        for state in ("not_configured", "unavailable", "available", "refresh_required"):
            with self.subTest(state=state), self.assertRaises(HOST.ReleaseError):
                self.smoke({"state": state, "daily": []})

    def test_readable_legacy_source_is_reported_without_migration(self):
        result = self.smoke({"state": "refresh_required", "daily": [], "sync_history": [{"status": "success"}]})
        self.assertEqual(result["energy_state"], "refresh_required")

    def smoke(self, energy, *, revoke_sessions=False, stale_deployment=False):
        with tempfile.TemporaryDirectory() as directory:
            app = make_app(make_settings(Path(directory)))
            observed = []
            existing_token, _ = app.sessions.create(Permission.READ)
            health_calls = 0

            def fetch(url, *, headers=None):
                nonlocal health_calls
                headers = headers or {}
                if url.startswith("http:"):
                    self.assertIn(headers.get("X-Forwarded-Proto"), (None, "https"))
                    return 308, {"Location": url.replace("http:", "https:")}, b""
                if url.endswith("/health"):
                    health_calls += 1
                    return (
                        200,
                        {
                            "X-Command-Centre-Release": SHA,
                            "X-Command-Centre-Deployment": (
                                "0" * 32 if stale_deployment and health_calls == 1 else DEPLOYMENT_ID
                            ),
                            "X-Content-Type-Options": "nosniff",
                            "Content-Security-Policy": "default-src 'self'",
                            "Strict-Transport-Security": "max-age=31536000",
                        },
                        b'{"status":"ok"}',
                    )
                if "/api/" in url:
                    if not headers.get("Cookie"):
                        return 401, {}, b"{}"
                    token = headers["Cookie"].split("=", 1)[1]
                    session = app.sessions.get(token)
                    if session is None:
                        return 401, {}, b"{}"
                    self.assertEqual(session.permission.label, "read")
                    observed.append(token)
                    return 200, {}, json.dumps(energy).encode()
                return 200, {}, b"<title>Jarvis api/v1/ {"

            with (
                patch.object(HOST, "fetch", side_effect=fetch),
                patch.object(HOST.runpy, "run_path", return_value={"app": app}),
                patch.object(HOST.time, "sleep") as sleep,
            ):
                result = HOST.verify(Path(directory), SHA, DEPLOYMENT_ID, revoke_sessions=revoke_sessions)
            self.assertTrue(result["authenticated_read_smoke"])
            self.assertFalse(result["owner_passphrase_sign_in_tested"])
            self.assertEqual(len(observed), 5)
            self.assertIsNone(app.sessions.get(observed[0]))
            self.assertEqual(app.sessions.get(existing_token) is None, revoke_sessions)
            self.assertEqual(health_calls, 2 if stale_deployment else 1)
            if stale_deployment:
                sleep.assert_called_once_with(3)
            return result


class VendorTests(unittest.TestCase):
    def setUp(self):
        import base64
        import hashlib
        import io
        import zipfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.release = Path(self.tmp.name)
        (self.release / "deploy/command-centre").mkdir(parents=True)

        def wheel(files):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, body in files.items():
                    archive.writestr(name, body)
            return buffer.getvalue()

        self.good = wheel({"fakepkg/__init__.py": "VALUE = 1\n"})
        self.evil = wheel({"../escape.py": "x"})
        hashes = " ".join(f"--hash=sha256:{hashlib.sha256(w).hexdigest()}" for w in (self.good, self.evil))
        (self.release / "deploy/command-centre/requirements.txt").write_text(f"fakepkg==1 {hashes}\n")
        self.encode = lambda data: base64.b64encode(data).decode()
        self.pip = self.enterContext(patch.object(HOST.subprocess, "run", side_effect=OSError("no pip")))

    def test_pinned_wheels_are_unpacked_without_pip(self):
        self.assertTrue(HOST.vendor(self.release, {"fakepkg-1-py3-none-any.whl": self.encode(self.good)}))
        self.assertTrue((self.release / "vendor/fakepkg/__init__.py").is_file())
        self.pip.assert_not_called()

    def test_unpinned_or_unsafe_wheels_fall_back_and_leave_no_vendor_dir(self):
        for wheels in (
            {"other-1-py3-none-any.whl": self.encode(b"PK not pinned")},
            {"evil-1-py3-none-any.whl": self.encode(self.evil)},
            {"../x.whl": self.encode(self.good)},
        ):
            with self.subTest(wheels=list(wheels)):
                self.assertFalse(HOST.vendor(self.release, wheels))
                self.assertFalse((self.release / "vendor").exists())
                self.assertFalse((self.release.parent / "escape.py").exists())
