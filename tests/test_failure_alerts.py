import io
import json
import runpy
import sqlite3
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import notifications
from instagram_agent import cli as ig_cli
from instagram_agent import env_install, graph, storage
from octopus_energy import refresh as energy_refresh

REPO_ROOT = Path(__file__).resolve().parents[1]
ALERT_SCRIPT = runpy.run_path(str(REPO_ROOT / "scripts/alert-failure.py"))
TOKEN = "0123456789abcdef" * 4
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


class _Response(io.BytesIO):
    def __init__(self, status=200, body=b"{}"):
        super().__init__(body)
        self.status = status


class _Opener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _workflow_event(
    *,
    name="Deploy command centre",
    conclusion="failure",
    event="workflow_run",
    branch="main",
    sha="7a9b9eb1234567890abcdef1234567890abcdef1",
    repo="squalliram/personal-ai",
    head_repo="squalliram/personal-ai",
    run_id=12345,
):
    return {
        "repository": {"full_name": repo},
        "workflow_run": {
            "id": run_id,
            "name": name,
            "conclusion": conclusion,
            "event": event,
            "head_branch": branch,
            "head_sha": sha,
            "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
            "head_repository": {"full_name": head_repo},
        },
    }


class AlertScriptTests(unittest.TestCase):
    def test_build_alert_for_monitored_workflow_failures(self):
        alert = ALERT_SCRIPT["build_alert"](_workflow_event(), {})
        self.assertIsNotNone(alert)
        self.assertEqual(alert["outcome"], "failed")
        self.assertEqual(alert["source"], "jarvis")
        self.assertEqual(alert["title"], "Jarvis run failed: Deploy command centre (failure on main, 7a9b9eb)")
        self.assertEqual(alert["link"], "https://github.com/squalliram/personal-ai/actions/runs/12345")
        self.assertEqual(alert["workflow"], "Deploy command centre")
        self.assertEqual(alert["run_id"], 12345)

        timed_out = ALERT_SCRIPT["build_alert"](
            _workflow_event(name="Instagram baseline", conclusion="timed_out", event="repository_dispatch"), {}
        )
        self.assertIsNotNone(timed_out)
        self.assertIn("Instagram baseline (timed_out on main", timed_out["title"])

        main_test = ALERT_SCRIPT["build_alert"](_workflow_event(name="Test", event="push", branch="main"), {})
        self.assertIsNotNone(main_test)

    def test_build_alert_skips_non_failures_prs_forks_and_non_main_tests(self):
        for event in (
            _workflow_event(conclusion="success"),
            _workflow_event(conclusion="cancelled"),
            _workflow_event(conclusion="skipped"),
            _workflow_event(event="pull_request"),
            _workflow_event(head_repo="attacker/personal-ai"),
            _workflow_event(name="Test", event="push", branch="feature"),
            _workflow_event(name="Test", event="pull_request", branch="main"),
        ):
            with self.subTest(event=event["workflow_run"]):
                self.assertIsNone(ALERT_SCRIPT["build_alert"](event, {}))

    def test_build_alert_for_forced_failure_dispatch(self):
        env = {"GITHUB_REPOSITORY": "squalliram/personal-ai", "GITHUB_RUN_ID": "999"}
        event = {"action": "jarvis-alert-test", "client_payload": {"source": "Cron smoke check"}}
        alert = ALERT_SCRIPT["build_alert"](event, env)
        self.assertEqual(alert["title"], "Jarvis run failed: Cron smoke check (forced failure test)")
        self.assertEqual(alert["link"], "https://github.com/squalliram/personal-ai/actions/runs/999")
        self.assertTrue(alert["forced_test"])

    def test_dispatch_sends_notify_owner_and_optional_webhook(self):
        sent_notify = []
        sent_webhook = []

        class FakeNotifyOwner:
            @staticmethod
            def ssh_target(env):
                return None

            @staticmethod
            def send(env):
                sent_notify.append(env)
                return 42

        alert = ALERT_SCRIPT["build_alert"](_workflow_event(), {})
        env = {
            "NOTIFY_INGEST_TOKEN": TOKEN,
            "JARVIS_ALERT_WEBHOOK_URL": "https://hooks.example.test/devin",
            "JARVIS_ALERT_WEBHOOK_SECRET": "supersecret123",
        }
        code = ALERT_SCRIPT["dispatch"](
            alert,
            env,
            notify_owner=FakeNotifyOwner,
            webhook_sender=lambda url, body, secret="": sent_webhook.append((url, body, secret)),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(sent_notify), 1)
        self.assertEqual(sent_notify[0]["NOTIFY_OUTCOME"], "failed")
        self.assertEqual(sent_notify[0]["NOTIFY_TITLE"], alert["title"])
        self.assertEqual(sent_notify[0]["NOTIFY_LINK"], alert["link"])
        self.assertEqual(len(sent_webhook), 1)
        self.assertEqual(sent_webhook[0][0], "https://hooks.example.test/devin")
        self.assertEqual(sent_webhook[0][2], "supersecret123")

    def test_dispatch_still_delivers_webhook_when_notify_owner_fails(self):
        sent_webhook = []

        class FailingNotifyOwner:
            @staticmethod
            def ssh_target(env):
                return ("host.example", "acct", "22")

            @staticmethod
            def relay(env, target):
                return 1

        alert = ALERT_SCRIPT["build_alert"](_workflow_event(), {})
        env = {
            "NOTIFY_INGEST_TOKEN": TOKEN,
            "CPANEL_SSH_HOST": "host.example",
            "CPANEL_SSH_USERNAME": "acct",
            "JARVIS_ALERT_WEBHOOK_URL": "https://hooks.example.test/devin",
        }
        code = ALERT_SCRIPT["dispatch"](
            alert,
            env,
            notify_owner=FailingNotifyOwner,
            webhook_sender=lambda url, body, secret="": sent_webhook.append((url, body, secret)),
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(sent_webhook), 1)

    def test_workflow_covers_expected_runs_without_self_trigger_or_untrusted_interpolation(self):
        workflow = (REPO_ROOT / ".github/workflows/alert-on-failure.yml").read_text(encoding="utf-8")
        for expected in (
            '"Deploy command centre"',
            '"Deploy protected energy dashboard"',
            '"Instagram baseline"',
            '"CodeRabbit findings log"',
            '"Test"',
            "jarvis-alert-test",
        ):
            self.assertIn(expected, workflow)
        self.assertNotIn('"Alert on run failure"', workflow)
        jobs_section = workflow.split("jobs:", 1)[1]
        self.assertNotIn("inputs.source", jobs_section)
        self.assertNotIn("client_payload", jobs_section)


