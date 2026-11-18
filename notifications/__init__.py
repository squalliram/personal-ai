"""Owner notifications: every call stores an inbox row, and sends a Telegram message when a bot is set up.

`notify(source, outcome, title, link)` is for any agent or cron job in this repo. The Command Centre
Inbox reads the same database. Telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (the owner's own
chat) in the environment or `.env`. A notification problem never raises into the caller.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_DATABASE = Path("data/notifications.sqlite3")
DEFAULT_COMMAND_CENTRE_URL = "https://iramkhan.com/command-centre/"
OUTCOMES = ("ok", "failed", "info")
TELEGRAM_API = "https://api.telegram.org/"
TELEGRAM_TIMEOUT = 10
MAX_SEND_ATTEMPTS = 3
# A claimed send is someone else's until this passes, so overlapping calls never send a row twice.
CLAIM_LEASE = timedelta(seconds=TELEGRAM_TIMEOUT * 3)
# Older unsent rows retried per call, after the new one; a call stops at its first failed send.
RETRY_BATCH = 3
BOT_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}")
CHAT_ID = re.compile(r"-?[0-9]{1,20}")
SOURCE = re.compile(r"[a-z0-9_-]{1,40}")
SAFE_LINK = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*)?")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
ICONS = {"ok": "\u2705", "failed": "\u274c", "info": "\u2139\ufe0f"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'failed', 'info')),
    title TEXT NOT NULL,
    link TEXT,
    dedupe_key TEXT UNIQUE,
    delivery TEXT NOT NULL CHECK (delivery IN ('none', 'pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    claim TEXT,
    claimed_until TEXT,
    delivered_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS notifications_unsent ON notifications(delivery) WHERE delivery = 'pending';
"""


