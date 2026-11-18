"""Refresh an Instagram Login long-lived token without exposing or replacing it on failure."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from . import env_install, storage
from .graph import GraphAPIError

TOKEN_KEY = "INSTAGRAM_ACCESS_TOKEN"
REFRESH_BEFORE = timedelta(days=30)
FAILURE_BACKOFF = timedelta(hours=24)
MAX_EXPIRES_IN = 366 * 24 * 60 * 60


class TokenRefreshError(RuntimeError):
    """A safe refresh error that never contains credentials, URLs or response bodies."""


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def should_refresh(state: dict, now: datetime) -> bool:
    """Refresh near the halfway point, with at most one failed API attempt per day."""
    attempted = _instant(state.get("last_attempt_at"))
    if state.get("last_outcome") == "failed" and attempted and now < attempted + FAILURE_BACKOFF:
        return False
    expires = _instant(state.get("expires_at"))
    return expires is None or now >= expires - REFRESH_BEFORE


def _validated(reply: dict[str, Any], now: datetime) -> tuple[str, dict[str, str], str]:
    if not isinstance(reply, dict) or reply.get("token_type") != "bearer":
        raise TokenRefreshError("Meta returned an invalid token refresh response")
    token = reply.get("access_token")
    seconds = reply.get("expires_in")
    if (
        not isinstance(token, str)
        or not token
        or isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or not 0 < seconds <= MAX_EXPIRES_IN
    ):
        raise TokenRefreshError("Meta returned an invalid token refresh response")
    try:
        values = env_install.parse(f"{TOKEN_KEY}={token}\n")
    except ValueError:
        raise TokenRefreshError("Meta returned an invalid access token") from None
    if values != {TOKEN_KEY: token}:
        raise TokenRefreshError("Meta returned an invalid access token")
    expires_at = (now + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    return token, values, expires_at


def _terminal(conn, state: dict, outcome: str) -> str:
    if state.get("last_outcome") != outcome:
        storage.record_token_status(conn, outcome)
    return outcome


def tick(conn, env: dict, env_file: Path, now: datetime | None = None, *, environ=None, client=None) -> str:
    """Refresh when due inside the caller's daily lock. Only the `.env` token is replaced."""
    now = now or datetime.now(timezone.utc)
    environ = os.environ if environ is None else environ
    state = storage.token_state(conn)
    if TOKEN_KEY in environ:
        return _terminal(conn, state, "externally_managed")
    current = env.get(TOKEN_KEY)
    if not current or not env.get("INSTAGRAM_USER_ID"):
        return _terminal(conn, state, "not_configured")
    if not current.startswith("IG"):
        return _terminal(conn, state, "unsupported")
    fingerprint = sha256(current.encode()).hexdigest()
    if state["token_fingerprint"] == fingerprint and not should_refresh(state, now):
        return "not_due"

    stamp = now.isoformat(timespec="seconds")
    try:
        if client is None:
            from .cli import graph_client

            client = graph_client(env)
        token, values, expires_at = _validated(client.refresh_access_token(), now)
        env_install.upsert(env_file, values, if_current={TOKEN_KEY: current})
    except env_install.EnvChangedError:
        return _terminal(conn, state, "token_changed")
    except (GraphAPIError, TokenRefreshError) as exc:
        error = str(exc)
    except (OSError, ValueError) as exc:
        error = type(exc).__name__
    else:
        # The current tick must use the persisted replacement even if its metadata write fails.
        env[TOKEN_KEY] = token
        try:
            storage.record_token_success(conn, expires_at, stamp, fingerprint=sha256(token.encode()).hexdigest())
        except sqlite3.Error:
            return "metadata_failed"
        return "refreshed"

    storage.record_token_failure(conn, stamp, error, fingerprint=fingerprint)
    return "failed"
