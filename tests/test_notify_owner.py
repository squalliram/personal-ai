import io
import json
import runpy
import subprocess
import sys
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

    def test_workflow_inputs_come_from_the_event_file(self):
        with TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"inputs": {"title": "From dispatch", "outcome": "failed", "link": ""}}))
            env = SCRIPT["with_event_inputs"]({"NOTIFY_INGEST_TOKEN": TOKEN, "GITHUB_EVENT_PATH": str(event)})
        self.assertEqual(SCRIPT["payload"](env), {"source": "jarvis", "outcome": "failed", "title": "From dispatch"})
        with TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"action": "notify-owner", "client_payload": {"title": "Via API"}}))
            env = SCRIPT["with_event_inputs"]({"NOTIFY_INGEST_TOKEN": TOKEN, "GITHUB_EVENT_PATH": str(event)})
        self.assertEqual(SCRIPT["payload"](env), {"source": "jarvis", "outcome": "ok", "title": "Via API"})
        self.assertIs(SCRIPT["with_event_inputs"](ENV), ENV)
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/notify-owner.yml").read_text()
        self.assertNotIn("inputs.title", workflow.split("jobs:", 1)[1])

    def test_with_ssh_configured_the_script_reruns_on_the_host_with_secrets_on_stdin(self):
        calls = []

        def run(argv, input, timeout):
            calls.append((argv, json.loads(input), input.decode()))
            return subprocess.CompletedProcess(argv, 0)

        ssh = {"CPANEL_SSH_HOST": "host.example", "CPANEL_SSH_USERNAME": "acct", "CPANEL_SSH_PRIVATE_KEY": "KEY"}
        env = {**ENV, **ssh}
        target = SCRIPT["ssh_target"](env)
        self.assertEqual(target, ("host.example", "acct", "22"))
        self.assertEqual(SCRIPT["relay"](env, target, run), 0)
        ((argv, forwarded, _),) = calls
        self.assertEqual(argv[-2], "acct@host.example")
        self.assertTrue(argv[-1].startswith("/opt/alt/python312/bin/python3 -c "))
        self.assertTrue(argv[-1].endswith(" --stdin"))
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(forwarded["NOTIFY_INGEST_TOKEN"], TOKEN)
        self.assertEqual(forwarded["NOTIFY_TITLE"], "Posted today's reel")
        self.assertNotIn("CPANEL_SSH_PRIVATE_KEY", forwarded)
        for long_input in (
            {"NOTIFY_TITLE": "x" * 70000},
            {"NOTIFY_TITLE": "\u00e9" * 12000},
            {"NOTIFY_OUTCOME": " " * 70000 + "ok"},
        ):
            long_env = {**env, **long_input}
            SCRIPT["relay"](long_env, target, run)
            host_env = SCRIPT["from_stdin"](calls.pop()[2])
            self.assertEqual(SCRIPT["payload"](host_env), SCRIPT["payload"](long_env))
        with self.assertRaises(ValueError):
            SCRIPT["relay"]({**env, "COMMAND_CENTRE_URL": "https://a.example/" + "a" * 70000}, target, run)
        with self.assertRaises(ValueError):
            SCRIPT["from_stdin"](json.dumps({"NOTIFY_TITLE": "x" * 70000}))

        self.assertIsNone(SCRIPT["ssh_target"](ENV))
        with self.assertRaises(ValueError):
            SCRIPT["ssh_target"]({**env, "CPANEL_SSH_HOST": "host; rm -rf /"})
        with self.assertRaises(ValueError):
            SCRIPT["relay"]({**env, "NOTIFY_TITLE": ""}, target, run)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(SCRIPT["Unconfirmed"]):
            SCRIPT["relay"](env, target, lambda argv, input, timeout: subprocess.CompletedProcess(argv, 255))

        def hang(argv, input, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        with self.assertRaises(SCRIPT["Unconfirmed"]):
            SCRIPT["relay"](env, target, hang)

    def test_host_side_reads_stdin_and_refuses_bad_input_without_a_request(self):
        self.assertEqual(
            SCRIPT["from_stdin"](json.dumps({**ENV, "CPANEL_SSH_HOST": "x"})),
            ENV,
        )
        with self.assertRaises(ValueError):
            SCRIPT["from_stdin"]("[1]")
        source = (Path(__file__).resolve().parents[1] / "scripts/notify-owner.py").read_text()
        result = subprocess.run(
            [sys.executable, "-c", source, "--stdin"],
            input=json.dumps({**ENV, "NOTIFY_TITLE": ""}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Notification not sent: NOTIFY_TITLE is empty", result.stderr)

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
