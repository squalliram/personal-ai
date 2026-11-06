"""Private, single-flight refresh ticks. No provider credentials reach the web process."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

STATUS_KEYS = {
    "schema_version",
    "last_attempt_at",
    "last_success_at",
    "result",
    "latest_reading_at",
    "request_state",
    "request_id",
    "error_category",
}
ERROR_CATEGORIES = frozenset(
    {
        "invalid_configuration",
        "lock_unavailable",
        "inherited_lock_invalid",
        "request_claim_failed",
        "sync_failed",
        "sync_timeout",
        "provider_error",
        "database_error",
        "io_error",
        "validation_error",
        "publish_failed",
        "publish_timeout",
        "status_publish_failed",
        "status_publish_timeout",
        "interrupted",
        "release_check_failed",
        "superseded_release",
        "release_head_mismatch",
    }
)
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUPS = 2
SYNC_TIMEOUT_SECONDS = 300
PUBLISH_TIMEOUT_SECONDS = 30
GIT_TIMEOUT_SECONDS = 60


class RefreshError(RuntimeError):
    def __init__(self, category: str, *, publisher_exit_code: int | None = None):
        self.category = category if category in ERROR_CATEGORIES else "sync_failed"
        self.publisher_exit_code = publisher_exit_code
        super().__init__(self.category)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _instant(value: Any, *, canonical: bool = False) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    if canonical and not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{6})?Z", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def validate_status(value: Any) -> dict[str, Any]:
    """The publisher may reuse this exact, payload-free public schema validator."""
    if not isinstance(value, dict) or set(value) != STATUS_KEYS:
        raise ValueError("Invalid refresh status")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Invalid refresh status")
    if value["result"] not in (None, "running", "success", "no_data", "partial", "failed"):
        raise ValueError("Invalid refresh status")
    if value["request_state"] not in (None, "pending", "running"):
        raise ValueError("Invalid refresh status")
    request_id = value["request_id"]
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id)):
        raise ValueError("Invalid refresh status")
    latest = value["latest_reading_at"]
    if not isinstance(latest, dict) or set(latest) != {"electricity", "gas"}:
        raise ValueError("Invalid refresh status")
    for timestamp in (value["last_attempt_at"], value["last_success_at"], *latest.values()):
        if timestamp is not None and _instant(timestamp, canonical=True) is None:
            raise ValueError("Invalid refresh status")
    category = value["error_category"]
    if category is not None and (not isinstance(category, str) or category not in ERROR_CATEGORIES):
        raise ValueError("Invalid refresh status")
    if (value["result"] == "failed") != (category is not None):
        raise ValueError("Invalid refresh status")
    if value["result"] == "running" and value["last_attempt_at"] is None:
        raise ValueError("Invalid refresh status")
    if value["result"] == "success" and value["last_success_at"] is None:
        raise ValueError("Invalid refresh status")
    if len(json.dumps(value, allow_nan=False).encode()) > 16 * 1024:
        raise ValueError("Invalid refresh status")
    return value


def _read_json(path: Path, limit: int = 16 * 1024) -> Any:
    try:
        with path.open("rb") as source:
            raw = source.read(limit + 1)
        return json.loads(raw) if len(raw) <= limit else None
    except (OSError, ValueError, UnicodeError):
        return None


def _private(path: Path, mode: int, *, directory: bool = False) -> None:
    info = path.lstat()
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != mode:
        raise RefreshError("invalid_configuration")


def _config(app: Path) -> tuple[Path, int]:
    state = app / "data/energy-refresh"
    try:
        _private(state, 0o700, directory=True)
        config = state / "config.json"
        _private(config, 0o600)
        values = _read_json(config)
        allowed = {"DASHBOARD_TARGET", "ENERGY_REFRESH_INTERVAL_MINUTES"}
        if not isinstance(values, dict) or not set(values) <= allowed:
            raise ValueError
        if not isinstance(values.get("DASHBOARD_TARGET"), str) or not Path(values["DASHBOARD_TARGET"]).is_absolute():
            raise ValueError
        default_interval = values.get("ENERGY_REFRESH_INTERVAL_MINUTES", 60)
        if type(default_interval) is not int or default_interval <= 0:
            raise ValueError
        target_value = os.environ.get("DASHBOARD_TARGET", values["DASHBOARD_TARGET"])
        interval_value = os.environ.get("ENERGY_REFRESH_INTERVAL_MINUTES", str(default_interval))
        if not re.fullmatch(r"[0-9]+", interval_value) or int(interval_value) <= 0:
            raise ValueError
        target = Path(target_value)
        if not target.is_absolute() or not target.is_dir():
            raise ValueError
        return target, int(interval_value)
    except (OSError, ValueError, TypeError):
        raise RefreshError("invalid_configuration") from None


@contextmanager
def _lock(app: Path, inherited_fd: int | None) -> Iterator[int | None]:
    path = app / ".git/energy-dashboard.deploy.lock"
    if inherited_fd is not None:
        try:
            expected, actual = path.lstat(), os.fstat(inherited_fd)
            if not stat.S_ISREG(expected.st_mode) or (expected.st_dev, expected.st_ino) != (
                actual.st_dev,
                actual.st_ino,
            ):
                raise ValueError
        except (OSError, ValueError):
            raise RefreshError("inherited_lock_invalid") from None
        # The deploy owns this open file description; do not unlock or close it.
        yield inherited_fd
        return
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise RefreshError("lock_unavailable") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise RefreshError("lock_unavailable") from None
            yield None
            return
        yield fd
    finally:
        os.close(fd)


class _PrivateLogHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            os.close(fd)
            raise RefreshError("invalid_configuration")
        return os.fdopen(fd, "a", encoding="utf-8")

    def handleError(self, record):
        # Logging failure must not dump paths, exception messages or tracebacks.
        pass


def _logger(state: Path) -> logging.Logger:
    logger = logging.Logger("energy-refresh")
    logger.addHandler(
        _PrivateLogHandler(state / "refresh.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    )
    return logger


def _event(
    logger: logging.Logger, event: str, status: dict[str, Any], began: float, publisher_exit_code: int | None = None
) -> None:
    record = {
        "at": _iso(_now()),
        "event": event,
        "result": status["result"],
        "error_category": status["error_category"],
        "elapsed_ms": round((time.monotonic() - began) * 1000),
    }
    if publisher_exit_code is not None:
        record["publisher_result"] = (
            "partially_published"
            if publisher_exit_code == 2
            else "nothing_changed_or_rolled_back"
            if publisher_exit_code == 1
            else "unknown_failure"
        )
    logger.info(json.dumps(record, allow_nan=False))


def _command(
    args: list[str],
    app: Path,
    env: dict[str, str],
    lock_fd: int,
    timeout: int,
    *,
    capture: bool = False,
    termination_grace_seconds: int = 2,
) -> tuple[int, str]:
    with subprocess.Popen(
        args,
        cwd=app,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(lock_fd,),
        start_new_session=True,
        text=True,
    ) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=termination_grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=2)
            raise
        return process.returncode, output.strip() if output else ""


def _write_status(path: Path, status: dict[str, Any]) -> None:
    validate_status(status)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".refresh-status-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(status, output, allow_nan=False, separators=(",", ":"))
            output.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _publish(app: Path, env: dict[str, str], lock_fd: int, *, status_only: bool) -> None:
    args = [str(app / "scripts/publish-energy-dashboard.sh")]
    if status_only:
        args.append("--status-only")
    try:
        code, _ = _command(args, app, env, lock_fd, PUBLISH_TIMEOUT_SECONDS, termination_grace_seconds=30)
    except subprocess.TimeoutExpired:
        raise RefreshError("status_publish_timeout" if status_only else "publish_timeout") from None
    if code:
        raise RefreshError("status_publish_failed" if status_only else "publish_failed", publisher_exit_code=code)


def _publish_status(app: Path, env: dict[str, str], lock_fd: int, status: dict[str, Any]) -> None:
    _write_status(app / "reports/energy/dashboard/status.json", status)
    _publish(app, env, lock_fd, status_only=True)


def _published(target: Path) -> tuple[datetime | None, dict[str, str | None]]:
    payload = _read_json(target / "data.json", 8 * 1024 * 1024)
    payload = payload if isinstance(payload, dict) else {}
    latest = payload.get("latest_reading_at")
    latest = latest if isinstance(latest, dict) else {}
    readings = {}
    for fuel in ("electricity", "gas"):
        instant = _instant(latest.get(fuel))
        readings[fuel] = _iso(instant) if instant else None
    success = _instant(payload.get("last_successful_sync_at"))
    if success is not None and success > _now():
        success = None
    return success, readings


def _history(app: Path) -> dict[str, Any]:
    path = app / "data/octopus_energy.sqlite3"
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT id,status,error_category FROM sync_history ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else {}
    except sqlite3.Error:
        return {}


def _sync_category(history: dict[str, Any]) -> str:
    category = history.get("error_category")
    if category == "OctopusAPIError":
        return "provider_error"
    if category in ("OperationalError", "DatabaseError", "IntegrityError"):
        return "database_error"
    if category in ("OSError", "PermissionError", "FileNotFoundError"):
        return "io_error"
    if category in ("ValueError", "KeyError"):
        return "validation_error"
    return "sync_failed"


def _release_guard(app: Path, env: dict[str, str], lock_fd: int, expected: str) -> str | None:
    try:
        code, _ = _command(["git", "fetch", "--no-tags", "origin", "main"], app, env, lock_fd, GIT_TIMEOUT_SECONDS)
        if code:
            return "release_check_failed"
        for reference, category in (("origin/main", "superseded_release"), ("HEAD", "release_head_mismatch")):
            code, value = _command(
                ["git", "rev-parse", reference], app, env, lock_fd, GIT_TIMEOUT_SECONDS, capture=True
            )
            if code or not re.fullmatch(r"[a-f0-9]{40}", value):
                return "release_check_failed"
            if value != expected:
                return category
    except (OSError, subprocess.TimeoutExpired):
        return "release_check_failed"
    return None


def _flags(state: Path) -> bool:
    found = False
    for name in ("request.flag", "request.running"):
        path = state / name
        if path.exists() or path.is_symlink():
            _private(path, 0o600)
            found = True
    return found


def _claimed_request_id(path: Path) -> str | None:
    # Never read a pending flag: only the claimed inode identifies this attempt.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            payload = source.read(33)
        return payload.decode("ascii") if re.fullmatch(rb"[0-9a-f]{32}", payload) else None
    except OSError:
        return None


def _initial_status(app: Path, target: Path, pending: bool) -> tuple[dict[str, Any], datetime | None]:
    previous = _read_json(app / "reports/energy/dashboard/status.json")
    if isinstance(previous, dict) and "request_id" not in previous:
        previous = dict(previous, request_id=None)
    try:
        previous = validate_status(previous)
    except (ValueError, TypeError):
        previous = {}
    published_at, readings = _published(target)
    last_success = previous.get("last_success_at") or (_iso(published_at) if published_at else None)
    status = {
        "schema_version": 1,
        "last_attempt_at": previous.get("last_attempt_at"),
        "last_success_at": last_success,
        "result": previous.get("result") or ("success" if last_success else None),
        "latest_reading_at": readings,
        "request_state": "pending" if pending else None,
        "request_id": previous.get("request_id"),
        "error_category": previous.get("error_category"),
    }
    if status["result"] == "running":
        status.update(result="failed", error_category="interrupted")
    return status, published_at


def _refresh_locked(app: Path, target: Path, interval: int, force: bool, lock_fd: int, expected_sha: str | None) -> int:
    state = app / "data/energy-refresh"
    logger = _logger(state)
    began = time.monotonic()
    env = dict(
        os.environ,
        DASHBOARD_TARGET=str(target),
        DASHBOARD_DIR=str(app / "reports/energy/dashboard"),
        GIT_OPTIONAL_LOCKS="0",
    )
    status: dict[str, Any] | None = None
    attempt_request_id: str | None = None
    pending, claimed = state / "request.flag", state / "request.running"
    try:
        status, published_at = _initial_status(app, target, False)
        requested = _flags(state)
        status["request_state"] = "pending" if requested else None
        due = force or requested or published_at is None or (_now() - published_at).total_seconds() >= interval * 60
        if not due:
            if _read_json(target / "status.json") != status:
                _publish_status(app, env, lock_fd, status)
            return 0
        attempt_request_id = _claimed_request_id(claimed) if claimed.exists() else None
        status.update(
            last_attempt_at=_iso(_now()),
            result="running",
            request_state="running",
            request_id=attempt_request_id,
            error_category=None,
        )
        # PHP must observe this timestamp before the pending flag disappears.
        _publish_status(app, env, lock_fd, status)
        # A retained claim keeps its identity. A successor must wait for a later tick.
        if not claimed.exists() and pending.exists():
            try:
                _private(pending, 0o600)
                os.replace(pending, claimed)
            except (OSError, RefreshError):
                raise RefreshError("request_claim_failed") from None
            attempt_request_id = _claimed_request_id(claimed)
            status["request_id"] = attempt_request_id
            _publish_status(app, env, lock_fd, status)
        baseline = _history(app).get("id", 0)
        try:
            code, _ = _command(
                [str(app / "scripts/start-energy.sh"), "--days", "90", "--gas-units", "kwh"],
                app,
                env,
                lock_fd,
                SYNC_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise RefreshError("sync_timeout") from None
        if code:
            history = _history(app)
            outcome = history.get("status") if history.get("id", 0) > baseline else None
            if outcome in ("no_data", "partial"):
                status.update(result=outcome, request_state="pending" if _flags(state) else None)
                _publish_status(app, env, lock_fd, status)
                _event(logger, "finish", status, began)
                return 1
            raise RefreshError(_sync_category(history))
        print("ENERGY_REFRESH collection_complete", flush=True)
        if expected_sha is not None:
            category = _release_guard(app, env, lock_fd, expected_sha)
            if category:
                raise RefreshError(category)
        _publish(app, env, lock_fd, status_only=False)
        print("ENERGY_REFRESH publication_complete", flush=True)
        _, status["latest_reading_at"] = _published(target)
        status.update(
            last_success_at=_iso(_now()),
            result="success",
            request_state="pending" if pending.exists() else None,
            error_category=None,
        )
        _publish_status(app, env, lock_fd, status)
        claimed.unlink(missing_ok=True)
        _event(logger, "finish", status, began)
        return 0
    except Exception as error:
        category = (
            error.category
            if isinstance(error, RefreshError)
            else "io_error"
            if isinstance(error, OSError)
            else "sync_failed"
        )
        if status is not None:
            status.update(
                result="failed",
                error_category=category,
                request_state="pending" if pending.exists() or claimed.exists() else None,
                request_id=attempt_request_id,
            )
            _, status["latest_reading_at"] = _published(target)
            try:
                _publish_status(app, env, lock_fd, status)
            except Exception:
                # Keep the private retry state and last good public data.
                pass
            _event(
                logger,
                "finish",
                status,
                began,
                error.publisher_exit_code if isinstance(error, RefreshError) else None,
            )
        if category == "superseded_release":
            print("ENERGY_REFRESH superseded_release", flush=True)
            return 0
        return 1
    finally:
        for handler in logger.handlers:
            handler.close()


def refresh(app: Path, *, force: bool = False, inherited_lock_fd: int | None = None) -> int:
    app = app.resolve()
    expected = os.environ.get("ENERGY_REFRESH_EXPECTED_SHA")
    if expected is not None and not re.fullmatch(r"[a-f0-9]{40}", expected):
        raise RefreshError("invalid_configuration")
    if inherited_lock_fd is not None and not force:
        raise RefreshError("inherited_lock_invalid")
    with _lock(app, inherited_lock_fd) as lock_fd:
        if lock_fd is None:
            return 0
        # Deploy replaces this config under the same lock. Never use a pre-lock snapshot.
        target, interval = _config(app)
        return _refresh_locked(app, target, interval, force, lock_fd, expected)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="refresh-energy", description="One private, single-flight energy refresh tick"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--inherited-lock-fd", type=int)
    args = parser.parse_args()
    try:
        return refresh(Path.cwd(), force=args.force, inherited_lock_fd=args.inherited_lock_fd)
    except Exception as error:
        category = (
            error.category
            if isinstance(error, RefreshError)
            else "io_error"
            if isinstance(error, OSError)
            else "sync_failed"
        )
        print(f"Energy refresh failed safely: {category}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
