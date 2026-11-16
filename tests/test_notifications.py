import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from command_centre_fixtures import Client, Clock, make_app, make_settings

import notifications
from command_centre.security import Permission
from instagram_agent import env_install

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456789:AAH-secret_token_value_0123456789abc"
NOW = datetime(2026, 11, 12, 18, 5, tzinfo=timezone.utc)


class FakeTelegram:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    def __call__(self, token, chat_id, text):
        if self.fail:
            self.fail -= 1
            raise notifications.NotifyError("Telegram request failed (HTTP 502)")
        self.sent.append((token, chat_id, text))


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, result):
        self.result, self.requests = result, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return Response(json.dumps(self.result).encode())


class NotifierTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "notifications.sqlite3"
        self.env = {
            "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.db),
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_CHAT_ID": "-1001234",
        }

    def rows(self):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM notifications ORDER BY id")]


class NotifyTests(NotifierTestCase):
    def test_stores_and_sends_to_the_owner_chat(self):
        telegram = FakeTelegram()
        link = "https://iramkhan.com/command-centre/#instagram"
        self.assertTrue(notifications.notify("instagram", "ok", "Post is live", link, env=self.env, send=telegram))
        self.assertEqual(telegram.sent, [(TOKEN, "-1001234", f"\u2705 Post is live\n{link}")])
        row = self.rows()[0]
        self.assertEqual(
            (row["source"], row["outcome"], row["title"], row["link"]), ("instagram", "ok", "Post is live", link)
        )
        self.assertEqual((row["delivery"], row["attempts"], row["error"]), ("sent", 1, None))

    def test_without_telegram_settings_it_only_stores(self):
        telegram = FakeTelegram()
        env = {"COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.db)}
        self.assertTrue(notifications.notify("instagram", "info", "Hello", env=env, send=telegram))
        self.assertEqual(telegram.sent, [])
        self.assertEqual(self.rows()[0]["delivery"], "none")

    def test_malformed_telegram_settings_are_never_sent(self):
        telegram = FakeTelegram()
        env = {**self.env, "TELEGRAM_CHAT_ID": "@channel"}
        self.assertTrue(notifications.notify("instagram", "ok", "x", env=env, send=telegram))
        self.assertEqual(telegram.sent, [])
        self.assertEqual((self.rows()[0]["delivery"], self.rows()[0]["error"]), ("failed", "invalid Telegram settings"))

    def test_unsent_rows_are_retried_later_with_a_cap(self):
        sent = []

        def flaky(token, chat_id, text):
            if text.endswith(" a"):
                raise notifications.NotifyError("Telegram request failed (HTTP 502)")
            sent.append(text)

        for title in ("a", "b", "c", "d"):
            notifications.notify("instagram", "failed", title, env=self.env, send=flaky)
        first = self.rows()[0]
        self.assertEqual((first["delivery"], first["attempts"]), ("failed", notifications.MAX_SEND_ATTEMPTS))
        self.assertEqual(first["error"], "Telegram request failed (HTTP 502)")
        self.assertEqual(sent, ["\u274c b", "\u274c c", "\u274c d"])
        self.assertEqual([r["delivery"] for r in self.rows()[1:]], ["sent", "sent", "sent"])

    def test_pending_rows_go_out_once_telegram_is_back(self):
        telegram = FakeTelegram(fail=2)
        for title in ("a", "b", "c"):
            notifications.notify("instagram", "ok", title, env=self.env, send=telegram)
        self.assertEqual([t for _, _, t in telegram.sent], ["\u2705 c", "\u2705 a", "\u2705 b"])
        self.assertEqual([(r["delivery"], r["attempts"]) for r in self.rows()], [("sent", 2), ("sent", 2), ("sent", 1)])

    def test_a_failed_send_stops_the_retry_batch(self):
        telegram = FakeTelegram(fail=1)
        notifications.notify("instagram", "failed", "a", env=self.env, send=telegram)
        telegram.fail = 1
        notifications.notify("instagram", "failed", "b", env=self.env, send=telegram)
        self.assertEqual([(r["delivery"], r["attempts"]) for r in self.rows()], [("pending", 1), ("pending", 1)])

    def test_overlapping_calls_never_send_a_row_twice(self):
        sent = []

        def slow(token, chat_id, text):
            sent.append(text)
            if text.endswith(" a"):  # a second caller runs while "a" is still being sent
                notifications.notify("instagram", "ok", "b", env=self.env, send=slow)

        notifications.notify("instagram", "ok", "a", env=self.env, send=slow)
        self.assertEqual(sent, ["\u2705 a", "\u2705 b"])
        self.assertEqual([(r["delivery"], r["attempts"]) for r in self.rows()], [("sent", 1), ("sent", 1)])

    def test_an_abandoned_claim_is_retried_after_its_lease(self):
        telegram = FakeTelegram()
        notifications.notify("instagram", "ok", "a", env={"COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.db)})
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE notifications SET delivery='pending', attempts=1, claimed_until=?", (NOW.isoformat(),))
        notifications.notify("instagram", "ok", "b", env=self.env, now=NOW, send=telegram)
        self.assertEqual([t for _, _, t in telegram.sent], ["\u2705 b"])
        later = NOW + notifications.CLAIM_LEASE + notifications.CLAIM_LEASE
        notifications.notify("instagram", "ok", "c", env=self.env, now=later, send=telegram)
        self.assertEqual([t for _, _, t in telegram.sent], ["\u2705 b", "\u2705 c", "\u2705 a"])

    def test_a_sender_whose_claim_expired_cannot_overwrite_the_new_owner(self):
        later = NOW + notifications.CLAIM_LEASE + notifications.CLAIM_LEASE
        sent = []

        def stale(token, chat_id, text):
            sent.append(text)
            if sent == ["\u2705 a"]:
                notifications.notify("instagram", "ok", "b", env=self.env, now=later, send=stale)
                raise notifications.NotifyError("Telegram request failed (HTTP 502)")

        notifications.notify("instagram", "ok", "a", env=self.env, now=NOW, send=stale)
        self.assertEqual(sent, ["\u2705 a", "\u2705 b", "\u2705 a"])
        self.assertEqual([(r["delivery"], r["error"]) for r in self.rows()], [("sent", None), ("sent", None)])

    def test_an_abandoned_last_attempt_is_marked_failed(self):
        notifications.notify("instagram", "ok", "a", env={"COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.db)})
        attempts = notifications.MAX_SEND_ATTEMPTS
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE notifications SET delivery='pending', attempts=?, claimed_until=?", (attempts, NOW.isoformat())
            )
        notifications.notify("instagram", "ok", "b", env=self.env, now=NOW, send=FakeTelegram())
        self.assertEqual(self.rows()[0]["delivery"], "pending")
        later = NOW + notifications.CLAIM_LEASE + notifications.CLAIM_LEASE
        notifications.notify("instagram", "ok", "c", env=self.env, now=later, send=FakeTelegram())
        self.assertEqual((self.rows()[0]["delivery"], self.rows()[0]["error"]), ("failed", "send interrupted"))

    def test_errors_never_hold_the_token(self):
        def leaky(token, chat_id, text):
            raise notifications.NotifyError(f"boom {token}")

        notifications.notify("instagram", "ok", "x", env=self.env, send=leaky)
        self.assertNotIn(TOKEN, json.dumps(self.rows()))

        def crash(token, chat_id, text):
            raise RuntimeError(f"https://api.telegram.org/bot{token}/sendMessage")

        notifications.notify("instagram", "ok", "y", env=self.env, send=crash)
        self.assertEqual(self.rows()[1]["error"], "RuntimeError")

    def test_a_broken_notifier_never_breaks_the_caller(self):
        (self.root / "blocker").write_text("not a directory")
        env = {**self.env, "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.root / "blocker" / "n.sqlite3")}
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(notifications.notify("instagram", "ok", "x", env=env, send=FakeTelegram()))
            self.assertFalse(notifications.notify("Bad Source!", "ok", "x", env=self.env))
            self.assertFalse(notifications.notify("instagram", "great", "x", env=self.env))
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertNotIn("/", err.getvalue())

    def test_a_symlinked_database_is_refused(self):
        target = self.root / "elsewhere.sqlite3"
        target.write_bytes(b"")
        self.db.symlink_to(target)
        with redirect_stderr(io.StringIO()):
            self.assertFalse(notifications.notify("instagram", "ok", "x", env=self.env, send=FakeTelegram()))

    def test_keys_make_a_notification_idempotent(self):
        telegram = FakeTelegram()
        for _ in range(2):
            self.assertTrue(notifications.notify("instagram", "ok", "x", key="k1", env=self.env, send=telegram))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(telegram.sent), 1)

    def test_only_https_links_are_kept_and_titles_are_cleaned(self):
        for link in ("http://example.test/", "javascript:alert(1)", "https://exa mple.test/", "https://" + "a" * 600):
            notifications.notify("instagram", "info", "x", link, env={"COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.db)})
        self.assertEqual({r["link"] for r in self.rows()}, {None})
        notifications.notify(
            "instagram", "info", "a\x00b\x1bc" + "z" * 400, env={"COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.db)}
        )
        title = self.rows()[-1]["title"]
        self.assertTrue(title.startswith("a b c"))
        self.assertEqual(len(title), 300)

    def test_command_centre_link(self):
        self.assertEqual(
            notifications.command_centre_link({}, "instagram"), "https://iramkhan.com/command-centre/#instagram"
        )
        env = {"COMMAND_CENTRE_URL": "https://example.test/cc#old"}
        self.assertEqual(notifications.command_centre_link(env, "inbox"), "https://example.test/cc/#inbox")
        bad = {"COMMAND_CENTRE_URL": "http://example.test/"}
        self.assertEqual(notifications.command_centre_link(bad, "inbox"), "https://iramkhan.com/command-centre/#inbox")

    def test_reads_settings_from_the_env_file(self):
        (self.root / ".env").write_text(
            f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=42\nCOMMAND_CENTRE_NOTIFICATIONS_DB={self.db}\n"
        )
        telegram = FakeTelegram()
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(notifications.notify("instagram", "ok", "x", send=telegram))
        self.assertEqual(telegram.sent[0][:2], (TOKEN, "42"))


class SendTelegramTests(unittest.TestCase):
    def test_one_post_to_send_message_with_a_timeout(self):
        opener = FakeOpener({"ok": True, "result": {}})
        notifications.send_telegram(TOKEN, "42", "hi", opener=opener)
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn(b"chat_id=42", request.data)
        self.assertEqual(timeout, notifications.TELEGRAM_TIMEOUT)

    def test_errors_are_safe(self):
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        for result in (
            HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"{}")),
            URLError(url),
            TimeoutError(url),
            {"ok": False, "description": url},
        ):
            with self.subTest(result=type(result).__name__), self.assertRaises(notifications.NotifyError) as caught:
                notifications.send_telegram(TOKEN, "42", "hi", opener=FakeOpener(result))
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertNotIn("http", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_redirects_are_refused(self):
        handler = notifications._NoRedirect()
        with self.assertRaises(notifications.NotifyError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test/")


class InboxApiTests(NotifierTestCase):
    def client(self, permission=Permission.READ, db=None):
        settings = make_settings(self.root, permission_ceiling=permission, notifications_db=db or self.db)
        client = Client(make_app(settings, clock=Clock()))
        client.login()
        return client

    def test_lists_newest_first_with_pages(self):
        env = {"COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.db)}
        for n in range(12):
            notifications.notify("instagram", "ok" if n % 2 else "failed", f"n{n}", "https://example.test/x", env=env)
        before = self.db.read_bytes()
        response = self.client().request("GET", "/api/v1/notifications", query="page=2&per_page=10")
        self.assertEqual(response.status, 200)
        data = response.json()
        self.assertEqual((data["state"], data["page"], data["pages"], data["total"]), ("available", 2, 2, 12))
        self.assertEqual([e["title"] for e in data["entries"]], ["n1", "n0"])
        self.assertEqual(
            set(data["entries"][0]), {"id", "created_at", "source", "outcome", "title", "link", "delivery"}
        )
        self.assertEqual(self.db.read_bytes(), before)

    def test_missing_database_is_not_configured(self):
        data = self.client(db=self.root / "missing.sqlite3").request("GET", "/api/v1/notifications").json()
        self.assertEqual((data["state"], data["entries"], data["pages"]), ("not_configured", [], 1))

    def test_a_corrupt_database_is_unavailable(self):
        self.db.write_bytes(b"not sqlite at all" * 100)
        data = self.client().request("GET", "/api/v1/notifications").json()
        self.assertEqual(data["state"], "unavailable")

    def test_bad_parameters_are_refused(self):
        client = self.client()
        for query in ("page=0", "page=x", "per_page=7", "per_page=1000", "page=1000000"):
            with self.subTest(query=query):
                self.assertEqual(client.request("GET", "/api/v1/notifications", query=query).status, 400)

    def test_requires_a_session(self):
        settings = make_settings(self.root, notifications_db=self.db)
        self.assertEqual(Client(make_app(settings, clock=Clock())).request("GET", "/api/v1/notifications").status, 401)

    def test_settings_and_deploy_name_the_database(self):
        from command_centre.config import load_settings

        self.assertEqual(load_settings(self.root, {}).notifications_db, self.root / "data/notifications.sqlite3")
        deploy = (ROOT / "deploy/command-centre/deploy_host.py").read_text()
        self.assertIn('"NOTIFICATIONS_DB": str(checkout / "data/notifications.sqlite3")', deploy)
        self.assertIn("*.sqlite3", (ROOT / ".gitignore").read_text().splitlines())

    def test_the_inbox_reads_the_database_the_notifier_writes(self):
        from command_centre.config import load_settings

        custom = self.root / "elsewhere/notes.sqlite3"
        env = {"COMMAND_CENTRE_NOTIFICATIONS_DB": str(custom)}
        self.assertTrue(notifications.notify("instagram", "ok", "a", env=env))
        settings = load_settings(self.root, env)
        self.assertEqual(settings.notifications_db, custom)
        with closing(notifications.connect(settings.notifications_db)) as conn:
            self.assertEqual([r["title"] for r in notifications.recent(conn)[0]], ["a"])


class InboxFrontendTests(unittest.TestCase):
    def test_inbox_is_a_nav_section_rendered_safely(self):
        script = (ROOT / "command_centre/static/app.js").read_text()
        self.assertIn('["inbox", "Inbox"]', script)
        self.assertIn("api/v1/notifications?page=", script)
        self.assertIn('link.startsWith("https://")', script)
        self.assertRegex(script, re.compile(r"const VIEWS = \{[^}]*\binbox\b"))
        self.assertIn("if (epoch !== cacheEpoch || ticket !== generation) return;", script)


class TelegramDeployTests(unittest.TestCase):
    def test_env_install_accepts_telegram_settings(self):
        text = f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=-1001234\nCOMMAND_CENTRE_URL=https://iramkhan.com/command-centre/\n"
        self.assertEqual(
            env_install.parse(text),
            {
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "TELEGRAM_CHAT_ID": "-1001234",
                "COMMAND_CENTRE_URL": "https://iramkhan.com/command-centre/",
            },
        )

    def test_widened_values_are_per_key(self):
        for text in (
            "CLOUDFLARE_API_TOKEN=a:b\n",
            "TELEGRAM_BOT_TOKEN=not-a-token\n",
            "TELEGRAM_CHAT_ID=12a\n",
            "TELEGRAM_CHAT_ID=--1\n",
            "COMMAND_CENTRE_URL=http://example.test/\n",
            "COMMAND_CENTRE_URL=https://example.test/$(id)\n",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                env_install.parse(text)

    def test_workflow_passes_the_optional_settings(self):
        workflow = (ROOT / ".github/workflows/deploy-dashboard.yml").read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}", workflow)
        self.assertIn("TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}", workflow)
        self.assertIn("COMMAND_CENTRE_URL: ${{ vars.COMMAND_CENTRE_URL }}", workflow)


if __name__ == "__main__":
    unittest.main()