class NotificationWebhookTests(unittest.TestCase):
    def test_notify_fires_webhook_only_for_new_failures(self):
        delivered = []
        with TemporaryDirectory() as directory:
            env = {
                "COMMAND_CENTRE_NOTIFICATIONS_DB": str(Path(directory) / "n.sqlite3"),
                "JARVIS_ALERT_WEBHOOK_URL": "https://hooks.example.test/jarvis",
                "JARVIS_ALERT_WEBHOOK_SECRET": "secret-token-123",
            }
            notifications.notify(
                "instagram",
                "ok",
                "All good",
                env=env,
                now=NOW,
                webhook=lambda u, b, s: delivered.append((u, b, s)),
            )
            self.assertEqual(delivered, [])

            notifications.notify(
                "instagram",
                "failed",
                "Cron failed",
                "https://iramkhan.com/command-centre/#instagram",
                key="k-fail",
                env=env,
                now=NOW,
                webhook=lambda u, b, s: delivered.append((u, b, s)),
            )
            notifications.notify(
                "instagram",
                "failed",
                "Cron failed",
                "https://iramkhan.com/command-centre/#instagram",
                key="k-fail",
                env=env,
                now=NOW,
                webhook=lambda u, b, s: delivered.append((u, b, s)),
            )
            self.assertEqual(len(delivered), 1)
            url, body, secret = delivered[0]
            self.assertEqual(url, "https://hooks.example.test/jarvis")
            self.assertEqual(secret, "secret-token-123")
            self.assertEqual(body["outcome"], "failed")
            self.assertEqual(body["source"], "instagram")
            self.assertEqual(body["title"], "Cron failed")

    def test_send_webhook_posts_json_with_bearer_and_refuses_http(self):
        opener = _Opener(_Response(202))
        notifications.send_webhook(
            "https://hooks.example.test/jarvis",
            {"title": "Boom"},
            secret="tok-secret-123",
            opener=opener,
        )
        (request,) = opener.requests
        self.assertEqual(request.get_header("Authorization"), "Bearer tok-secret-123")
        self.assertEqual(json.loads(request.data), {"title": "Boom"})

        with self.assertRaises(notifications.NotifyError):
            notifications.send_webhook("http://hooks.example.test/jarvis", {"title": "Boom"}, opener=opener)

        err = urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"fail"))
        with self.assertRaises(notifications.NotifyError) as caught:
            notifications.send_webhook(
                "https://hooks.example.test/jarvis",
                {"title": "Boom"},
                secret="tok-secret-123",
                opener=_Opener(err),
            )
        self.assertNotIn("tok-secret-123", str(caught.exception))

    def test_env_install_accepts_and_validates_webhook_settings(self):
        parsed = env_install.parse(
            "JARVIS_ALERT_WEBHOOK_URL=https://hooks.example.test/a/b?x=1\n"
            "JARVIS_ALERT_WEBHOOK_SECRET=secret_token_12345\n"
        )
        self.assertEqual(parsed["JARVIS_ALERT_WEBHOOK_URL"], "https://hooks.example.test/a/b?x=1")
        self.assertEqual(parsed["JARVIS_ALERT_WEBHOOK_SECRET"], "secret_token_12345")
        with self.assertRaises(ValueError):
            env_install.parse("JARVIS_ALERT_WEBHOOK_URL=http://insecure.example/hook\n")
        with self.assertRaises(ValueError):
            env_install.parse("JARVIS_ALERT_WEBHOOK_SECRET=short\n")


