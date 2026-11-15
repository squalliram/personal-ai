"""Append-only audit log of operational metadata. Never pass content, secrets or request bodies."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}'
);
"""


class AuditLog:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("Audit database must not be a symlink")
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript(SCHEMA)

    def record(self, action: str, outcome: str, *, actor: str = "anonymous", **detail: object) -> None:
        clean = {}
        for key, value in detail.items():
            if isinstance(value, (bool, int, float)) or value is None:
                clean[key] = value
            else:
                clean[key] = str(value)[:120]
        at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
        with self._lock, closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute(
                "INSERT INTO audit_log(at,actor,action,outcome,detail) VALUES(?,?,?,?,?)",
                (at, actor, action, outcome, json.dumps(clean, sort_keys=True)),
            )

    def recent(self, limit: int = 50) -> list[dict]:
        return self.page(limit=limit)[0]

    def page(self, *, limit: int = 50, offset: int = 0, action: str | None = None) -> tuple[list[dict], int]:
        """Newest first, optionally one action only. Returns the rows and the total matching count."""
        where, params = ("WHERE action=?", (action,)) if action else ("", ())
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            (total,) = conn.execute(f"SELECT COUNT(*) FROM audit_log {where}", params).fetchone()
            rows = conn.execute(
                f"SELECT id,at,actor,action,outcome,detail FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows], total

    def actions(self) -> list[str]:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]

    def sign_in_activity(self, since: float, *, limit: int = 5) -> dict:
        """Recent successful sign-ins and how many sign-in attempts failed since `since` (a Unix time)."""
        cutoff = datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT at,detail FROM audit_log WHERE action='login' AND outcome='success' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            (failed,) = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action IN ('login','passkey_login')"
                " AND outcome IN ('failure','rate_limited') AND at>=?",
                (cutoff,),
            ).fetchone()
        sign_ins = []
        for row in rows:
            detail = json.loads(row["detail"])
            sign_ins.append(
                {
                    "at": row["at"],
                    "method": detail.get("method"),
                    "client": detail.get("client"),
                    "device": detail.get("device"),
                }
            )
        return {"recent_sign_ins": sign_ins, "failed_attempts": failed}
