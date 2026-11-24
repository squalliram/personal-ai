"""The owner's music library for reels: tracks they uploaded with the licence and source they recorded.

Only a track with a recorded licence can go under a reel. Nothing is ever downloaded automatically.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

from . import uploads

FOLDER = "music"
MAX_TRACK_BYTES = 20 * 1024 * 1024
MAX_TITLE, MAX_LICENCE, MAX_SOURCE = 120, 300, 300
MIXES = ("under", "replace")
TABLE = """
CREATE TABLE IF NOT EXISTS music (
    track_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    licence TEXT NOT NULL CHECK (length(trim(licence)) > 0),
    source TEXT NOT NULL,
    ext TEXT NOT NULL CHECK (ext IN ('mp3', 'm4a', 'wav')),
    bytes INTEGER NOT NULL
)
"""


class MusicError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def kind(data: bytes) -> str | None:
    """mp3, m4a or wav from the magic bytes."""
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mp3"
    if data[4:8] == b"ftyp" and data[8:11] in (b"M4A", b"mp4", b"iso", b"mp4"):
        return "m4a"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    return None


def _field(value, limit: int, code: str, required: bool = True) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if (required and not text) or len(text) > limit or uploads.CONTROL.search(text):
        raise MusicError(code)
    return text


def save(conn: sqlite3.Connection, media_dir: Path, title, licence, source, data: bytes, now: datetime) -> str:
    """Store a track with its licence and source note. Returns the track ID."""
    title = _field(title, MAX_TITLE, "invalid_title")
    licence = _field(licence, MAX_LICENCE, "licence_required")
    source = _field(source, MAX_SOURCE, "invalid_source", required=False)
    if not 0 < len(data) <= MAX_TRACK_BYTES:
        raise MusicError("track_too_large" if data else "unsupported_track")
    ext = kind(data)
    if ext is None:
        raise MusicError("unsupported_track")
    root = uploads.private_root(media_dir, FOLDER)
    track_id = secrets.token_hex(8)
    (root / track_id).mkdir(mode=0o700)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(root / track_id / f"track.{ext}", flags, 0o600), "wb") as stream:
            stream.write(data)
        with conn:
            conn.execute(TABLE)
            conn.execute(
                "INSERT INTO music(track_id,created_at,title,licence,source,ext,bytes) VALUES(?,?,?,?,?,?,?)",
                (track_id, now.isoformat(timespec="seconds"), title, licence, source, ext, len(data)),
            )
    except BaseException:
        shutil.rmtree(root / track_id, ignore_errors=True)
        raise
    return track_id


def delete(conn: sqlite3.Connection, media_dir: Path, track_id: str) -> bool:
    if not uploads.UPLOAD_ID.fullmatch(track_id):
        return False
    try:
        with conn:
            changed = conn.execute("DELETE FROM music WHERE track_id=?", (track_id,)).rowcount
    except sqlite3.OperationalError:
        return False
    if changed:
        shutil.rmtree(media_dir / FOLDER / track_id, ignore_errors=True)
    return bool(changed)


def tracks(conn: sqlite3.Connection) -> list[dict]:
    """Usable tracks (licence recorded), newest first."""
    try:
        rows = conn.execute(
            "SELECT track_id,created_at,title,licence,source FROM music WHERE length(trim(licence))>0 "
            "ORDER BY created_at DESC, track_id"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(zip(("track_id", "created_at", "title", "licence", "source"), row, strict=True)) for row in rows]


def usable_file(conn: sqlite3.Connection, media_dir: Path, track_id: str) -> Path | None:
    """The track's file if it still exists with a recorded licence, refusing symlinks."""
    if not isinstance(track_id, str) or not uploads.UPLOAD_ID.fullmatch(track_id):
        return None
    try:
        row = conn.execute("SELECT ext FROM music WHERE track_id=? AND length(trim(licence))>0", (track_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    path = media_dir / FOLDER / track_id / f"track.{row[0]}"
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if path.parent.is_symlink() or not stat.S_ISREG(info.st_mode):
        return None
    return path


def for_reel(conn: sqlite3.Connection, media_dir: Path) -> list[dict]:
    """Licensed files available to render, with observed reach/views kept separate by checkpoint."""
    available = [t for t in tracks(conn) if usable_file(conn, media_dir, t["track_id"]) is not None]
    by_id = {t["track_id"]: t for t in available}
    for track in available:
        track["performance"] = {}
    if not available:
        return available
    rows = conn.execute(
        "SELECT d.reel,p.checkpoint,p.views,p.reach FROM post_reports p JOIN drafts d ON d.draft_id=p.draft_id "
        "WHERE d.kind='reel' AND p.captured_at IS NOT NULL AND p.format IS NOT 'story'"
    ).fetchall()
    observed = {}
    for row in rows:
        try:
            track_id = json.loads(row[0] or "{}").get("music", {}).get("track_id")
        except (ValueError, AttributeError, TypeError):
            continue
        if not isinstance(track_id, str) or track_id not in by_id:
            continue
        observed.setdefault((track_id, row[1]), []).append(row[2:])
    for (track_id, checkpoint), values in observed.items():
        metrics = {"reels": len(values)}
        for index, name in enumerate(("views", "reach")):
            measured = [v[index] for v in values if isinstance(v[index], (int, float)) and v[index] >= 0]
            metrics[f"mean_{name}"] = sum(measured) / len(measured) if measured else None
            metrics[f"{name}_samples"] = len(measured)
        by_id[track_id]["performance"][checkpoint] = metrics
    return available


def choice(value, mix, conn: sqlite3.Connection | None = None) -> dict:
    """Default to the agents; preserve Off as an explicit opt-out rather than a missing choice."""
    if value == "off":
        return {"pick": "off"}
    if mix not in MIXES:
        raise MusicError("invalid_music")
    if value in (None, "", "agents"):
        return {"pick": "agents", "mix": mix}
    if not isinstance(value, str) or not uploads.UPLOAD_ID.fullmatch(value):
        raise MusicError("invalid_music")
    if conn is not None and not any(t["track_id"] == value for t in tracks(conn)):
        raise MusicError("unknown_track")
    return {"pick": "track", "track_id": value, "mix": mix}
