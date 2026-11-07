"""Password hashing, permission levels, rate limiting and sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

PBKDF2_ITERATIONS = 600_000
MIN_ITERATIONS = 1_000
MAX_ITERATIONS = 10_000_000
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
_PREFIX = "pbkdf2_sha256"


class Permission(IntEnum):
    """READ retrieves data, RECOMMEND prepares suggestions, EXECUTE performs approved operations."""

    READ = 1
    RECOMMEND = 2
    EXECUTE = 3

    @classmethod
    def parse(cls, value: str) -> Permission:
        try:
            return cls[value.strip().upper()]
        except (KeyError, AttributeError):
            raise ValueError("Permission must be read, recommend or execute") from None

    @property
    def label(self) -> str:
        return self.name.lower()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"Passphrase must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters")
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ValueError("Unsupported iteration count")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{_PREFIX}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded or not isinstance(password, str) or not 0 < len(password) <= MAX_PASSWORD_LENGTH:
        return False
    try:
        prefix, iterations_text, salt_text, expected_text = encoded.split("$")
        iterations = int(iterations_text)
        salt, expected = _unb64(salt_text), _unb64(expected_text)
    except (ValueError, TypeError):
        return False
    if prefix != _PREFIX or not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS or len(expected) != 32:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


class RateLimiter:
    """Sliding-window counter. Keys are bounded so a flood of clients cannot exhaust memory."""

    def __init__(
        self, limit: int, window_seconds: float, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000
    ) -> None:
        self.limit, self.window, self.clock, self.max_keys = limit, window_seconds, clock, max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self.max_keys:
                for stale in [k for k, v in self._events.items() if not v or v[-1] <= now - self.window]:
                    del self._events[stale]
                if len(self._events) >= self.max_keys:
                    self._events.pop(next(iter(self._events)))
            events = self._events[key] = deque()
        while events and events[0] <= now - self.window:
            events.popleft()
        return events

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, self.clock())) >= self.limit

    def record(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            self._prune(key, now).append(now)

    def hit(self, key: str) -> bool:
        """Record one event and report whether it is within the limit."""
        with self._lock:
            now = self.clock()
            events = self._prune(key, now)
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


@dataclass
class Session:
    token_hash: str
    csrf_token: str
    permission: Permission
    created_at: float
    last_seen_at: float


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    """Process-memory sessions: a restart signs everyone out and no token is ever written to disk."""

    def __init__(
        self,
        *,
        idle_seconds: int,
        absolute_seconds: int,
        clock: Callable[[], float] = time.time,
        max_sessions: int = 20,
    ) -> None:
        self.idle, self.absolute, self.clock, self.max_sessions = idle_seconds, absolute_seconds, clock, max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _expired(self, session: Session, now: float) -> bool:
        return now - session.last_seen_at > self.idle or now - session.created_at > self.absolute

    def create(self, permission: Permission) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(_token_hash(token), secrets.token_urlsafe(32), permission, now, now)
        with self._lock:
            for key in [k for k, s in self._sessions.items() if self._expired(s, now)]:
                del self._sessions[key]
            while len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.last_seen_at)
                del self._sessions[oldest.token_hash]
            self._sessions[session.token_hash] = session
        return token, session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        key = _token_hash(token)
        now = self.clock()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if self._expired(session, now):
                del self._sessions[key]
                return None
            session.last_seen_at = now
            return session

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(_token_hash(token), None)

    def expires_at(self, session: Session) -> float:
        return min(session.last_seen_at + self.idle, session.created_at + self.absolute)


SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  csrf_token TEXT NOT NULL,
  permission INTEGER NOT NULL,
  created_at REAL NOT NULL,
  last_seen_at REAL NOT NULL
);
"""


class SqliteSessionStore:
    """File-backed sessions for multi-process hosting such as Passenger.

    Only SHA-256 token hashes are stored, never tokens. The file is created
    with mode 0600 and symlinks are refused. SQLite serializes writers, so
    several Passenger processes share sessions safely. A server restart no
    longer signs the owner out; idle and absolute expiry are unchanged.
    """

    def __init__(
        self,
        path: Path,
        *,
        idle_seconds: int,
        absolute_seconds: int,
        clock: Callable[[], float] = time.time,
        max_sessions: int = 20,
    ) -> None:
        self.path, self.idle, self.absolute, self.clock, self.max_sessions = (
            path,
            idle_seconds,
            absolute_seconds,
            clock,
            max_sessions,
        )
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("Session database must not be a symlink")
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript(SESSIONS_SCHEMA)

    def _expired(self, session: Session, now: float) -> bool:
        return now - session.last_seen_at > self.idle or now - session.created_at > self.absolute

    def create(self, permission: Permission) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(_token_hash(token), secrets.token_urlsafe(32), permission, now, now)
        with self._lock, closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute(
                "DELETE FROM sessions WHERE ? - last_seen_at > ? OR ? - created_at > ?",
                (now, self.idle, now, self.absolute),
            )
            (count,) = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            if count >= self.max_sessions:
                conn.execute(
                    "DELETE FROM sessions WHERE token_hash IN "
                    "(SELECT token_hash FROM sessions ORDER BY last_seen_at LIMIT ?)",
                    (count - self.max_sessions + 1,),
                )
            conn.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?)",
                (session.token_hash, session.csrf_token, int(session.permission), session.created_at, now),
            )
        return token, session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        key = _token_hash(token)
        now = self.clock()
        with self._lock, closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            row = conn.execute(
                "SELECT csrf_token, permission, created_at, last_seen_at FROM sessions WHERE token_hash = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            session = Session(key, row[0], Permission(row[1]), row[2], row[3])
            if self._expired(session, now):
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (key,))
                return None
            session.last_seen_at = now
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, key))
            return session

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock, closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def expires_at(self, session: Session) -> float:
        return min(session.last_seen_at + self.idle, session.created_at + self.absolute)