class NotifyError(RuntimeError):
    """A safe delivery error: never the bot token, URLs or response bodies."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NotifyError("Telegram redirected the request")


def safe_link(value: object) -> str | None:
    """An https link fit to store and show, or None."""
    if isinstance(value, str) and len(value) <= 500 and SAFE_LINK.fullmatch(value):
        return value
    return None


def command_centre_link(env: Mapping[str, str], section: str) -> str:
    base = safe_link(env.get("COMMAND_CENTRE_URL")) or DEFAULT_COMMAND_CENTRE_URL
    return f"{base.split('#', 1)[0].rstrip('/')}/#{section}"


def connect(path: Path = DEFAULT_DATABASE) -> sqlite3.Connection:
    if path.is_symlink():
        raise OSError("refusing a symlinked notifications database")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def recent(conn: sqlite3.Connection, limit: int = 25, offset: int = 0) -> tuple[list[dict], int]:
    """Newest first. Works on a read-only connection."""
    rows = conn.execute(
        "SELECT id, created_at, source, outcome, title, link, delivery FROM notifications "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    return [{**dict(row), "link": safe_link(row["link"])} for row in rows], total


def send_telegram(token: str, chat_id: str, text: str, *, timeout: float = TELEGRAM_TIMEOUT, opener=None) -> None:
    """One sendMessage call, no retries. Raises NotifyError."""
    opener = opener or build_opener(_NoRedirect())
    body = urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    request = Request(
        f"{TELEGRAM_API}bot{token}/sendMessage",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "personal-ai-notify/1.0"},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except NotifyError:
        raise
    except HTTPError as exc:
        raise NotifyError(f"Telegram request failed (HTTP {exc.code})") from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise NotifyError(f"Telegram request failed ({type(exc).__name__})") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise NotifyError("Telegram did not accept the message")


def _message(row) -> str:
    text = f"{ICONS.get(row['outcome'], '')} {row['title']}".strip()
    return f"{text}\n{row['link']}" if row["link"] else text


def _deliver(conn, row, token: str, chat_id: str, send: Callable, clock: Callable[[], datetime]) -> bool:
    now, claim = clock(), secrets.token_hex(8)
    with conn:
        claimed = conn.execute(
            "UPDATE notifications SET attempts=attempts+1, claim=?, claimed_until=? "
            "WHERE id=? AND delivery='pending' AND attempts<? AND (claimed_until IS NULL OR claimed_until<?)",
            (
                claim,
                (now + CLAIM_LEASE).isoformat(timespec="seconds"),
                row["id"],
                MAX_SEND_ATTEMPTS,
                now.isoformat(timespec="seconds"),
            ),
        ).rowcount
    if not claimed:
        return True
    try:
        send(token, chat_id, _message(row))
    except Exception as exc:
        error = str(exc) if isinstance(exc, NotifyError) else type(exc).__name__
        with conn:
            conn.execute(
                "UPDATE notifications SET error=?, claim=NULL, claimed_until=NULL, "
                "delivery=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END WHERE id=? AND claim=?",
                (error.replace(token, "[redacted]")[:200], MAX_SEND_ATTEMPTS, row["id"], claim),
            )
        return False
    with conn:
        conn.execute(
            "UPDATE notifications SET delivery='sent', delivered_at=?, claim=NULL, claimed_until=NULL, error=NULL "
            "WHERE id=? AND claim=?",
            (clock().isoformat(timespec="seconds"), row["id"], claim),
        )
    return True


def _env() -> dict[str, str]:
    from octopus_energy.cli import load_env

    return {**load_env(Path(".env")), **os.environ}


def notify(
    source: str,
    outcome: str,
    title: str,
    link: str | None = None,
    *,
    key: str | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    send: Callable[[str, str, str], None] = send_telegram,
) -> int | None:
    """Store a notification and push it to Telegram if configured. Returns its inbox ID, or None if not stored.

    `key` makes the call idempotent: a second notification with the same key is dropped (the first one's ID
    is returned).
    """
    try:
        env = _env() if env is None else env
        if not SOURCE.fullmatch(source) or outcome not in OUTCOMES:
            raise ValueError("invalid notification")
        title = CONTROL.sub(" ", str(title)).strip()[:300] or "(no title)"
        fixed = now is not None
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        clock = (
            (lambda: now) if fixed else (lambda: datetime.now(timezone.utc))
        )  # each send claims at the time it starts
        stamp = now.isoformat(timespec="seconds")
        token, chat_id = env.get("TELEGRAM_BOT_TOKEN", "").strip(), env.get("TELEGRAM_CHAT_ID", "").strip()
        configured = bool(token and chat_id)
        valid = configured and BOT_TOKEN.fullmatch(token) and CHAT_ID.fullmatch(chat_id)
        delivery, error = ("pending", None) if valid else ("failed", "invalid Telegram settings")
        if not configured:
            delivery, error = "none", None
        path = Path(env.get("COMMAND_CENTRE_NOTIFICATIONS_DB") or DEFAULT_DATABASE).expanduser()
        conn = connect(path)
        try:
            with conn:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO notifications(created_at,source,outcome,title,link,dedupe_key,delivery,error) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (stamp, source, outcome, title, safe_link(link), key, delivery, error),
                )
            if inserted.rowcount:
                stored = inserted.lastrowid
            else:
                stored = conn.execute("SELECT id FROM notifications WHERE dedupe_key=?", (key,)).fetchone()[0]
            if inserted.rowcount and valid:
                _send_queue(conn, inserted.lastrowid, stamp, token, chat_id, send, clock)
        finally:
            conn.close()
        return stored
    except Exception as exc:
        print(f"notifications: not stored ({type(exc).__name__})", file=sys.stderr)
        return None


def _send_queue(conn, new_id, stamp, token, chat_id, send, clock) -> None:
    """Send the new row and retry earlier pending ones. The row is already stored, so a failure here only
    leaves rows pending for the next call; it never hides the stored ID from the caller."""
    try:
        with conn:
            conn.execute(
                "UPDATE notifications SET delivery='failed', claim=NULL, claimed_until=NULL, error='send interrupted' "
                "WHERE delivery='pending' AND attempts>=? AND claimed_until<?",
                (MAX_SEND_ATTEMPTS, stamp),
            )
        queue = conn.execute("SELECT * FROM notifications WHERE id=?", (new_id,)).fetchall()
        queue += conn.execute(
            "SELECT * FROM notifications WHERE delivery='pending' AND attempts<? AND id<>? ORDER BY id LIMIT ?",
            (MAX_SEND_ATTEMPTS, new_id, RETRY_BATCH),
        ).fetchall()
        for row in queue:
            if not _deliver(conn, row, token, chat_id, send, clock):
                break
    except Exception as exc:
        print(f"notifications: stored, delivery deferred ({type(exc).__name__})", file=sys.stderr)