class HostCronFailureAlertTests(unittest.TestCase):
    def _rows(self, db_path: Path) -> list[dict]:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows, _ = notifications.recent(conn, limit=20)
            return rows

    def test_story_insights_followers_and_post_reports_alert_on_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "ig.sqlite3"
            notify_db = root / "notifications.sqlite3"
            env = {
                "INSTAGRAM_ACCESS_TOKEN": "IG_TOKEN",
                "INSTAGRAM_USER_ID": "12345",
                "COMMAND_CENTRE_NOTIFICATIONS_DB": str(notify_db),
            }
            conn = storage.connect(db)
            try:
                with mock.patch.object(ig_cli.story_insights, "capture", side_effect=graph.GraphAPIError("boom")):
                    result = ig_cli._capture_stories(conn, env, NOW)
                self.assertEqual(result, "Story insights failed (GraphAPIError)")

                with mock.patch.object(ig_cli, "graph_client", side_effect=graph.GraphAPIError("boom")):
                    result = ig_cli._capture_followers(conn, env, NOW)
                self.assertEqual(result, "follower snapshot failed (GraphAPIError)")

                with mock.patch.object(ig_cli.post_report, "tick", side_effect=RuntimeError("boom")):
                    result = ig_cli._post_reports(conn, env, NOW)
                self.assertEqual(result, "reports failed (RuntimeError)")
            finally:
                conn.close()

            rows = self._rows(notify_db)
            titles = {row["title"] for row in rows}
            self.assertIn("Story insights capture failed (GraphAPIError)", titles)
            self.assertIn("Instagram follower snapshot failed (GraphAPIError)", titles)
            self.assertIn("Instagram post reports failed (RuntimeError)", titles)
            self.assertTrue(all(row["outcome"] == "failed" for row in rows))

    def test_daily_cron_crash_and_token_refresh_failure_alert(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "ig.sqlite3"
            media = root / "media"
            notify_db = root / "notifications.sqlite3"
            env_file = root / ".env"
            env_file.write_text(f"COMMAND_CENTRE_NOTIFICATIONS_DB={notify_db}\n", encoding="utf-8")

            with (
                mock.patch.object(ig_cli, "_now", return_value=NOW),
                mock.patch.object(ig_cli.token_refresh, "tick", return_value="failed"),
                mock.patch.object(ig_cli.content_plan, "ensure_week", side_effect=RuntimeError("db broken")),
            ):
                code = ig_cli.main(["--env-file", str(env_file), "--database", str(db), "daily", "--media", str(media)])
            self.assertEqual(code, 1)
            titles = {row["title"] for row in self._rows(notify_db)}
            self.assertIn("Instagram token refresh failed", titles)
            self.assertIn("Instagram cron crashed (RuntimeError)", titles)

    def test_energy_refresh_failure_notifies_when_configured(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            notify_db = root / "notifications.sqlite3"
            (root / ".env").write_text(f"COMMAND_CENTRE_NOTIFICATIONS_DB={notify_db}\n", encoding="utf-8")
            energy_refresh._notify_failure(root, {}, "superseded_release")
            self.assertFalse(notify_db.exists())
            energy_refresh._notify_failure(root, {}, "sync_failed")
            energy_refresh._notify_failure(root, {}, "partial")
            rows = self._rows(notify_db)
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {row["title"] for row in rows},
                {"Energy refresh failed (sync_failed)", "Energy refresh failed (partial)"},
            )
            self.assertTrue(all(row["source"] == "energy" and row["outcome"] == "failed" for row in rows))


if __name__ == "__main__":
    unittest.main()
