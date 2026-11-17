import io
import json
import runpy
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

from command_centre_fixtures import Client, Clock, make_settings

from command_centre.app import CommandCentreApp

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/notify-owner.py"))
TOKEN = "0123456789abcdef" * 4
ENV = {"NOTIFY_INGEST_TOKEN": TOKEN, "NOTIFY_TITLE": "Posted today's reel", "NOTIFY_OUTCOME": "ok"}


class Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status


class Opener:
    def __init__(self, result):
        self.result, self.requests = result, []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(result := self.result, Exception):
            raise result
        return result


class NotifyOwnerTests(unittest.TestCase):
    def test_posts_bearer_json_to_the_hosted_ingest(self):
        opener = Opener(Response(201, {"id": 9}))
        self.assertEqual(SCRIPT["send"]({**ENV, "NOTIFY_LINK": "https://github.com/x/y/pull/1"}, opener), 9)
        (request,) = opener.requests
        self.assertEqual(request.full_url, "https://iramkhan.com/command-centre/api/v1/notifications")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request.data),
            {
                "source": "jarvis",
                "outcome": "ok",
                "title": "Posted today's reel",
                "link": "https://github.com/x/y/pull/1",
            },
        )

    def test_title_is_flattened_and_trimmed_to_the_ingest_limit(self):
        body = SCRIPT["payload"]({**ENV, "NOTIFY_TITLE": "line one\nline\ttwo " + "x" * 300})
        self.assertEqual(len(body["title"]), 200)
        self.assertTrue(body["title"].startswith("line one line two x"))
        self.assertTrue(body["title"].endswith("\u2026"))
        self.assertEqual(SCRIPT["payload"]({**ENV, "NOTIFY_OUTCOME": ""})["outcome"], "ok")

    def test_bad_inputs_are_refused_before_any_request(self):
        for env in (
            {**ENV, "NOTIFY_INGEST_TOKEN": ""},
            {**ENV, "NOTIFY_INGEST_TOKEN": "short"},
            {**ENV, "NOTIFY_TITLE": " \n "},
            {**ENV, "NOTIFY_OUTCOME": "info"},
            {**ENV, "NOTIFY_LINK": "http://example.com"},
            {**ENV, "COMMAND_CENTRE_URL": "http://example.com"},
        ):
            with self.subTest(env=env):
                opener = Opener(Response(201, {"id": 1}))
                with self.assertRaises(ValueError):
                    SCRIPT["send"](env, opener)
                self.assertEqual(opener.requests, [])

    def test_refusals_and_outages_fail_without_echoing_the_token(self):
        refused = urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b'{"error": "authentication_required"}'))
        for result, expected in (
            (refused, "HTTP 401 authentication_required"),
            (urllib.error.URLError("down"), "unreachable"),
            (Response(200, {"ok": True}), "Unexpected"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(RuntimeError) as caught:
                    SCRIPT["send"](ENV, Opener(result))
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_command_centre_accepts_the_payload(self):
        calls = []
        with TemporaryDirectory() as directory:
            root, clock = Path(directory), Clock()
            app = CommandCentreApp(
                make_settings(root, notify_ingest_token=TOKEN, notifications_db=root / "n.sqlite3"),
                allowed_hosts={"127.0.0.1:8770"},
                clock=clock,
                monotonic=clock,
                cron_reader=lambda: ("absent", ""),
                notify=lambda *args, **kwargs: calls.append(args) or 3,
            )
            body = SCRIPT["payload"]({**ENV, "NOTIFY_TITLE": "y" * 500, "NOTIFY_LINK": "https://example.com/a"})
            response = Client(app).request(
                "POST", "/api/v1/notifications", body=body, headers={"Authorization": f"Bearer {TOKEN}"}, origin=False
            )
        self.assertEqual(response.status, 201)
        self.assertEqual(calls[0][:2], ("jarvis", "ok"))
