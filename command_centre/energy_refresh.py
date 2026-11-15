"""Queue an energy refresh from the Command Centre with the dashboard's request-flag protocol.

This mirrors ``octopus_energy/refresh.php``: it only publishes a private ``request.flag`` holding a
random 32-hex request ID. It never runs the sync, calls Octopus or touches the energy database; the
existing runner claims the flag on its next cron tick.
"""

from __future__ import annotations

import math
import os
import re
import secrets
import stat
from datetime import datetime
from pathlib import Path

from .config import Settings
from .sources import _instant, energy_refresh_status

COOLDOWN_SECONDS = 300
ACCEPTED = ("pending", "already_running", "retry_queued")
_REQUEST_ID = re.compile(rb"[0-9a-f]{32}")


class Unavailable(Exception):
    pass


class _Vanished(Exception):
    pass


def _reply(code: int, state: str, retry_after: int = 0, attempt_at: str | None = None, request_id: str | None = None):
    body: dict = {"state": state, "retry_after": retry_after}
    if state in ACCEPTED:
        body.update(last_attempt_at=attempt_at, request_id=request_id)
    headers = [("Retry-After", str(retry_after))] if retry_after > 0 else []
    return code, body, headers


def _status(settings: Settings) -> dict:
    state, status = energy_refresh_status(settings)
    if state == "missing":
        return {"last_attempt_at": None, "request_id": None}
    if status is None:
        raise Unavailable
    return status


def _regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode)


def _read_id(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise _Vanished from None
    except OSError:
        raise Unavailable from None
    with os.fdopen(fd, "rb") as source:
        payload = source.read(33)
    # Legacy or malformed payloads are still served by the runner, but cannot identify a request.
    return payload.decode("ascii") if _REQUEST_ID.fullmatch(payload) else None


def _existing(settings: Settings, status: dict, flag: Path, running: Path):
    # The runner may rename the selected flag while this request is reading it. Re-resolve once.
    for resolution in range(2):
        path = flag if _regular(flag) else running if _regular(running) else None
        active = status.get("request_state") == "running" or status.get("result") == "running"
        attempt = status.get("last_attempt_at")
        if path is not None:
            try:
                request_id = _read_id(path)
            except _Vanished:
                if resolution:
                    raise Unavailable from None
                status = _status(settings)
                continue
            matches = request_id == status.get("request_id")
            if path == running and active and matches and attempt:
                return _reply(202, "already_running", 0, attempt, request_id)
            if (
                matches
                and status.get("request_state") == "pending"
                and status.get("result") in ("no_data", "partial", "failed")
                and attempt
            ):
                return _reply(202, "retry_queued", 0, attempt, request_id)
            return _reply(202, "pending", 0, attempt, request_id)
        if active:
            if not attempt:
                raise Unavailable
            return _reply(202, "already_running", 0, attempt, status.get("request_id"))
        return None
    raise Unavailable


def _publish(directory: Path, flag: Path, request_id: str) -> bool:
    temporary = directory / f".request-{request_id}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(request_id.encode("ascii"))
            target.flush()
            os.fsync(target.fileno())
        # Exclusive publication exposes only a complete ID and never replaces another caller's request.
        os.link(temporary, flag)
        return True
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime if _regular(path) else 0
    except FileNotFoundError:
        return 0


def request_refresh(settings: Settings, now: datetime):
    """Return ``(http_status, body, headers)`` with the same states and body as refresh.php."""
    try:
        directory = settings.energy_refresh_dir
        if (
            directory is None
            or not directory.is_absolute()
            or directory.is_symlink()
            or not directory.is_dir()
            or directory.resolve() != directory
        ):
            return _reply(503, "unavailable")
        info = directory.lstat()
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            return _reply(503, "unavailable")
        flag, running = directory / "request.flag", directory / "request.running"
        for path in (flag, running):
            if path.is_symlink() or (
                path.exists() and (not _regular(path) or stat.S_IMODE(path.stat().st_mode) != 0o600)
            ):
                return _reply(503, "unavailable")
        status = _status(settings)
        existing = _existing(settings, status, flag, running)
        if existing:
            return existing
        attempt = _instant(status.get("last_attempt_at"))
        last_request = max(attempt.timestamp() if attempt else 0, _mtime(flag), _mtime(running))
        remaining = COOLDOWN_SECONDS - (now.timestamp() - last_request)
        if remaining > 0:
            return _reply(429, "rate_limited", min(COOLDOWN_SECONDS, math.ceil(remaining)))
        request_id = secrets.token_hex(16)
        if not _publish(directory, flag, request_id):
            return _existing(settings, _status(settings), flag, running) or _reply(503, "unavailable")
        # A fast worker may already have claimed or fulfilled this ID. The watermark alone cannot prove that.
        return _reply(202, "pending", 0, _status(settings).get("last_attempt_at"), request_id)
    except (Unavailable, OSError):
        return _reply(503, "unavailable")
