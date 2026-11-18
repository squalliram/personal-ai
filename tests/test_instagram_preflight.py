import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from instagram_agent import cli, preflight, publish, storage
from instagram_agent.graph import GraphAPIError

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
DRAFT = "a" * 16
OTHER = "b" * 16
TOKEN = "IG_TEST_PREFLIGHT"
JPEG = b"\xff\xd8\xff" + b"image" * 8


class FakeGraph:
    def __init__(self, account=None, error=None):
        self.account = {"id": "123", "username": "instafoodielove"} if account is None else account
        self.error = error
        self.get_calls = []
        self.post_calls = []

    def get(self, path, params=None):
        self.get_calls.append((path, params))
        if self.error:
            raise self.error
        return self.account

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        raise AssertionError("preflight must not make Graph POST calls")


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = storage.connect(self.root / "instagram.sqlite3")
        self.addCleanup(self.conn.close)
        self.media = self.root / "drafts"
        self.public = self.root / "public"
        self.env = {
            "INSTAGRAM_ACCESS_TOKEN": TOKEN,
            "INSTAGRAM_USER_ID": "123",
            "INSTAGRAM_AUTO_PUBLISH": "0",
            "INSTAGRAM_PUBLIC_DIR": str(self.public),
            "INSTAGRAM_PUBLIC_URL": "https://iramkhan.com/ig-media",
        }

    def add(self, draft_id=DRAFT, *, score=80, slot="2026-11-15T18:00+00:00", caption="Save this.", slides=3):
        with self.conn:
            storage.insert_draft(
                self.conn,
                {
                    "draft_id": draft_id,
                    "category": "coffee",
                    "topic": "Flat whites",
                    "slides": [{"text": f"Slide {n}", "file": f"slide-{n}.jpg"} for n in range(1, slides + 1)],
                    "caption": caption,
                    "hashtags": ["#coffee"],
                    "review_score": score,
                    "scheduled_for": slot,
                },
                "2026-11-15",
                "2026-11-15T10:00:00+00:00",
            )
        folder = self.media / draft_id
        folder.mkdir(parents=True, exist_ok=True)
        for number in range(1, slides + 1):
            (folder / f"slide-{number}.jpg").write_bytes(JPEG)
        return draft_id

    def preflight(self, *, env=None, client=None, now=NOW, draft_id=DRAFT):
        return preflight.run(
            self.conn,
            self.env if env is None else env,
            self.media,
            now,
            draft_id=draft_id,
            client=FakeGraph() if client is None else client,
        )

    def check(self, report, name):
        return next(item for item in report["checks"] if item["name"] == name)

    def test_ready_with_auto_publish_off_and_no_writes(self):
        self.add()
        graph = FakeGraph()
        report = self.preflight(client=graph)
        self.assertTrue(report["ok"], report)
        self.assertFalse(report["auto_publish"])
        self.assertEqual(report["eligible_drafts"], [DRAFT])
        self.assertEqual(graph.get_calls, [("123", {"fields": "id,username"})])
        self.assertEqual(graph.post_calls, [])
        self.assertFalse(self.public.exists())
        self.assertNotIn(TOKEN, json.dumps(report))

    def test_wrong_account_fails_closed(self):
        self.add()
        for account in ({"id": "999", "username": "instafoodielove"}, {"id": "123", "username": "other"}):
            with self.subTest(account=account):
                report = self.preflight(client=FakeGraph(account))
                self.assertFalse(report["ok"])
                self.assertFalse(self.check(report, "account")["ok"])

    def test_missing_or_non_numeric_credentials_skip_meta(self):
        self.add()
        for env in ({}, {**self.env, "INSTAGRAM_USER_ID": "not-numeric"}):
            with self.subTest(env=env):
                graph = FakeGraph()
                report = self.preflight(env=env, client=graph)
                self.assertFalse(report["ok"])
                self.assertFalse(self.check(report, "credentials")["ok"])
                self.assertEqual(graph.get_calls, [])

    def test_weak_rejected_and_stale_drafts_are_not_candidates(self):
        self.add(score=60)
        report = self.preflight()
        self.assertFalse(self.check(report, "draft_eligibility")["ok"])
        self.assertEqual(report["eligible_drafts"], [])
        with self.conn:
            storage.decide_draft(self.conn, DRAFT, "rejected", NOW.isoformat(timespec="seconds"))
        report = self.preflight()
        self.assertFalse(self.check(report, "draft_eligibility")["ok"])

        other = storage.connect(self.root / "other.sqlite3")
        self.addCleanup(other.close)
        self.conn = other
        self.add(slot="2026-11-15T09:00+00:00")
        report = self.preflight(now=NOW + timedelta(hours=4, seconds=1))
        self.assertFalse(self.check(report, "publish_window")["ok"])
        self.assertEqual(report["eligible_drafts"], [])

    def test_png_missing_and_malformed_media_fail_clearly(self):
        self.add()
        slide = self.media / DRAFT / "slide-2.jpg"
        slide.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image")
        report = self.preflight()
        self.assertFalse(self.check(report, "draft_media")["ok"])
        self.assertIn("not JPEG", self.check(report, "draft_media")["detail"])
        slide.unlink()
        report = self.preflight()
        self.assertIn("missing", self.check(report, "draft_media")["detail"])
        with self.conn:
            self.conn.execute("UPDATE drafts SET slides='not-json' WHERE draft_id=?", (DRAFT,))
        report = self.preflight()
        self.assertIn("invalid", self.check(report, "draft_media")["detail"])

    def test_unsafe_public_configuration_fails(self):
        self.add()
        for url in (
            "http://iramkhan.com/ig-media",
            "HTTPS://iramkhan.com/ig-media",
            " https://iramkhan.com/ig-media",
            "https://bad host/ig-media",
            "https://[bad",
            "https://user@iramkhan.com/ig-media",
            "https://iramkhan.com/ig-media?",
            "https://iramkhan.com/ig-media#",
            "https://iramkhan.com:0/ig-media",
            "https://iramkhan.com:bad/ig-media",
            "https://iramkhan.com:99999/ig-media",
        ):
            with self.subTest(url=url):
                env = {**self.env, "INSTAGRAM_PUBLIC_URL": url}
                report = self.preflight(env=env)
                self.assertFalse(report["ok"])
                self.assertFalse(self.check(report, "public_media")["ok"])
                self.assertFalse(publish.ready({**env, "INSTAGRAM_AUTO_PUBLISH": "1"}))
        env = {**self.env, "INSTAGRAM_PUBLIC_URL": "https://iramkhan.com:8443/ig-media"}
        report = self.preflight(env=env)
        self.assertTrue(report["ok"], report)
        self.assertTrue(self.check(report, "public_media")["ok"])
        self.assertTrue(publish.ready({**env, "INSTAGRAM_AUTO_PUBLISH": "1"}))
        target = self.root / "target"
        target.mkdir()
        self.public.symlink_to(target, target_is_directory=True)
        report = self.preflight()
        self.assertFalse(self.check(report, "public_media")["ok"])

    def test_veto_publish_time_is_displayed_in_london_during_bst(self):
        self.add(slot="2026-07-15T12:00+01:00")
        with self.conn:
            self.conn.execute("UPDATE drafts SET created_at='2026-07-15T10:30:00+00:00' WHERE draft_id=?", (DRAFT,))
        report = self.preflight(now=datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc))
        self.assertIn("2026-07-15T12:30+01:00", self.check(report, "publish_window")["detail"])

    def test_other_eligible_drafts_must_be_resolved(self):
        self.add()
        self.add(OTHER, slot="2026-11-15T19:00+00:00")
        report = self.preflight()
        self.assertFalse(report["ok"])
        self.assertFalse(self.check(report, "single_candidate")["ok"])
        self.assertEqual(report["eligible_drafts"], [DRAFT, OTHER])

    def test_photo_post_with_one_image_is_ready(self):
        self.add(slides=1)
        report = self.preflight()
        self.assertTrue(report["ok"], report)

    def test_reel_held_for_approval_is_not_a_candidate_until_approved(self):
        self.add()
        self.add(OTHER, slot="2026-11-15T19:00+00:00")
        with self.conn:
            self.conn.execute("UPDATE drafts SET kind='reel', reel=? WHERE draft_id=?", ('{"hold":true}', OTHER))
        self.assertEqual(self.preflight()["eligible_drafts"], [DRAFT])
        with self.conn:
            storage.decide_draft(self.conn, OTHER, "approved", NOW.isoformat(timespec="seconds"))
        report = self.preflight(draft_id=OTHER)
        self.assertEqual(report["eligible_drafts"], [DRAFT, OTHER])
        self.assertFalse(self.check(report, "draft_media")["ok"])

    def test_graph_error_is_safe_and_never_retries_as_post(self):
        self.add()
        graph = FakeGraph(error=GraphAPIError("Graph API request failed (HTTP 400, code 190)"))
        report = self.preflight(client=graph)
        self.assertFalse(report["ok"])
        self.assertFalse(self.check(report, "account")["ok"])
        self.assertEqual(graph.post_calls, [])
        self.assertNotIn(TOKEN, json.dumps(report))

    def test_caption_and_hashtags_must_fit_meta_limit(self):
        self.add(caption="x" * 2200)
        report = self.preflight()
        self.assertFalse(self.check(report, "draft_media")["ok"])
        self.assertIn("2200", self.check(report, "draft_media")["detail"])


class PreflightCliTests(unittest.TestCase):
    def test_cli_starts_in_a_fresh_process(self):
        result = subprocess.run(
            [sys.executable, "-m", "instagram_agent", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preflight", result.stdout)

    def test_cli_uses_read_only_preflight_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "instagram.sqlite3"
            storage.connect(database).close()
            args = [
                "--database",
                str(database),
                "preflight",
                "--draft",
                DRAFT,
                "--media",
                str(root / "drafts"),
            ]
            with (
                patch.object(cli.preflight, "run", return_value={"ok": True, "checks": []}) as run,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.main(args), 0)
                self.assertEqual(run.call_args.kwargs["draft_id"], DRAFT)
                self.assertEqual(json.loads(output.getvalue())["ok"], True)

    def test_cli_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "absent" / "instagram.sqlite3"
            with patch.object(cli.preflight, "run") as run, redirect_stderr(io.StringIO()) as output:
                self.assertEqual(cli.main(["--database", str(database), "preflight", "--draft", DRAFT]), 1)
            self.assertFalse(database.parent.exists())
            self.assertIn("existing, initialized Instagram database", output.getvalue())
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
