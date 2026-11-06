import contextlib
import copy
import fcntl
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from octopus_energy import refresh

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
SHA = "a" * 40
REQUEST_A = "a1" * 16
REQUEST_B = "b2" * 16
SENSITIVE = "PRIVATE_SENTINEL_not_for_status_or_logs"
ROOT = Path(__file__).resolve().parents[1]


class EnergyRefreshTests(unittest.TestCase):
    def enter(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def setUp(self):
        self.enter(patch.dict(os.environ))
        for name in ("DASHBOARD_TARGET", "ENERGY_REFRESH_INTERVAL_MINUTES", "ENERGY_REFRESH_EXPECTED_SHA"):
            os.environ.pop(name, None)
        self.enter(patch.object(refresh, "_now", return_value=NOW))
        temporary = Path(self.enter(tempfile.TemporaryDirectory(prefix="energy-refresh-tests-"))).resolve()
        self.app = temporary / "private app"
        self.app.mkdir()
        (self.app / ".git").mkdir()
        (self.app / "scripts").mkdir()
        self.target = temporary / "public target"
        self.target.mkdir()
        self.state = self.app / "data/energy-refresh"
        self.state.mkdir(parents=True, mode=0o700)
        self.config = self.state / "config.json"
        self.configure()
        self.source = self.app / "reports/energy/dashboard"
        self.source.mkdir(parents=True)
        self.pending = self.state / "request.flag"
        self.claimed = self.state / "request.running"
        self.events = []
        self.published_statuses = []
        self.stdout = ""
        self.outcome = "success"
        self.sync_error = None
        self.sync_exception = None
        self.publisher_code = 0
        self.status_failures = 0
        self.after_sync = None
        self.origin_sha = SHA
        self.head_sha = SHA
        self.fetch_code = 0
        self.seed_public(NOW - timedelta(minutes=61))

    def configure(self, **overrides):
        values = {"DASHBOARD_TARGET": str(self.target), "ENERGY_REFRESH_INTERVAL_MINUTES": 60}
        values.update(overrides)
        self.config.write_text(json.dumps(values))
        self.config.chmod(0o600)

    def payload(self, at):
        return {
            "last_successful_sync_at": refresh._iso(at),
            "latest_reading_at": {
                "electricity": "2026-01-01T23:30:00+00:00",
                "gas": "2026-01-01T23:30:00+00:00",
            },
            "notice": SENSITIVE,
        }

    def seed_public(self, at):
        (self.target / "data.json").write_text(json.dumps(self.payload(at)))
        (self.target / "index.html").write_text("old dashboard")

    def flag(self, path=None):
        path = path or self.pending
        path.touch(mode=0o600)
        path.chmod(0o600)

    def history(self, outcome, category=None):
        with sqlite3.connect(self.app / "data/octopus_energy.sqlite3") as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sync_history(id INTEGER PRIMARY KEY,status,error_category)")
            conn.execute("INSERT INTO sync_history(status,error_category) VALUES (?,?)", (outcome, category))

    def worker(self, args, app, env, lock_fd, timeout, **kwargs):
        self.assertEqual(app, self.app)
        self.assertEqual(os.fstat(lock_fd).st_ino, (self.app / ".git/energy-dashboard.deploy.lock").stat().st_ino)
        name = Path(args[0]).name
        if name == "git":
            self.events.append(tuple(args[1:]))
            if args[1] == "fetch":
                return self.fetch_code, ""
            return 0, self.origin_sha if args[-1] == "origin/main" else self.head_sha
        if name == "start-energy.sh":
            self.events.append("sync")
            self.assertEqual(args[1:], ["--days", "90", "--gas-units", "kwh"])
            if self.sync_exception:
                raise self.sync_exception
            self.history(self.outcome, self.sync_error)
            if self.after_sync:
                self.after_sync()
            if self.outcome != "success":
                return 1, ""
            (self.source / "data.json").write_text(json.dumps(self.payload(NOW)))
            (self.source / "index.html").write_text("new dashboard")
            return 0, ""
        self.assertEqual(name, "publish-energy-dashboard.sh")
        self.assertEqual(env["DASHBOARD_TARGET"], str(self.target))
        self.assertEqual(env["DASHBOARD_DIR"], str(self.source))
        self.assertEqual(kwargs["termination_grace_seconds"], 30)
        if "--status-only" in args:
            status = refresh.validate_status(json.loads((self.source / "status.json").read_text()))
            self.published_statuses.append(copy.deepcopy(status))
            self.events.append(("status", status["result"], self.pending.exists(), self.claimed.exists()))
            if self.status_failures:
                self.status_failures -= 1
                return 1, ""
            shutil.copyfile(self.source / "status.json", self.target / "status.json")
        else:
            self.events.append("publish")
            if self.publisher_code:
                return self.publisher_code, ""
            for filename in ("data.json", "index.html"):
                shutil.copyfile(self.source / filename, self.target / filename)
        return 0, ""

    def run_tick(self, **kwargs):
        output = io.StringIO()
        with patch.object(refresh, "_command", side_effect=self.worker), contextlib.redirect_stdout(output):
            result = refresh.refresh(self.app, **kwargs)
        self.stdout = output.getvalue()
        return result

    def status(self):
        value = json.loads((self.target / "status.json").read_text())
        return refresh.validate_status(value)

    def test_due_default_interval_and_success_status(self):
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(
            self.events, [("status", "running", False, False), "sync", "publish", ("status", "success", False, False)]
        )
        status = self.status()
        self.assertEqual(status["last_success_at"], refresh._iso(NOW))
        self.assertEqual(status["last_attempt_at"], refresh._iso(NOW))
        self.assertEqual(status["latest_reading_at"]["gas"], "2026-01-01T23:30:00.000000Z")
        self.assertEqual(stat.S_IMODE((self.source / "status.json").stat().st_mode), 0o600)

    def test_fresh_public_bootstrap_does_not_sync_or_invent_attempt(self):
        self.seed_public(NOW - timedelta(minutes=1))
        self.assertEqual(self.run_tick(), 0)
        self.assertNotIn("sync", self.events)
        self.assertNotIn("publish", self.events)
        self.assertIsNone(self.status()["last_attempt_at"])
        self.events.clear()
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.events, [])

    def test_interval_exact_boundary_and_environment_override(self):
        os.environ["ENERGY_REFRESH_INTERVAL_MINUTES"] = "2"
        self.seed_public(NOW - timedelta(seconds=119))
        self.assertEqual(self.run_tick(), 0)
        self.assertNotIn("sync", self.events)
        self.events.clear()
        self.seed_public(NOW - timedelta(seconds=120))
        self.assertEqual(self.run_tick(), 0)
        self.assertIn("sync", self.events)

    def test_default_interval_when_omitted(self):
        self.config.write_text(json.dumps({"DASHBOARD_TARGET": str(self.target)}))
        self.assertEqual(refresh._config(self.app)[1], 60)

    def test_environment_target_overrides_config(self):
        alternate = self.target.parent / "alternate"
        alternate.mkdir()
        os.environ["DASHBOARD_TARGET"] = str(alternate)
        self.assertEqual(refresh._config(self.app)[0], alternate)

    def test_empty_manual_request_status_published_before_claim_and_sync(self):
        self.seed_public(NOW - timedelta(minutes=1))
        self.flag()
        self.assertEqual(self.pending.read_bytes(), b"")
        self.after_sync = lambda: self.assertTrue(self.claimed.exists())
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.events[0], ("status", "running", True, False))
        self.assertEqual(self.events[-1], ("status", "success", False, True))
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.claimed.exists())

    def test_flag_content_is_ignored(self):
        self.flag()
        self.pending.write_text(SENSITIVE)
        self.assertEqual(self.run_tick(), 0)
        self.assertNotIn(SENSITIVE, (self.target / "status.json").read_text())
        self.assertIsNone(self.status()["request_id"])

    def identified_flag(self, request_id, path=None):
        path = path or self.pending
        self.flag(path)
        path.write_bytes(request_id.encode("ascii"))

    def test_request_id_is_read_only_after_claim_and_published_before_sync(self):
        self.identified_flag(REQUEST_A)
        read_id = refresh._claimed_request_id
        reads = []

        def claimed_id(path):
            self.assertEqual(path, self.claimed)
            self.assertFalse(self.pending.exists())
            self.assertTrue(self.claimed.exists())
            reads.append(path)
            return read_id(path)

        def check_before_source():
            self.assertEqual(self.published_statuses[-1]["result"], "running")
            self.assertEqual(self.published_statuses[-1]["request_id"], REQUEST_A)

        self.after_sync = check_before_source
        with patch.object(refresh, "_claimed_request_id", side_effect=claimed_id):
            self.assertEqual(self.run_tick(), 0)
        self.assertEqual(reads, [self.claimed])
        self.assertEqual(
            self.events[:3],
            [("status", "running", True, False), ("status", "running", False, True), "sync"],
        )
        self.assertIsNone(self.published_statuses[0]["request_id"])
        self.assertEqual([row["request_id"] for row in self.published_statuses[1:]], [REQUEST_A, REQUEST_A])
        self.assertEqual(self.status()["request_id"], REQUEST_A)

    def test_request_id_survives_terminal_retries_and_idle_ticks(self):
        self.identified_flag(REQUEST_A)
        for outcome in ("no_data", "partial", "failed"):
            self.outcome = outcome
            self.assertEqual(self.run_tick(), 1)
            self.assertEqual(self.status()["request_id"], REQUEST_A)
            self.assertEqual(self.claimed.read_text(), REQUEST_A)
        self.outcome = "success"
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.status()["request_id"], REQUEST_A)
        self.published_statuses.clear()
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.status()["request_id"], REQUEST_A)
        self.assertEqual(self.published_statuses, [])

    def test_retained_claim_is_served_before_new_successor_without_losing_either_id(self):
        self.identified_flag(REQUEST_A, self.claimed)
        self.identified_flag(REQUEST_B)
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.status()["request_id"], REQUEST_A)
        self.assertEqual(self.status()["request_state"], "pending")
        self.assertEqual(self.pending.read_text(), REQUEST_B)
        self.assertFalse(self.claimed.exists())
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.status()["request_id"], REQUEST_B)
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.claimed.exists())

    def test_scheduled_attempt_clears_previous_request_id_but_idle_tick_does_not(self):
        self.identified_flag(REQUEST_A)
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.status()["request_id"], REQUEST_A)
        self.seed_public(NOW - timedelta(minutes=61))
        self.published_statuses.clear()
        self.assertEqual(self.run_tick(), 0)
        self.assertTrue(all(row["request_id"] is None for row in self.published_statuses))

    def test_legacy_status_is_normalized_on_read_but_write_schema_requires_request_id(self):
        self.seed_public(NOW - timedelta(minutes=1))
        self.assertEqual(self.run_tick(), 0)
        previous = self.status()
        previous.pop("request_id")
        with self.assertRaises(ValueError):
            refresh.validate_status(previous)
        (self.source / "status.json").write_text(json.dumps(previous))
        (self.target / "status.json").write_text(json.dumps(previous))
        self.assertEqual(self.run_tick(), 0)
        self.assertIsNone(self.status()["request_id"])
        self.assertIsNone(self.status()["last_attempt_at"])

    def test_flag_arriving_after_initial_running_is_claimed_then_identified(self):
        worker = self.worker
        created = False

        def incoming(args, *params, **kwargs):
            nonlocal created
            result = worker(args, *params, **kwargs)
            if "--status-only" in args and not created:
                created = True
                self.identified_flag(REQUEST_A)
            return result

        with patch.object(refresh, "_command", side_effect=incoming), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh.refresh(self.app), 0)
        self.assertEqual([row["request_id"] for row in self.published_statuses], [None, REQUEST_A, REQUEST_A])
        self.assertFalse(self.pending.exists())

    def test_flag_arriving_after_claim_check_cannot_tag_the_scheduled_attempt(self):
        history = refresh._history
        created = False

        def incoming(app):
            nonlocal created
            if not created:
                created = True
                self.identified_flag(REQUEST_A)
            return history(app)

        with patch.object(refresh, "_history", side_effect=incoming):
            self.assertEqual(self.run_tick(), 0)
        self.assertIsNone(self.status()["request_id"])
        self.assertEqual(self.pending.read_text(), REQUEST_A)
        self.assertEqual(self.status()["request_state"], "pending")
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.status()["request_id"], REQUEST_A)

    def test_empty_and_malformed_payloads_are_served_once_with_null_identity(self):
        for payload in (b"", b"A" * 32, b"a" * 31, b"a" * 32 + b"\n", b"\xff" * 32, b"a" * 100000):
            with self.subTest(size=len(payload)):
                self.flag()
                self.pending.write_bytes(payload)
                self.assertEqual(self.run_tick(), 0)
                self.assertEqual(self.status()["result"], "success")
                self.assertIsNone(self.status()["request_id"])
                self.assertFalse(self.pending.exists())
                self.assertFalse(self.claimed.exists())

    def test_unreadable_payload_is_served_with_null_identity_without_exposing_error(self):
        self.identified_flag(REQUEST_A)
        open_file = os.open

        def unreadable(path, *args, **kwargs):
            if Path(path) == self.claimed:
                raise PermissionError(SENSITIVE)
            return open_file(path, *args, **kwargs)

        with patch.object(refresh.os, "open", side_effect=unreadable):
            self.assertEqual(self.run_tick(), 0)
        self.assertIsNone(self.status()["request_id"])
        self.assertFalse(self.claimed.exists())
        self.assertNotIn(SENSITIVE, (self.state / "refresh.log").read_text())

    def test_temporary_request_files_are_not_consumed_or_used_as_identity(self):
        self.seed_public(NOW - timedelta(minutes=1))
        temporary = self.state / f".request-{REQUEST_A}.tmp"
        temporary.write_text(REQUEST_A)
        temporary.chmod(0o600)
        self.assertEqual(self.run_tick(), 0)
        self.assertNotIn("sync", self.events)
        self.assertTrue(temporary.exists())
        self.assertIsNone(self.status()["request_id"])

    def test_request_id_validator_accepts_only_lowercase_hex_or_null(self):
        self.assertEqual(self.run_tick(), 0)
        for invalid in ("", "A" * 32, "a" * 31, "a" * 33, "g" * 32, "a" * 32 + "\n", [], 123, False):
            with self.subTest(value=repr(invalid)):
                status = dict(self.status(), request_id=invalid)
                with self.assertRaises(ValueError):
                    refresh.validate_status(status)
        self.assertEqual(refresh.validate_status(dict(self.status(), request_id=REQUEST_A))["request_id"], REQUEST_A)

    def test_unclaimed_failure_cannot_reuse_the_previous_attempts_identity(self):
        self.identified_flag(REQUEST_A)
        self.assertEqual(self.run_tick(), 0)
        self.identified_flag(REQUEST_B)
        self.pending.chmod(0o644)
        self.assertEqual(self.run_tick(), 1)
        self.assertIsNone(self.status()["request_id"])
        self.assertEqual(self.status()["error_category"], "invalid_configuration")
        self.assertTrue(self.pending.exists())

    def test_running_status_publish_failure_never_claims_or_syncs(self):
        self.flag()
        self.status_failures = 2
        self.assertEqual(self.run_tick(), 1)
        self.assertTrue(self.pending.exists())
        self.assertFalse(self.claimed.exists())
        self.assertNotIn("sync", self.events)
        self.assertEqual(
            json.loads((self.source / "status.json").read_text())["error_category"], "status_publish_failed"
        )

    def test_click_during_run_remains_pending(self):
        self.flag()
        self.after_sync = self.flag
        self.assertEqual(self.run_tick(), 0)
        self.assertTrue(self.pending.exists())
        self.assertFalse(self.claimed.exists())
        self.assertEqual(self.status()["request_state"], "pending")

    def test_leftover_running_request_retries_even_when_public_is_fresh(self):
        self.seed_public(NOW - timedelta(minutes=1))
        self.flag(self.claimed)
        self.assertEqual(self.run_tick(), 0)
        self.assertIn("sync", self.events)
        self.assertFalse(self.claimed.exists())

    def test_no_data_partial_failed_preserve_old_public_bytes_and_claim(self):
        old = {name: (self.target / name).read_bytes() for name in ("data.json", "index.html")}
        for outcome in ("no_data", "partial", "failed"):
            with self.subTest(outcome=outcome):
                self.outcome = outcome
                self.flag()
                self.events.clear()
                self.assertEqual(self.run_tick(), 1)
                self.assertTrue(self.claimed.exists())
                self.assertNotIn("publish", self.events)
                status = self.status()
                self.assertEqual(status["result"], outcome)
                self.assertEqual(status["request_state"], "pending")
                self.assertEqual(status["last_success_at"], refresh._iso(NOW - timedelta(minutes=61)))
                for name, content in old.items():
                    self.assertEqual((self.target / name).read_bytes(), content)

    def test_sync_timeout_keeps_request_and_sanitizes_error(self):
        self.flag()
        self.sync_exception = subprocess.TimeoutExpired("ignored", 1, output=SENSITIVE, stderr=SENSITIVE)
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual(self.status()["error_category"], "sync_timeout")
        self.assertTrue(self.claimed.exists())
        self.assertNotIn(SENSITIVE, (self.state / "refresh.log").read_text())

    def test_exception_messages_and_payload_fields_never_reach_status_or_log(self):
        self.sync_exception = RuntimeError(SENSITIVE + str(self.app))
        self.assertEqual(self.run_tick(), 1)
        for path in (self.target / "status.json", self.state / "refresh.log"):
            text = path.read_text()
            self.assertNotIn(SENSITIVE, text)
            self.assertNotIn(str(self.app), text)
        self.assertEqual(self.status()["error_category"], "sync_failed")

    def test_provider_error_category_is_allowlisted_not_raw(self):
        self.outcome, self.sync_error = "failed", "OctopusAPIError"
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual(self.status()["error_category"], "provider_error")

    def test_database_success_does_not_suppress_old_public_refresh(self):
        self.history("success")
        self.assertEqual(self.run_tick(), 0)
        self.assertIn("sync", self.events)

    def test_missing_or_future_public_snapshot_forces_collection(self):
        for future in (False, True):
            with self.subTest(future=future):
                if future:
                    self.seed_public(NOW + timedelta(days=1))
                else:
                    (self.target / "data.json").unlink()
                self.events.clear()
                self.assertEqual(self.run_tick(), 0)
                self.assertIn("sync", self.events)

    def test_publisher_exit_codes_public_category_private_distinction(self):
        old = (self.target / "data.json").read_bytes()
        for code, label in ((1, "nothing_changed_or_rolled_back"), (2, "partially_published")):
            with self.subTest(code=code):
                self.flag()
                self.publisher_code = code
                self.assertEqual(self.run_tick(), 1)
                self.assertEqual(self.status()["error_category"], "publish_failed")
                self.assertEqual((self.target / "data.json").read_bytes(), old)
                self.assertTrue(self.claimed.exists())
                records = [json.loads(line) for line in (self.state / "refresh.log").read_text().splitlines()]
                self.assertEqual(records[-1]["publisher_result"], label)
                self.assertNotIn("publisher_result", self.status())

    def test_force_bypasses_due_check(self):
        self.seed_public(NOW - timedelta(minutes=1))
        self.assertEqual(self.run_tick(force=True), 0)
        self.assertIn("sync", self.events)

    def test_busy_deploy_lock_is_silent_and_does_not_touch_state(self):
        path = self.app / ".git/energy-dashboard.deploy.lock"
        with path.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = sorted(path.name for path in self.state.iterdir())
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                self.assertEqual(self.run_tick(), 0)
            self.assertEqual(self.events, [])
            self.assertEqual(sorted(path.name for path in self.state.iterdir()), before)
            self.assertEqual(output.getvalue() + error.getvalue(), "")
            self.assertEqual(self.stdout, "")

    def test_matching_inherited_fd_works_and_does_not_unlock_deploy(self):
        path = self.app / ".git/energy-dashboard.deploy.lock"
        with path.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_tick(force=True, inherited_lock_fd=held.fileno()), 0)
            self.assertIn("sync", self.events)
            with path.open("r+") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fstat(held.fileno())

    def test_config_is_read_under_the_shared_lock(self):
        read_config = refresh._config

        def check_lock(app):
            with (app / ".git/energy-dashboard.deploy.lock").open("r+") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return read_config(app)

        with patch.object(refresh, "_config", side_effect=check_lock):
            self.assertEqual(self.run_tick(), 0)

    def test_inherited_fd_inode_mismatch_fails_before_workers(self):
        (self.app / ".git/energy-dashboard.deploy.lock").touch()
        with (self.app / "wrong.lock").open("w") as wrong:
            with self.assertRaises(refresh.RefreshError) as raised:
                self.run_tick(force=True, inherited_lock_fd=wrong.fileno())
        self.assertEqual(raised.exception.category, "inherited_lock_invalid")
        self.assertEqual(self.events, [])

    def test_expected_release_guards_run_between_sync_and_set_publish(self):
        os.environ["ENERGY_REFRESH_EXPECTED_SHA"] = SHA
        self.assertEqual(self.run_tick(force=True), 0)
        self.assertLess(self.events.index("sync"), self.events.index(("fetch", "--no-tags", "origin", "main")))
        self.assertLess(self.events.index(("rev-parse", "HEAD")), self.events.index("publish"))

    def test_superseded_release_success_skip_preserves_data_and_request(self):
        os.environ["ENERGY_REFRESH_EXPECTED_SHA"] = SHA
        self.origin_sha = "b" * 40
        self.flag()
        old = (self.target / "data.json").read_bytes()
        self.assertEqual(self.run_tick(force=True), 0)
        self.assertNotIn("publish", self.events)
        self.assertEqual(self.status()["error_category"], "superseded_release")
        self.assertEqual((self.target / "data.json").read_bytes(), old)
        self.assertTrue(self.claimed.exists())

    def test_head_mismatch_and_fetch_failure_fail_without_set_publication(self):
        os.environ["ENERGY_REFRESH_EXPECTED_SHA"] = SHA
        for head, fetch, category in (("b" * 40, 0, "release_head_mismatch"), (SHA, 1, "release_check_failed")):
            with self.subTest(category=category):
                self.head_sha, self.fetch_code = head, fetch
                self.events.clear()
                self.assertEqual(self.run_tick(force=True), 1)
                self.assertNotIn("publish", self.events)
                self.assertEqual(self.status()["error_category"], category)

    def test_cron_does_not_contact_git(self):
        self.assertEqual(self.run_tick(), 0)
        self.assertFalse(any(isinstance(event, tuple) and event[0] in ("fetch", "rev-parse") for event in self.events))

    def test_invalid_expected_sha_fails_before_workers(self):
        os.environ["ENERGY_REFRESH_EXPECTED_SHA"] = SENSITIVE
        with self.assertRaises(refresh.RefreshError):
            self.run_tick(force=True)
        self.assertEqual(self.events, [])

    def test_bad_revision_and_guard_timeout_never_publish_data(self):
        os.environ["ENERGY_REFRESH_EXPECTED_SHA"] = SHA
        self.origin_sha = SENSITIVE
        self.assertEqual(self.run_tick(force=True), 1)
        self.assertEqual(self.status()["error_category"], "release_check_failed")
        self.assertNotIn("publish", self.events)
        with patch.object(refresh, "_command", side_effect=subprocess.TimeoutExpired("ignored", 1)):
            self.assertEqual(refresh._release_guard(self.app, dict(os.environ), 9, SHA), "release_check_failed")

    def test_final_status_failure_keeps_claim_and_records_delivered_snapshot(self):
        self.flag()

        def worker(args, *params, **kwargs):
            if "--status-only" in args:
                status = json.loads((self.source / "status.json").read_text())
                if status["result"] == "success":
                    return 1, ""
            return self.worker(args, *params, **kwargs)

        with patch.object(refresh, "_command", side_effect=worker):
            self.assertEqual(refresh.refresh(self.app), 1)
        self.assertEqual(self.status()["error_category"], "status_publish_failed")
        self.assertEqual(self.status()["last_success_at"], refresh._iso(NOW))
        self.assertTrue(self.claimed.exists())
        self.assertEqual((self.target / "index.html").read_text(), "new dashboard")

    def test_stale_incomplete_history_does_not_misclassify_new_launch_failure(self):
        self.history("no_data")
        original_worker = self.worker

        def worker(args, *params, **kwargs):
            if Path(args[0]).name == "start-energy.sh":
                return 1, ""
            return original_worker(args, *params, **kwargs)

        with patch.object(refresh, "_command", side_effect=worker):
            self.assertEqual(refresh.refresh(self.app), 1)
        self.assertEqual(self.status()["result"], "failed")
        self.assertEqual(self.status()["error_category"], "sync_failed")

    def test_interrupted_running_status_is_repaired_without_source_when_fresh(self):
        self.seed_public(NOW - timedelta(minutes=1))
        self.assertEqual(self.run_tick(), 0)
        status = self.status()
        status.update(result="running", request_state="running", last_attempt_at=refresh._iso(NOW))
        (self.source / "status.json").write_text(json.dumps(status))
        self.events.clear()
        self.assertEqual(self.run_tick(), 0)
        self.assertNotIn("sync", self.events)
        self.assertEqual(self.status()["error_category"], "interrupted")

    def test_insecure_request_is_rejected_without_source(self):
        self.flag()
        self.pending.chmod(0o644)
        self.assertEqual(self.run_tick(), 1)
        self.assertNotIn("sync", self.events)
        self.assertEqual(self.status()["error_category"], "invalid_configuration")

    def test_private_config_permissions_owner_and_target_fail_closed(self):
        for mode in (0o644, 0o400):
            with self.subTest(mode=mode):
                self.config.chmod(mode)
                with self.assertRaises(refresh.RefreshError):
                    self.run_tick()
        self.config.chmod(0o600)
        self.state.chmod(0o755)
        with self.assertRaises(refresh.RefreshError):
            self.run_tick()
        self.state.chmod(0o700)
        with patch.object(refresh.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(refresh.RefreshError):
                self.run_tick()
        self.configure(DASHBOARD_TARGET=str(self.target / "missing"))
        with self.assertRaises(refresh.RefreshError):
            self.run_tick()
        self.assertEqual(self.events, [])

    def test_invalid_config_and_environment_rejected(self):
        for raw in (
            "not json",
            "[]",
            '{"DASHBOARD_TARGET":"relative"}',
            json.dumps({"DASHBOARD_TARGET": str(self.target), "API_KEY": SENSITIVE}),
        ):
            with self.subTest(raw=raw[:20]):
                self.config.write_text(raw)
                with self.assertRaises(refresh.RefreshError):
                    self.run_tick()
        self.configure()
        for value in ("0", "-1", "nan", ""):
            with self.subTest(value=value):
                os.environ["ENERGY_REFRESH_INTERVAL_MINUTES"] = value
                with self.assertRaises(refresh.RefreshError):
                    self.run_tick()
        self.assertEqual(self.events, [])

    def test_symlink_config_rejected(self):
        other = self.app / "other.json"
        self.config.rename(other)
        self.config.symlink_to(other)
        with self.assertRaises(refresh.RefreshError):
            self.run_tick()

    def test_status_validator_rejects_payloads_wrong_types_and_dates(self):
        self.assertEqual(self.run_tick(), 0)
        original = self.status()
        changes = (
            {"account": SENSITIVE},
            {"schema_version": True},
            {"request_state": "other"},
            {"last_attempt_at": "2026-02-30T12:00:00Z"},
            {"last_attempt_at": "2026-01-02T12:00:00"},
            {"last_success_at": None},
            {"error_category": SENSITIVE},
            {"latest_reading_at": {"electricity": None, "gas": None, "path": SENSITIVE}},
        )
        for change in changes:
            with self.subTest(change=list(change)):
                invalid = copy.deepcopy(original)
                invalid.update(change)
                with self.assertRaises(ValueError):
                    refresh.validate_status(invalid)

    def test_logs_are_rotated_bounded_and_private(self):
        with patch.object(refresh, "LOG_MAX_BYTES", 512):
            logger = refresh._logger(self.state)
        status = {"result": "failed", "error_category": "sync_failed"}
        try:
            for _ in range(100):
                refresh._event(logger, "finish", status, time.monotonic())
        finally:
            for handler in logger.handlers:
                handler.close()
        files = list(self.state.glob("refresh.log*"))
        self.assertEqual(len(files), 3)
        self.assertLessEqual(sum(path.stat().st_size for path in files), 3 * 512)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files))

    def test_command_inherits_lock_fd_and_discards_worker_payloads(self):
        with refresh._lock(self.app, None) as fd:
            code, output = refresh._command(
                [
                    sys.executable,
                    "-c",
                    f"import os,sys;os.fstat({fd});print({SENSITIVE!r});print({SENSITIVE!r},file=sys.stderr)",
                ],
                self.app,
                dict(os.environ),
                fd,
                5,
            )
        self.assertEqual((code, output), (0, ""))

    def test_timeout_terms_group_waits_rollback_grace_then_kills(self):
        events = []
        process = MagicMock()
        process.pid = 123456
        process.__enter__.return_value = process

        def communicate(timeout):
            events.append(("wait", timeout))
            if len(events) < 5:
                raise subprocess.TimeoutExpired("ignored", timeout)
            return "", ""

        process.communicate.side_effect = communicate
        with (
            patch.object(refresh.subprocess, "Popen", return_value=process),
            patch.object(refresh.os, "killpg", side_effect=lambda pid, sig: events.append(("signal", sig))),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                refresh._command(["publisher"], self.app, dict(os.environ), 9, 1, termination_grace_seconds=30)
        self.assertEqual(
            events,
            [
                ("wait", 1),
                ("signal", refresh.signal.SIGTERM),
                ("wait", 30),
                ("signal", refresh.signal.SIGKILL),
                ("wait", 2),
            ],
        )

    def test_timeout_allows_publisher_to_rollback_without_sigkill(self):
        process = MagicMock()
        process.pid = 123456
        process.__enter__.return_value = process
        process.communicate.side_effect = [subprocess.TimeoutExpired("ignored", 1), ("", "")]
        with (
            patch.object(refresh.subprocess, "Popen", return_value=process),
            patch.object(refresh.os, "killpg") as kill,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                refresh._command(["publisher"], self.app, dict(os.environ), 9, 1, termination_grace_seconds=30)
        kill.assert_called_once_with(process.pid, refresh.signal.SIGTERM)
        self.assertEqual(process.communicate.call_args.kwargs, {"timeout": 30})

    def test_completion_markers_follow_collection_and_full_publication(self):
        with patch("builtins.print", side_effect=lambda message, **kwargs: self.events.append(("marker", message))):
            self.assertEqual(self.run_tick(), 0)
        self.assertEqual(
            self.events,
            [
                ("status", "running", False, False),
                "sync",
                ("marker", "ENERGY_REFRESH collection_complete"),
                "publish",
                ("marker", "ENERGY_REFRESH publication_complete"),
                ("status", "success", False, False),
            ],
        )

    def test_success_markers_are_fixed_and_contain_no_child_payloads(self):
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.stdout, "ENERGY_REFRESH collection_complete\nENERGY_REFRESH publication_complete\n")
        self.assertNotIn(SENSITIVE, self.stdout)
        self.assertNotIn(str(self.app), self.stdout)

    def test_status_only_bootstrap_emits_no_completion_markers(self):
        self.seed_public(NOW - timedelta(minutes=1))
        self.assertEqual(self.run_tick(), 0)
        self.assertTrue(self.events)
        self.assertEqual(self.stdout, "")

    def test_incomplete_and_failed_collection_emit_no_completion_markers(self):
        for outcome in ("no_data", "partial", "failed"):
            with self.subTest(outcome=outcome):
                self.outcome = outcome
                self.assertEqual(self.run_tick(), 1)
                self.assertEqual(self.stdout, "")
        self.sync_exception = subprocess.TimeoutExpired("ignored", 1, output=SENSITIVE)
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual(self.stdout, "")

    def test_failed_full_publication_emits_only_collection_marker(self):
        for code in (1, 2):
            with self.subTest(code=code):
                self.publisher_code = code
                self.assertEqual(self.run_tick(), 1)
                self.assertEqual(self.stdout, "ENERGY_REFRESH collection_complete\n")

    def test_superseded_marker_is_distinct_from_full_publication(self):
        os.environ["ENERGY_REFRESH_EXPECTED_SHA"] = SHA
        self.origin_sha = "b" * 40
        self.assertEqual(self.run_tick(force=True), 0)
        self.assertEqual(self.stdout, "ENERGY_REFRESH collection_complete\nENERGY_REFRESH superseded_release\n")
        self.origin_sha, self.head_sha = SHA, "b" * 40
        self.assertEqual(self.run_tick(force=True), 1)
        self.assertEqual(self.stdout, "ENERGY_REFRESH collection_complete\n")

    def install_launcher(self, source_body, publisher_body):
        package = self.app / "octopus_energy"
        package.mkdir()
        (package / "__init__.py").touch()
        shutil.copy2(ROOT / "octopus_energy/refresh.py", package / "refresh.py")
        for script in ("find-python.sh", "refresh-energy.sh"):
            shutil.copy2(ROOT / "scripts" / script, self.app / "scripts" / script)
        publisher = self.app / "scripts/publish-energy-dashboard.sh"
        publisher.write_text("#!/bin/sh\n" + publisher_body)
        publisher.chmod(0o755)
        source = self.app / "scripts/start-energy.sh"
        source.write_text("#!/bin/sh\n" + source_body)
        source.chmod(0o755)

    def test_launcher_from_outside_app_uses_private_config_without_source_calls(self):
        self.install_launcher("exit 77\n", 'cp "$DASHBOARD_DIR/status.json" "$DASHBOARD_TARGET/status.json"\n')
        self.seed_public(datetime.now(timezone.utc) - timedelta(seconds=1))
        result = subprocess.run(
            [str(self.app / "scripts/refresh-energy.sh")],
            cwd=self.app.parent,
            env=dict(os.environ, PYTHON=sys.executable),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout + result.stderr, b"")
        self.assertIsNone(self.status()["last_attempt_at"])

    def test_runtime_runner_emits_only_markers_not_child_stdout_or_stderr(self):
        self.install_launcher(
            f'printf "%s\\n" "{SENSITIVE}"\nprintf "%s\\n" "{SENSITIVE}" >&2\n'
            'cp "$DASHBOARD_TARGET/data.json" "$DASHBOARD_DIR/data.json"\n'
            'printf "new dashboard" > "$DASHBOARD_DIR/index.html"\n',
            f'printf "%s\\n" "{SENSITIVE}"\nprintf "%s\\n" "{SENSITIVE}" >&2\n'
            'if [ "${1:-}" != "--status-only" ]; then\n'
            '  cp "$DASHBOARD_DIR/data.json" "$DASHBOARD_TARGET/data.json"\n'
            '  cp "$DASHBOARD_DIR/index.html" "$DASHBOARD_TARGET/index.html"\n'
            "fi\n"
            'cp "$DASHBOARD_DIR/status.json" "$DASHBOARD_TARGET/status.json"\n',
        )
        result = subprocess.run(
            [str(self.app / "scripts/refresh-energy.sh"), "--force"],
            cwd=self.app.parent,
            env=dict(os.environ, PYTHON=sys.executable),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b"ENERGY_REFRESH collection_complete\nENERGY_REFRESH publication_complete\n")
        self.assertEqual(result.stderr, b"")
        self.assertEqual((self.target / "index.html").read_text(), "new dashboard")

    def test_runtime_hardlinked_request_identity_order_inherited_lock_and_idle_persistence(self):
        publisher = """exec "$PYTHON" - "$@" <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path
source = Path(os.environ["DASHBOARD_DIR"])
target = Path(os.environ["DASHBOARD_TARGET"])
status = json.loads((source / "status.json").read_text())
record = {
    "result": status["result"],
    "request_id": status["request_id"],
    "claimed": (source.parents[2] / "data/energy-refresh/request.running").exists(),
}
with (target.parent / "runtime-statuses.jsonl").open("a") as output:
    output.write(json.dumps(record) + "\\n")
if "--status-only" not in sys.argv:
    for name in ("data.json", "index.html"):
        shutil.copyfile(source / name, target / name)
shutil.copyfile(source / "status.json", target / "status.json")
PY
"""
        self.install_launcher(
            'cp "$DASHBOARD_TARGET/data.json" "$DASHBOARD_DIR/data.json"\n'
            'printf "new dashboard" > "$DASHBOARD_DIR/index.html"\n',
            publisher,
        )
        temporary = self.state / f".request-{REQUEST_A}.tmp"
        temporary.write_text(REQUEST_A)
        temporary.chmod(0o600)
        os.link(temporary, self.pending)
        with refresh._lock(self.app, None) as fd:
            result = subprocess.run(
                [str(self.app / "scripts/refresh-energy.sh"), "--force", "--inherited-lock-fd", str(fd)],
                cwd=self.app.parent,
                env=dict(os.environ, PYTHON=sys.executable),
                pass_fds=(fd,),
                capture_output=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b"ENERGY_REFRESH collection_complete\nENERGY_REFRESH publication_complete\n")
        self.assertEqual(result.stderr, b"")
        records = [
            json.loads(line) for line in (self.target.parent / "runtime-statuses.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [(row["result"], row["request_id"], row["claimed"]) for row in records],
            [
                ("running", None, False),
                ("running", REQUEST_A, True),
                ("running", REQUEST_A, True),
                ("success", REQUEST_A, True),
            ],
        )
        self.assertEqual(self.status()["request_id"], REQUEST_A)
        self.assertTrue(temporary.exists())
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.claimed.exists())
        self.seed_public(datetime.now(timezone.utc) - timedelta(seconds=1))
        idle = subprocess.run(
            [str(self.app / "scripts/refresh-energy.sh")],
            cwd=self.app.parent,
            env=dict(os.environ, PYTHON=sys.executable),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(idle.returncode, 0, idle.stderr.decode())
        self.assertEqual(idle.stdout + idle.stderr, b"")
        self.assertEqual(self.status()["request_id"], REQUEST_A)
        self.assertTrue(temporary.exists())


if __name__ == "__main__":
    unittest.main()
