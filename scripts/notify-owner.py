#!/usr/bin/env python3
"""Send one update to the owner's Command Centre Inbox and Telegram through the notification ingest.

Run by .github/workflows/notify-owner.yml, so callers (the Jarvis Builder Bot) never hold the token.
Reads NOTIFY_INGEST_TOKEN and optional COMMAND_CENTRE_URL from the environment, and the message from the
workflow_dispatch inputs or repository_dispatch client_payload in GITHUB_EVENT_PATH (title, outcome ok/failed,
link), so the text never appears in
the step's env listing of this public repo's run log; NOTIFY_TITLE / NOTIFY_OUTCOME / NOTIFY_LINK serve local
runs. Prints only the status and inbox ID, never the token or the message.

iramkhan.com drops connections from GitHub's runners, so when CPANEL_SSH_HOST is set the script runs itself on
the host over the deploy's SSH access (`--stdin`), passing the token and message on stdin, never in argv.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://iramkhan.com/command-centre"
SOURCE = "jarvis"
OUTCOMES = ("ok", "failed")
MAX_TITLE = 200
TOKEN = re.compile(r"[0-9a-fA-F]{64}")
SAFE_LINK = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*)?")
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
TIMEOUT = 30
HOST_PYTHON = "/opt/alt/python312/bin/python3"
FORWARDED = ("NOTIFY_INGEST_TOKEN", "NOTIFY_TITLE", "NOTIFY_OUTCOME", "NOTIFY_LINK", "COMMAND_CENTRE_URL")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def clean_title(value: str) -> str:
    title = " ".join(CONTROL.sub(" ", value).split())
    return title if len(title) <= MAX_TITLE else title[: MAX_TITLE - 1].rstrip() + "\u2026"


def payload(env: dict[str, str]) -> dict[str, str]:
    title = clean_title(env.get("NOTIFY_TITLE", ""))
    if not title:
        raise ValueError("NOTIFY_TITLE is empty")
    outcome = env.get("NOTIFY_OUTCOME", "").strip() or "ok"
    if outcome not in OUTCOMES:
        raise ValueError(f"NOTIFY_OUTCOME must be one of {', '.join(OUTCOMES)}")
    body = {"source": SOURCE, "outcome": outcome, "title": title}
    link = env.get("NOTIFY_LINK", "").strip()
    if link:
        if len(link) > 500 or not SAFE_LINK.fullmatch(link):
            raise ValueError("NOTIFY_LINK must be an https link of at most 500 characters")
        body["link"] = link
    return body


def endpoint(env: dict[str, str]) -> str:
    base = env.get("COMMAND_CENTRE_URL", "").strip() or DEFAULT_URL
    if not SAFE_LINK.fullmatch(base):
        raise ValueError("COMMAND_CENTRE_URL must be an https link")
    return base.split("#", 1)[0].rstrip("/") + "/api/v1/notifications"


def build_request(env: dict[str, str]) -> urllib.request.Request:
    token = env.get("NOTIFY_INGEST_TOKEN", "").strip()
    if not TOKEN.fullmatch(token):
        raise ValueError("NOTIFY_INGEST_TOKEN is missing or malformed")
    return urllib.request.Request(
        endpoint(env),
        data=json.dumps(payload(env)).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )


def send(env: dict[str, str], opener=None) -> int:
    request = build_request(env)
    opener = opener or urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            status, raw = response.status, response.read(4096)
    except urllib.error.HTTPError as exc:
        error = ""
        try:
            error = json.loads(exc.read(4096)).get("error", "")
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"Command Centre refused the notification (HTTP {exc.code} {error})".strip()) from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Command Centre unreachable ({type(exc).__name__})") from None
    try:
        stored = json.loads(raw)["id"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(f"Unexpected Command Centre response (HTTP {status})") from None
    if status != 201 or not isinstance(stored, int):
        raise RuntimeError(f"Unexpected Command Centre response (HTTP {status})")
    return stored


def with_event_inputs(env: dict[str, str]) -> dict[str, str]:
    path = env.get("GITHUB_EVENT_PATH")
    if not path:
        return env
    with open(path, encoding="utf-8") as event:
        payload = json.load(event)
    inputs = payload.get("inputs") or payload.get("client_payload") or {}
    if not isinstance(inputs, dict):
        raise ValueError("event inputs must be an object")
    names = {"title": "NOTIFY_TITLE", "outcome": "NOTIFY_OUTCOME", "link": "NOTIFY_LINK"}
    return env | {var: str(inputs.get(name) or "") for name, var in names.items()}


def ssh_target(env: dict[str, str]) -> tuple[str, str, str] | None:
    host = env.get("CPANEL_SSH_HOST", "").strip()
    if not host:
        return None
    user = env.get("CPANEL_SSH_USERNAME", "").strip()
    port = env.get("CPANEL_SSH_PORT", "").strip() or "22"
    for value, pattern in (
        (host, r"[a-zA-Z0-9][a-zA-Z0-9.-]*"),
        (user, r"[a-zA-Z0-9_][a-zA-Z0-9_-]*"),
        (port, r"[0-9]{1,5}"),
    ):
        if not re.fullmatch(pattern, value):
            raise ValueError("CPANEL_SSH_HOST, CPANEL_SSH_USERNAME or CPANEL_SSH_PORT is malformed")
    return host, user, port


def relay(env: dict[str, str], target: tuple[str, str, str], run=subprocess.run) -> int:
    """Runs this script on the host, which can reach its own site; returns the host's exit code."""
    build_request(env)
    host, user, port = target
    command = shlex.join([HOST_PYTHON, "-c", Path(__file__).read_text(), "--stdin"])
    ssh = ["ssh", "-i", str(Path.home() / ".ssh/id_ed25519"), "-o", "BatchMode=yes"]
    ssh += ["-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20", "-p", port, f"{user}@{host}", command]
    forwarded = {name: env.get(name, "") for name in FORWARDED}
    forwarded["NOTIFY_TITLE"] = clean_title(forwarded["NOTIFY_TITLE"])
    result = run(ssh, input=json.dumps(forwarded).encode(), timeout=120)
    if result.returncode == 255:
        raise RuntimeError("SSH to the host failed")
    return result.returncode


def from_stdin(raw: str) -> dict[str, str]:
    data = json.loads(raw)
    if not isinstance(data, dict) or not all(isinstance(value, str) for value in data.values()):
        raise ValueError("stdin must be a JSON object of strings")
    return {name: data[name] for name in FORWARDED if name in data}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv == ["--stdin"]:
            env = from_stdin(sys.stdin.read(65536))
        else:
            env = with_event_inputs(dict(os.environ))
            if target := ssh_target(env):
                return relay(env, target)
        stored = send(env)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Notification not sent: {exc}", file=sys.stderr)
        return 1
    print(f"Notification stored (inbox ID {stored}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
