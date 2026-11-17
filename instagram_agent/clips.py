"""The owner's video clips waiting to become reels, uploaded from the Command Centre in chunks.

A clip is `uploading` until every byte has arrived, then checked against Meta's spec (`video.check`) and either
dropped with the reasons or left `waiting` for the daily run, which turns the oldest into that day's reel draft.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path

from . import uploads, video

FOLDER = "clips"
MAX_CLIP_BYTES = video.MAX_BYTES
CHUNK_BYTES = 8 * 1024 * 1024
STALE_UPLOAD = timedelta(days=1)
PART = "clip.part"
TABLE = """
CREATE TABLE IF NOT EXISTS clips (
    clip_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    note TEXT NOT NULL,
    size INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading', 'waiting', 'used')),
    facts TEXT,
    draft_id TEXT,
    used_at TEXT
)
"""


class ClipError(ValueError):
    """A clip the owner should fix or send again; `code` is the API error code, `problems` the spec breaks."""

    def __init__(self, code: str, problems: list[str] | None = None):
        super().__init__(code)
        self.code, self.problems = code, problems or []


def folder(media_dir: Path, clip_id: str) -> Path:
    if not uploads.UPLOAD_ID.fullmatch(clip_id):
        raise ValueError("invalid clip id")
    return media_dir / FOLDER / clip_id


def _open(path: Path, flags: int):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("not a file")
    return fd


def clip_file(media_dir: Path, row) -> Path:
    facts = json.loads(row["facts"] or "{}")
    return folder(media_dir, row["clip_id"]) / f"clip.{'mov' if facts.get('ext') == 'mov' else 'mp4'}"


def start(conn: sqlite3.Connection, media_dir: Path, note: str, size: int, now: datetime) -> str:
    """A new `uploading` clip of `size` bytes; also clears uploads abandoned a day ago."""
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_CLIP_BYTES:
        raise ClipError("clip_too_large" if isinstance(size, int) and size > 0 else "invalid_size")
    root = uploads.private_root(media_dir, FOLDER)
    cutoff = (now - STALE_UPLOAD).isoformat(timespec="seconds")
    with conn:
        conn.execute(TABLE)
        stale = [
            r[0] for r in conn.execute("SELECT clip_id FROM clips WHERE status='uploading' AND created_at<?", (cutoff,))
        ]
    for clip_id in stale:
        with conn:  # rechecked: a clip finished meanwhile is no longer stale
            gone = conn.execute(
                "DELETE FROM clips WHERE clip_id=? AND status='uploading' AND created_at<?", (clip_id, cutoff)
            ).rowcount
        if gone:
            shutil.rmtree(folder(media_dir, clip_id), ignore_errors=True)
    clip_id = secrets.token_hex(8)
    (root / clip_id).mkdir(mode=0o700)
    os.close(_open(root / clip_id / PART, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
    try:
        with conn:
            conn.execute(
                "INSERT INTO clips(clip_id,created_at,note,size) VALUES(?,?,?,?)",
                (clip_id, now.isoformat(timespec="seconds"), note, size),
            )
    except BaseException:
        shutil.rmtree(root / clip_id, ignore_errors=True)
        raise
    return clip_id


def _uploading(conn: sqlite3.Connection, clip_id: str):
    try:
        return conn.execute("SELECT * FROM clips WHERE clip_id=? AND status='uploading'", (clip_id,)).fetchone()
    except sqlite3.OperationalError:
        return None  # no clips table yet


def append(conn: sqlite3.Connection, media_dir: Path, clip_id: str, offset: int, stream, length: int) -> int:
    """Add the next chunk, which must start where the file ends. Returns the bytes received so far."""
    row = _uploading(conn, clip_id)
    if row is None:
        raise ClipError("not_found")
    if not 0 < length <= CHUNK_BYTES or offset + length > row["size"]:
        raise ClipError("invalid_chunk")
    fd = _open(folder(media_dir, clip_id) / PART, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "wb") as out:
        fcntl.flock(out, fcntl.LOCK_EX)  # one chunk at a time
        have = os.fstat(out.fileno()).st_size
        if offset < have and offset + length <= have:
            return _replay(folder(media_dir, clip_id) / PART, offset, stream, length, have)
        if have != offset:
            raise ClipError("wrong_offset")
        left = length
        try:
            while left:
                data = stream.read(min(left, 1024 * 1024))
                if not data:
                    raise ClipError("invalid_chunk")
                out.write(data)
                left -= len(data)
            out.flush()
        except BaseException:
            out.flush()
            os.ftruncate(out.fileno(), offset)  # a failed chunk leaves nothing behind, so it can be resent
            raise
        return offset + length


def _replay(part: Path, offset: int, stream, length: int, have: int) -> int:
    """A chunk sent again after its reply was lost: accepted if it is byte-identical to what is stored."""
    with part.open("rb") as stored:
        stored.seek(offset)
        left = length
        while left:
            data = stream.read(min(left, 1024 * 1024))
            if not data or stored.read(len(data)) != data:
                raise ClipError("wrong_offset")
            left -= len(data)
    return have


def finish(conn: sqlite3.Connection, media_dir: Path, clip_id: str, *, check=video.check) -> dict:
    """Check the whole clip; keep it `waiting` and return its facts, or drop it and raise the problems."""
    row = _uploading(conn, clip_id)
    if row is None:
        raise ClipError("not_found")
    part = folder(media_dir, clip_id) / PART
    for done in (folder(media_dir, clip_id) / f"clip.{ext}" for ext in ("mp4", "mov")):
        if not part.exists() and done.is_file() and not done.is_symlink():
            done.rename(part)  # an earlier finish renamed it but didn't commit
    if part.is_symlink() or not part.is_file() or part.stat().st_size != row["size"]:
        raise ClipError("incomplete")
    facts, problems = check(part)
    if problems:
        delete(conn, media_dir, clip_id)
        raise ClipError("clip_rejected", problems)
    final = folder(media_dir, clip_id) / f"clip.{facts['ext']}"
    part.rename(final)
    with conn:
        conn.execute(
            "UPDATE clips SET status='waiting', facts=? WHERE clip_id=? AND status='uploading'",
            (json.dumps(facts), clip_id),
        )
    return facts


def delete(conn: sqlite3.Connection, media_dir: Path, clip_id: str) -> bool:
    """Remove a clip not yet drafted, and its file. False if it is unknown or already a draft."""
    try:
        with conn:
            changed = conn.execute(
                "DELETE FROM clips WHERE clip_id=? AND status IN ('uploading','waiting')", (clip_id,)
            ).rowcount
    except sqlite3.OperationalError:
        return False
    if changed:
        shutil.rmtree(folder(media_dir, clip_id), ignore_errors=True)
    return bool(changed)


def oldest(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT clip_id, created_at, note, facts FROM clips WHERE status='waiting' ORDER BY created_at, clip_id"
    ).fetchone()


def is_waiting(conn: sqlite3.Connection, clip_id: str) -> bool:
    return conn.execute("SELECT 1 FROM clips WHERE clip_id=? AND status='waiting'", (clip_id,)).fetchone() is not None
