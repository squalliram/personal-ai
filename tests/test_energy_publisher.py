import copy
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from octopus_energy import publisher
from octopus_energy.refresh import ERROR_CATEGORIES

ROOT = Path(__file__).resolve().parents[1]


def status(**changes):
    value = {
        "schema_version": 1,
        "last_attempt_at": "2026-11-05T12:00:00Z",
        "last_success_at": "2026-11-05T12:00:00Z",
        "result": "success",
        "latest_reading_at": {"electricity": "2026-11-04T23:30:00Z", "gas": None},
        "request_state": None,
        "request_id": None,
        "error_category": None,
    }
    value.update(changes)
    return json.dumps(value).encode()


class PublishDashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.app = self.root / "application with spaces and café"
        self.private = self.app / "data/energy-refresh"
        self.private.mkdir(parents=True, mode=0o700)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "index.html").write_text("<html>synthetic dashboard</html>")
        (self.source / "data.json").write_text('{"daily":[],"hourly":[]}')
        (self.source / "status.json").write_bytes(status(request_id="0123456789abcdef" * 2))
        (self.source / "refresh.php").write_text(
            "<?php\nconst ENERGY_REFRESH_DIRECTORY_HEX = '__ENERGY_REFRESH_DIRECTORY_HEX__';\n"
        )
        (self.source / ".env").write_text("synthetic excluded file")
        self.target = self.root / "protected"
        self.password_file = self.root / "passwd"
        self.password_file.write_text("synthetic test fixture")

    def publish(self, *args):
        env = dict(
            os.environ,
            PYTHON=sys.executable,
            PYTHONPATH=str(ROOT),
            DASHBOARD_DIR=str(self.source),
            DASHBOARD_TARGET=str(self.target),
        )
        return subprocess.run(
            [str(ROOT / "scripts/publish-energy-dashboard.sh"), *args],
            cwd=self.app,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def protection(self, extra=""):
        self.target.mkdir(exist_ok=True)
        (self.target / ".htaccess").write_text(
            f'AuthType Basic\nAuthName "Private energy"\nAuthUserFile "{self.password_file}"\nRequire valid-user\n{extra}'
        )

    def previous_files(self, names=publisher.FILES):
        for name in names:
            (self.target / name).write_text("previous synthetic " + name)
            (self.target / name).chmod(0o640)
        return self.snapshot()

    def snapshot(self):
        return {
            p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
            for p in self.target.iterdir()
            if p.is_file()
        }

    def test_refuses_to_create_unprotected_target(self):
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())

    def test_refuses_existing_target_without_protection_and_preserves_files(self):
        self.target.mkdir()
        (self.target / "index.html").write_text("existing dashboard")
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.target / "index.html").read_text(), "existing dashboard")
        self.assertFalse((self.target / "data.json").exists())

    def test_refuses_authorization_bypass_or_conditional_configuration(self):
        for extra in ("Require all granted\n", "Satisfy any\n", "Allow from all\n"):
            with self.subTest(extra=extra):
                self.protection(extra)
                self.assertNotEqual(self.publish().returncode, 0)
                self.assertFalse((self.target / "data.json").exists())
        self.protection()
        path = self.target / ".htaccess"
        path.write_text("<IfModule mod_auth_basic.c>\n" + path.read_text() + "</IfModule>\n")
        self.assertNotEqual(self.publish().returncode, 0)

    def test_requires_existing_password_file(self):
        self.protection()
        self.password_file.unlink()
        self.assertNotEqual(self.publish().returncode, 0)

    def test_publishes_only_four_files_and_preserves_protection(self):
        self.protection()
        protection = (self.target / ".htaccess").read_bytes()
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / ".htaccess").read_bytes(), protection)
        self.assertEqual((self.target / "data.json").read_bytes(), (self.source / "data.json").read_bytes())
        self.assertEqual((self.target / "status.json").read_bytes(), (self.source / "status.json").read_bytes())
        self.assertEqual({p.name for p in self.target.iterdir()}, {".htaccess", *publisher.FILES})
        php = (self.target / "refresh.php").read_text()
        self.assertIn(str(self.private).encode("utf-8").hex(), php)
        self.assertNotIn(publisher.PLACEHOLDER, php)
        self.assertNotIn(str(self.private), php)
        self.assertFalse((self.target / ".env").exists())
        for name in publisher.FILES:
            self.assertEqual(stat.S_IMODE((self.target / name).stat().st_mode), 0o644)

    def test_missing_source_file_does_not_replace_existing_data(self):
        self.protection()
        (self.target / "data.json").write_text("previous")
        (self.source / "index.html").unlink()
        self.assertNotEqual(self.publish().returncode, 0)
        self.assertEqual((self.target / "data.json").read_text(), "previous")

    def test_every_missing_source_fails_before_any_destination_change(self):
        self.protection()
        before = self.previous_files()
        for name in publisher.FILES:
            with self.subTest(name=name):
                path = self.source / name
                body = path.read_bytes()
                path.unlink()
                self.assertNotEqual(self.publish().returncode, 0)
                self.assertEqual(self.snapshot(), before)
                path.write_bytes(body)

    def test_invalid_request_id_cannot_change_full_or_status_only_publication(self):
        self.protection()
        before = self.previous_files()
        (self.source / "status.json").write_bytes(status(request_id="not-a-valid-request-id"))
        for args in ((), ("--status-only",)):
            with self.subTest(args=args):
                self.assertNotEqual(self.publish(*args).returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_exactly_one_php_placeholder_is_required(self):
        self.protection()
        before = self.previous_files()
        for count in (0, 2):
            with self.subTest(count=count):
                (self.source / "refresh.php").write_text("<?php " + publisher.PLACEHOLDER * count)
                self.assertNotEqual(self.publish().returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_invalid_or_public_private_directory_is_refused(self):
        self.protection()
        before = self.previous_files()
        for app in (Path("relative"), self.root / "missing", self.root):
            with self.subTest(app=app), self.assertRaises(publisher.PublicationError):
                publisher.publish(self.source, self.target, app)
            self.assertEqual(self.snapshot(), before)
        self.private.chmod(0o755)
        self.assertNotEqual(self.publish().returncode, 0)
        self.private.chmod(0o700)
        with self.assertRaises(publisher.PublicationError):
            publisher.publish(self.source, self.app, self.app)
        self.assertEqual(self.snapshot(), before)

    def test_stages_every_asset_and_publishes_status_last(self):
        self.protection()
        calls = []
        replace = os.replace

        def observe(source, target):
            if target.parent == self.target:
                self.assertTrue(all((source.parent / name).is_file() for name in publisher.FILES[len(calls) :]))
                calls.append(target.name)
            return replace(source, target)

        with patch("octopus_energy.publisher.os.replace", side_effect=observe):
            publisher.publish(self.source, self.target, self.app)
        self.assertEqual(calls, list(publisher.FILES))

    def test_each_swap_failure_restores_all_prior_files_and_permissions(self):
        self.protection()
        before = self.previous_files()
        replace = os.replace
        for position in range(4):
            calls = 0

            def fail_once(source, target, position=position):
                nonlocal calls
                if target.parent == self.target:
                    calls += 1
                    if calls == position + 1:
                        raise OSError("synthetic one-time swap failure")
                return replace(source, target)

            with (
                self.subTest(position=position),
                patch("octopus_energy.publisher.os.replace", side_effect=fail_once),
                self.assertRaises(publisher.PublicationError) as caught,
            ):
                publisher.publish(self.source, self.target, self.app)
            self.assertEqual(caught.exception.category, "rolled_back" if position else "nothing_changed")
            self.assertEqual(self.snapshot(), before)
            self.assertFalse(any(p.is_dir() for p in self.target.iterdir()))

    def test_failure_removes_new_files_but_keeps_preexisting_index_and_data(self):
        self.protection()
        before = self.previous_files(("index.html", "data.json"))
        replace = os.replace

        def fail_status(source, target):
            if source.parent.name.startswith(".energy-publish-") and target.name == "status.json":
                raise OSError("synthetic status swap failure")
            return replace(source, target)

        with patch("octopus_energy.publisher.os.replace", side_effect=fail_status):
            with self.assertRaises(publisher.PublicationError) as caught:
                publisher.publish(self.source, self.target, self.app)
        self.assertEqual(caught.exception.category, "rolled_back")
        self.assertEqual(self.snapshot(), before)

    def test_staging_failure_is_nothing_changed(self):
        self.protection()
        before = self.previous_files()
        write_bytes = Path.write_bytes

        def fail_stage(path, body):
            if path.name == "index.html" and path.parent.name.startswith(".energy-publish-"):
                raise OSError("synthetic staging failure")
            return write_bytes(path, body)

        with patch.object(Path, "write_bytes", fail_stage), patch("octopus_energy.publisher.os.replace") as replace:
            with self.assertRaises(publisher.PublicationError) as caught:
                publisher.publish(self.source, self.target, self.app)
        self.assertEqual(caught.exception.category, "nothing_changed")
        replace.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_partial_rollback_is_distinct_and_keeps_recovery_and_old_success_status(self):
        self.protection()
        before = self.previous_files()
        replace = os.replace

        def fail_publish_and_restore(source, target):
            if target.name == "index.html" and source.parent.name.startswith(".energy-publish-"):
                raise OSError("synthetic publication failure")
            if target.name == "data.json" and source.parent.name == "previous":
                raise OSError("synthetic rollback failure")
            return replace(source, target)

        with patch("octopus_energy.publisher.os.replace", side_effect=fail_publish_and_restore):
            with self.assertRaises(publisher.PublicationError) as caught:
                publisher.publish(self.source, self.target, self.app)
        self.assertEqual(caught.exception.category, "partially_published")
        self.assertEqual((self.target / "index.html").read_bytes(), before["index.html"][0])
        self.assertEqual((self.target / "status.json").read_bytes(), before["status.json"][0])
        recovery = list(self.target.glob(".energy-publish-*/previous/data.json"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].read_bytes(), before["data.json"][0])

    def test_refuses_links_and_pipes_without_copying_excluded_content(self):
        self.protection()
        outside = self.root / "excluded-private-file"
        outside.write_text("synthetic excluded data")
        (self.target / "index.html").symlink_to(outside)
        self.assertNotEqual(self.publish().returncode, 0)
        self.assertTrue((self.target / "index.html").is_symlink())
        self.assertFalse((self.target / "data.json").exists())
        (self.target / "index.html").unlink()
        (self.source / "index.html").unlink()
        os.mkfifo(self.source / "index.html")
        self.assertNotEqual(self.publish().returncode, 0)
        self.assertFalse((self.target / "data.json").exists())

    def test_status_only_needs_no_other_source_and_preserves_every_other_file(self):
        self.protection()
        before = self.previous_files()
        for name in publisher.FILES[:-1]:
            (self.source / name).unlink()
        result = self.publish("--status-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("index.html", "data.json", "refresh.php", ".htaccess"):
            self.assertEqual(self.snapshot()[name], before[name])
        self.assertEqual((self.target / "status.json").read_bytes(), (self.source / "status.json").read_bytes())
        self.assertEqual(set(self.snapshot()), set(before))
        (self.source / "status.json").write_bytes(b'{"unknown":true}')
        updated = self.snapshot()
        self.assertNotEqual(self.publish("--status-only").returncode, 0)
        self.assertEqual(self.snapshot(), updated)
        (self.target / ".htaccess").unlink()
        (self.source / "status.json").write_bytes(status())
        self.assertNotEqual(self.publish("--status-only").returncode, 0)

    def test_overlapping_status_only_waits_for_full_transaction(self):
        self.protection()
        self.previous_files()
        other = self.root / "status-only-source"
        other.mkdir()
        (other / "status.json").write_bytes(status(result="failed", error_category="sync_failed"))
        first_paused, release_first, second_started = Event(), Event(), Event()
        replace = os.replace

        def pause_full(source, target):
            if target.name == "data.json" and source.parent.name.startswith(".energy-publish-"):
                first_paused.set()
                if not release_first.wait(5):
                    raise OSError("Synthetic overlap test timed out")
            return replace(source, target)

        def second_publish():
            second_started.set()
            publisher.publish(other, self.target, self.app, status_only=True)

        with patch("octopus_energy.publisher.os.replace", side_effect=pause_full), ThreadPoolExecutor(2) as pool:
            first = pool.submit(publisher.publish, self.source, self.target, self.app)
            try:
                self.assertTrue(first_paused.wait(5))
                second = pool.submit(second_publish)
                self.assertTrue(second_started.wait(5))
                self.assertFalse(second.done(), "Status-only must wait for the active full transaction")
            finally:
                release_first.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual((self.target / "status.json").read_bytes(), (other / "status.json").read_bytes())
        self.assertEqual((self.target / "data.json").read_bytes(), (self.source / "data.json").read_bytes())
        self.assertEqual((self.target / "index.html").read_bytes(), (self.source / "index.html").read_bytes())

    def test_sigterm_rolls_back_completed_swaps_before_exit(self):
        self.protection()
        before = self.previous_files()
        marker = self.root / "swap-started"
        program = """
import os,sys,time
from pathlib import Path
from octopus_energy import publisher
original = os.replace
marker = Path(sys.argv[1])
def replace(source, target):
    if target.name == "data.json" and source.parent.name.startswith(".energy-publish-"):
        marker.touch()
        time.sleep(20)
    return original(source, target)
publisher.os.replace = replace
sys.argv = ["publisher", "--source", sys.argv[2], "--target", sys.argv[3], "--app-path", sys.argv[4]]
raise SystemExit(publisher.main())
"""
        process = subprocess.Popen(
            [sys.executable, "-c", program, str(marker), str(self.source), str(self.target), str(self.app)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "Publisher did not reach its second swap")
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 1, stderr)
            self.assertIn("rolled_back", stderr)
            self.assertEqual(self.snapshot(), before)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_shell_execs_the_rollback_owning_python_process(self):
        launcher = (ROOT / "scripts/publish-energy-dashboard.sh").read_text()
        self.assertIn('exec "$PYTHON" -m octopus_energy.publisher', launcher)
        capture = self.root / "publisher-pid"
        fake_python = self.root / "fake-python"
        fake_python.write_text(
            '#!/bin/sh\nif [ "$1" = -c ]; then exit 0; fi\n'
            f"exec {sys.executable} -c 'import os;from pathlib import Path;"
            'Path(os.environ["PID_CAPTURE"]).write_text(str(os.getpid()))\'\n'
        )
        fake_python.chmod(0o755)
        process = subprocess.Popen(
            [str(ROOT / "scripts/publish-energy-dashboard.sh")],
            cwd=self.app,
            env={
                **os.environ,
                "PYTHON": str(fake_python),
                "DASHBOARD_TARGET": str(self.target),
                "PID_CAPTURE": str(capture),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(int(capture.read_text()), process.pid)


class StatusValidationTests(unittest.TestCase):
    def test_request_id_is_nullable_or_exactly_32_lowercase_hex_characters(self):
        for request_id in (None, "0123456789abcdef" * 2, "f" * 32):
            with self.subTest(request_id=request_id):
                self.assertEqual(publisher.validate_status(status(request_id=request_id))["request_id"], request_id)
        for request_id in ("", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 32 + "\n", 1, True, [], {}):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                publisher.validate_status(status(request_id=request_id))
        missing = json.loads(status())
        del missing["request_id"]
        with self.assertRaises(ValueError):
            publisher.validate_status(json.dumps(missing).encode())

    def test_valid_states_and_every_allowlisted_error(self):
        for result in (None, "running", "success", "no_data", "partial"):
            publisher.validate_status(status(result=result))
        for request in (None, "pending", "running"):
            publisher.validate_status(status(request_state=request))
        for category in ERROR_CATEGORIES:
            publisher.validate_status(status(result="failed", error_category=category))
        publisher.validate_status(status(last_success_at="2024-02-29T23:59:59.123456Z"))
        publisher.validate_status(status(result=None, last_attempt_at=None, last_success_at=None))

    def test_rejects_wrong_types_keys_states_and_invalid_timestamps(self):
        valid = json.loads(status())
        invalid = []
        for key, values in {
            "schema_version": (True, 1.0, 2, None),
            "result": ("unknown", [], {}),
            "request_state": ("queued", False, []),
            "error_category": ("secret error text", 1, []),
            "latest_reading_at": (None, [], {"electricity": None}, {"electricity": None, "gas": None, "extra": None}),
            "last_attempt_at": (
                "2026-02-30T00:00:00Z",
                "2026-11-05T00:00:00+00:00",
                "2026-11-05T00:00:00.1Z",
                "2026-11-05T24:00:00Z",
                "2026-11-05",
                True,
            ),
        }.items():
            for value in values:
                item = copy.deepcopy(valid)
                item[key] = value
                invalid.append(item)
        invalid.extend(
            [
                {**valid, "unexpected": None},
                {key: value for key, value in valid.items() if key != "last_success_at"},
                {**valid, "result": "running", "last_attempt_at": None},
                {**valid, "last_success_at": None},
                {**valid, "result": "failed"},
                {**valid, "error_category": "publish_failed"},
                {**valid, "latest_reading_at": {"electricity": "2026-13-01T00:00:00Z", "gas": None}},
            ]
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                publisher.validate_status(json.dumps(value).encode())
        for body in (
            b"NaN",
            b"Infinity",
            b"-Infinity",
            b"{}",
            b"[]",
            b"\xff",
            status() + b" " * 16384,
            status().replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
        ):
            with self.subTest(body=body[:80]), self.assertRaises(ValueError):
                publisher.validate_status(body)
