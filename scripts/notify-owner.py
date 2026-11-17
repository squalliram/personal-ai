#!/usr/bin/env python3
"""Send one update to the owner's Command Centre Inbox and Telegram through the notification ingest.

Run by .github/workflows/notify-owner.yml, so callers (the Jarvis Builder Bot) never hold the token.
Reads NOTIFY_INGEST_TOKEN and optional COMMAND_CENTRE_URL from the environment, and the message from the
workflow_dispatch inputs or repository_dispatch client_payload in GITHUB_EVENT_PATH (title, outcome ok/failed,
link), so the text never appears in
the step's env listing of this public repo's run log; NOTIFY_TITLE / NOTIFY_OUTCOME / NOTIFY_LINK serve local
runs. Prints only the status and inbox ID, never the token or the message.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://iramkhan.com/command-centre"
SOURCE = "jarvis"
OUTCOMES = ("ok", "failed")
MAX_TITLE = 200
TOKEN = re.compile(r"[0-9a-fA-F]{64}")
SAFE_LINK = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*)?")
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
TIMEOUT = 30


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


def send(env: dict[str, str], opener=None) -> int:
    token = env.get("NOTIFY_INGEST_TOKEN", "").strip()
    if not TOKEN.fullmatch(token):
        raise ValueError("NOTIFY_INGEST_TOKEN is missing or malformed")
    request = urllib.request.Request(
        endpoint(env),
        data=json.dumps(payload(env)).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
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


def main() -> int:
    try:
        stored = send(with_event_inputs(dict(os.environ)))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Notification not sent: {exc}", file=sys.stderr)
        return 1
    print(f"Notification stored (inbox ID {stored}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
