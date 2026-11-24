import fcntl
import io
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from instagram_agent import cli, content_plan, env_install, storage, token_refresh
from instagram_agent.graph import GRAPH_HOST, INSTAGRAM_HOST, GraphAPIError, GraphClient

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
OLD_TOKEN = "IG_TEST_OLD"
NEW_TOKEN = "IG_TEST_NEW"
ROTATED_TOKEN = "IG_TEST_ROTATED"
TOKEN_KEY = "INSTAGRAM_ACCESS_TOKEN"


class FakeRefresh:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def refresh_access_token(self):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TokenRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "instagram.sqlite3"
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.env_file = self.root / ".env"
        self.env_file.write_text(
            f"INSTAGRAM_ACCESS_TOKEN={OLD_TOKEN}\nINSTAGRAM_USER_ID=123\nUNRELATED=keep\n", encoding="utf-8"
        )
        self.env = {"INSTAGRAM_ACCESS_TOKEN": OLD_TOKEN, "INSTAGRAM_USER_ID": "123"}

    def response(self, **overrides):
        return {
            "access_token": NEW_TOKEN,
            "token_type": "bearer",
            "expires_in": 60 * 24 * 60 * 60,
        } | overrides

    def tick(self, response, *, now=NOW, env=None, environ=None):
        return token_refresh.tick(
            self.conn,
            self.env if env is None else env,
            self.env_file,
            now,
            environ={} if environ is None else environ,
            client=FakeRefresh(response),
        )

    def test_success_persists_token_and_only_non_secret_metadata(self):
        client = FakeRefresh(self.response())
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            outcome = token_refresh.tick(self.conn, self.env, self.env_file, NOW, environ={}, client=client)
        self.assertEqual(outcome, "refreshed")
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.env["INSTAGRAM_ACCESS_TOKEN"], NEW_TOKEN)
        content = self.env_file.read_text()
        self.assertIn(f"INSTAGRAM_ACCESS_TOKEN={NEW_TOKEN}\n", content)
        self.assertIn("UNRELATED=keep\n", content)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        state = storage.token_state(self.conn)
        expected = NOW + timedelta(days=60)
        self.assertEqual(state["expires_at"], expected.isoformat(timespec="seconds"))
        self.assertEqual(state["last_refreshed_at"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(state["last_outcome"], "refreshed")
        self.assertIsNone(state["last_error"])
        self.assertEqual(state["token_fingerprint"], sha256(NEW_TOKEN.encode()).hexdigest())
        self.assertNotIn(OLD_TOKEN, repr(state))
        self.assertNotIn(NEW_TOKEN, repr(state))
        self.assertNotIn(OLD_TOKEN.encode(), self.db.read_bytes())
        self.assertNotIn(NEW_TOKEN.encode(), self.db.read_bytes())

    def test_not_due_does_not_call_meta_again(self):
        self.tick(self.response())
        client = FakeRefresh(AssertionError("unexpected Meta call"))
        outcome = token_refresh.tick(self.conn, self.env, self.env_file, NOW, environ={}, client=client)
        self.assertEqual(outcome, "not_due")
        self.assertEqual(client.calls, 0)

    def test_replacement_ignores_the_previous_tokens_expiry_and_backoff(self):
        self.assertEqual(self.tick(self.response(expires_in=31 * 24 * 60 * 60)), "refreshed")

        env_install.upsert(self.env_file, {TOKEN_KEY: ROTATED_TOKEN})
        self.env[TOKEN_KEY] = ROTATED_TOKEN
        error = GraphAPIError("Graph API request failed")
        self.assertEqual(self.tick(error), "failed")
        state = storage.token_state(self.conn)
        self.assertIsNone(state["expires_at"])
        self.assertIsNone(state["last_refreshed_at"])
        self.assertEqual(self.tick(AssertionError("unexpected retry")), "not_due")

        env_install.upsert(self.env_file, {TOKEN_KEY: OLD_TOKEN})
        self.env[TOKEN_KEY] = OLD_TOKEN
        self.assertEqual(self.tick(self.response()), "refreshed")

    def test_manual_rotation_during_refresh_wins_and_leaves_no_stale_schedule(self):
        def rotate():
            env_install.upsert(self.env_file, {TOKEN_KEY: ROTATED_TOKEN})
            return self.response()

        with patch.object(FakeRefresh, "refresh_access_token", side_effect=rotate):
            self.assertEqual(self.tick(None), "token_changed")
        self.assertIn(f"{TOKEN_KEY}={ROTATED_TOKEN}\n", self.env_file.read_text())
        state = storage.token_state(self.conn)
        self.assertIsNone(state["token_fingerprint"])
        self.assertIsNone(state["expires_at"])
        self.assertIsNone(state["last_attempt_at"])
        self.env[TOKEN_KEY] = ROTATED_TOKEN
        self.assertEqual(self.tick(self.response()), "refreshed")

    def test_existing_token_state_table_is_upgraded_and_rescheduled(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("ALTER TABLE token_state DROP COLUMN token_fingerprint")
            conn.execute(
                "INSERT INTO token_state(id,expires_at,last_outcome) VALUES(1,?,'refreshed')",
                ((NOW + timedelta(days=60)).isoformat(),),
            )
        with storage.connect(self.db) as conn:
            self.assertIsNone(storage.token_state(conn)["token_fingerprint"])
            self.assertEqual(
                token_refresh.tick(conn, self.env, self.env_file, NOW, environ={}, client=FakeRefresh(self.response())),
                "refreshed",
            )

    def test_schedule_uses_thirty_day_margin_and_daily_failure_backoff(self):
        self.assertTrue(token_refresh.should_refresh({"expires_at": (NOW + timedelta(days=30)).isoformat()}, NOW))
        self.assertFalse(
            token_refresh.should_refresh({"expires_at": (NOW + timedelta(days=30, seconds=1)).isoformat()}, NOW)
        )
        failed = {
            "expires_at": None,
            "last_outcome": "failed",
            "last_attempt_at": (NOW - timedelta(hours=23, minutes=59)).isoformat(),
        }
        self.assertFalse(token_refresh.should_refresh(failed, NOW))
        failed["last_attempt_at"] = (NOW - timedelta(hours=24)).isoformat()
        self.assertTrue(token_refresh.should_refresh(failed, NOW))

    def test_failure_keeps_old_token_and_backs_off(self):
        error = GraphAPIError("Graph API request failed (HTTP 500, code 190)")
        self.assertEqual(self.tick(error), "failed")
        self.assertEqual(self.env["INSTAGRAM_ACCESS_TOKEN"], OLD_TOKEN)
        self.assertIn(f"INSTAGRAM_ACCESS_TOKEN={OLD_TOKEN}\n", self.env_file.read_text())
        state = storage.token_state(self.conn)
        self.assertEqual(state["last_outcome"], "failed")
        self.assertEqual(state["last_error"], str(error))
        self.assertIsNone(state["expires_at"])
        client = FakeRefresh(AssertionError("unexpected retry"))
        self.assertEqual(
            token_refresh.tick(self.conn, self.env, self.env_file, NOW + timedelta(hours=1), environ={}, client=client),
            "not_due",
        )
        self.assertEqual(client.calls, 0)

    def test_malformed_responses_never_replace_the_token(self):
        replies = (
            {},
            self.response(token_type="other"),
            self.response(access_token=""),
            self.response(access_token="not safe"),
            self.response(access_token=NEW_TOKEN + "\n"),
            self.response(access_token=NEW_TOKEN + "\nINSTAGRAM_USER_ID=456"),
            self.response(expires_in=True),
            self.response(expires_in=0),
            self.response(expires_in=token_refresh.MAX_EXPIRES_IN + 1),
        )
        for reply in replies:
            with self.subTest(reply=reply):
                with self.assertRaises(token_refresh.TokenRefreshError) as caught:
                    token_refresh._validated(reply, NOW)
                self.assertNotIn(NEW_TOKEN, str(caught.exception))

    def test_external_unsupported_and_missing_credentials_skip_api(self):
        external = dict(self.env)
        outcome = self.tick(
            AssertionError("unexpected Meta call"),
            env=external,
            environ={"INSTAGRAM_ACCESS_TOKEN": OLD_TOKEN},
        )
        self.assertEqual(outcome, "externally_managed")
        self.assertEqual(storage.token_state(self.conn)["last_outcome"], "externally_managed")
        self.assertIsNone(storage.token_state(self.conn)["last_attempt_at"])

        unsupported = {"INSTAGRAM_ACCESS_TOKEN": "EA_TEST", "INSTAGRAM_USER_ID": "123"}
        self.assertEqual(self.tick(AssertionError("unexpected Meta call"), env=unsupported), "unsupported")
        self.assertEqual(self.tick(AssertionError("unexpected Meta call"), env={}), "not_configured")
        self.assertIn(OLD_TOKEN, self.env_file.read_text())

    def test_symlinked_env_file_fails_without_touching_target(self):
        self.env_file.unlink()
        target = self.root / "target.env"
        target.write_text(f"INSTAGRAM_ACCESS_TOKEN={OLD_TOKEN}\n")
        self.env_file.symlink_to(target)
        self.assertEqual(self.tick(self.response()), "failed")
        self.assertEqual(target.read_text(), f"INSTAGRAM_ACCESS_TOKEN={OLD_TOKEN}\n")
        self.assertEqual(storage.token_state(self.conn)["last_error"], "ValueError")

    def test_metadata_failure_does_not_roll_back_replacement(self):
        with patch.object(storage, "record_token_success", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(self.tick(self.response()), "metadata_failed")
        self.assertEqual(self.env["INSTAGRAM_ACCESS_TOKEN"], NEW_TOKEN)
        self.assertIn(f"INSTAGRAM_ACCESS_TOKEN={NEW_TOKEN}\n", self.env_file.read_text())
        self.assertIsNone(storage.token_state(self.conn)["last_outcome"])

    def test_failed_refresh_with_unwritable_metadata_keeps_current_token(self):
        with patch.object(storage, "record_token_failure", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(self.tick(GraphAPIError("Graph API request failed")), "metadata_failed")
        self.assertEqual(self.env[TOKEN_KEY], OLD_TOKEN)
        self.assertIn(f"{TOKEN_KEY}={OLD_TOKEN}\n", self.env_file.read_text())

    def test_unreadable_metadata_skips_the_api_attempt(self):
        with patch.object(storage, "token_state", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(self.tick(AssertionError("unexpected Meta call")), "metadata_failed")


class RefreshTransportTests(unittest.TestCase):
    def test_instagram_login_uses_fixed_unversioned_refresh_endpoint(self):
        client = GraphClient(OLD_TOKEN, "123", host=INSTAGRAM_HOST)
        with patch.object(client, "get", return_value={}) as get:
            client.refresh_access_token()
        get.assert_called_once_with(
            "https://graph.instagram.com/refresh_access_token", {"grant_type": "ig_refresh_token"}
        )

    def test_facebook_login_is_not_sent_to_instagram_refresh_endpoint(self):
        client = GraphClient("EA_TEST", "123", host=GRAPH_HOST)
        with patch.object(client, "get") as get, self.assertRaises(GraphAPIError):
            client.refresh_access_token()
        get.assert_not_called()


class DailyRefreshIntegrationTests(unittest.TestCase):
    def test_late_launch_keeps_the_plan_but_does_not_draft_for_an_elapsed_slot(self):
        monday = NOW + timedelta(days=1, hours=8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "instagram.sqlite3"
            args = ["--env-file", str(root / ".env"), "--database", str(db), "daily", "--media", str(root / "media")]
            with (
                patch.dict(cli.os.environ, {}, clear=True),
                patch.object(cli, "_now", return_value=monday) as clock,
                patch.object(cli.token_refresh, "tick", return_value="not_due"),
                patch.object(cli.publish, "configured", return_value=True),
                patch.object(cli.publish, "tick", return_value="publish_off"),
                patch.object(cli, "model_client") as model,
                patch.object(cli.render, "render_due"),
                patch.object(cli, "_capture_followers", return_value="followers_off"),
                patch.object(cli, "_capture_stories", return_value="stories_off"),
                patch.object(cli, "_post_reports", return_value="reports_off"),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(args), 0)
                clock.return_value += timedelta(days=1)
                self.assertEqual(cli.main(args), 0)
            with storage.connect(db) as conn:
                self.assertEqual(len(content_plan.rows(conn, monday)), 3)
                self.assertIsNone(content_plan.slot_today(conn, monday))
            model.assert_not_called()

    def test_refresh_runs_before_drafting_and_publishing_inside_daily_lock(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = [
                "--env-file",
                str(root / ".env"),
                "--database",
                str(root / "instagram.sqlite3"),
                "daily",
                "--media",
                str(root / "media"),
            ]

            def refresh(*args):
                with (root / "media" / ".daily.lock").open("r+") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                events.append("refresh")
                return "not_due"

            def capture(*args):
                with (root / "media" / ".daily.lock").open("r+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                events.append("stories")
                return "stories_off"

            with (
                patch.object(cli.token_refresh, "tick", side_effect=refresh),
                patch.object(
                    cli, "_capture_followers", side_effect=lambda *a: events.append("followers") or "followers_off"
                ),
                patch.object(cli, "_capture_stories", side_effect=capture),
                patch.object(cli, "daily", side_effect=lambda *a: events.append("draft") or "skipped"),
                patch.object(cli.render, "render_due"),
                patch.object(cli.publish, "tick", side_effect=lambda *a: events.append("publish") or "publish_off"),
                patch.object(cli, "_post_reports", side_effect=lambda *a: events.append("reports") or "reports_off"),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.main(args), 0)
        self.assertEqual(events, ["refresh", "draft", "publish", "reports", "followers", "stories"])
        self.assertEqual(
            output.getvalue().strip(), "skipped; not_due; publish_off; stories_off; reports_off; followers_off"
        )

    def test_daily_reloads_config_after_concurrent_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text(f"{TOKEN_KEY}={OLD_TOKEN}\nINSTAGRAM_USER_ID=123\nINSTAGRAM_AUTO_PUBLISH=1\n")

            def refresh():
                env_install.upsert(env_file, {TOKEN_KEY: ROTATED_TOKEN, "INSTAGRAM_AUTO_PUBLISH": "0"})
                return {"access_token": NEW_TOKEN, "token_type": "bearer", "expires_in": 5184000}

            with (
                patch.dict(cli.os.environ, {}, clear=True),
                patch.object(GraphClient, "refresh_access_token", side_effect=refresh),
                patch.object(cli, "_capture_followers", return_value="followers_off") as followers,
                patch.object(cli, "_capture_stories", return_value="stories_off") as stories,
                patch.object(cli, "daily", return_value="skipped") as draft,
                patch.object(cli.render, "render_due"),
                patch.object(cli.publish, "tick", return_value="publish_off") as publish,
                patch.object(cli, "_post_reports", return_value="reports_off") as reports,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "--env-file",
                            str(env_file),
                            "--database",
                            str(root / "db"),
                            "daily",
                            "--media",
                            str(root / "media"),
                        ]
                    ),
                    0,
                )
            for stage in (followers, stories, draft, publish, reports):
                env = stage.call_args.args[1]
                self.assertEqual(env[TOKEN_KEY], ROTATED_TOKEN)
                self.assertEqual(env["INSTAGRAM_AUTO_PUBLISH"], "0")


if __name__ == "__main__":
    unittest.main()
