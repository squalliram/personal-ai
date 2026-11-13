"""Publish due drafts to @instafoodielove as carousels: the one Instagram write this project makes.

A draft is due at max(scheduled_for, created_at + VETO_LEAD) when it was approved, or is still
pending and passed review. Rejecting it in the Command Centre before then stops it. Meta fetches
each slide from a public URL, so the slides are copied to an unguessable folder under
INSTAGRAM_PUBLIC_DIR for the length of the call and removed afterwards.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import storage
from .drafts import PASS_SCORE
from .graph import GraphAPIError, GraphClient

VETO_LEAD = timedelta(hours=1)
STALE_AFTER = timedelta(hours=3)
MAX_ATTEMPTS = 3
STATUS_POLLS = 10


class PublishError(RuntimeError):
    """A safe publishing error: no tokens, URLs or response bodies."""


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def posts_at(scheduled_for: str | None, created_at: str) -> datetime | None:
    if not scheduled_for:
        return None
    return max(_instant(scheduled_for), _instant(created_at) + VETO_LEAD)


def will_post(status: str, review_score: int | None) -> bool:
    return status == "approved" or (status == "pending" and (review_score or 0) >= PASS_SCORE)


def set_state(conn, value: str, now: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT INTO publisher_state(key,value,updated_at) VALUES('auto_publish',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (value, now.isoformat(timespec="seconds")),
        )


def _record(conn, draft_id: str, stamp: str, outcome: str, media_id=None, error=None) -> None:
    conn.execute(
        "INSERT INTO publish_runs(draft_id,started_at,outcome,media_id,error) VALUES(?,?,?,?,?)",
        (draft_id, stamp, outcome, media_id, error[:300] if error else None),
    )


def recover(conn, now: datetime) -> int:
    """A 'started' publish with no result means a crash mid-publish: don't risk a duplicate post."""
    stuck = conn.execute(
        "SELECT r.id, r.draft_id FROM publish_runs r WHERE r.outcome='started' AND NOT EXISTS "
        "(SELECT 1 FROM publish_runs s WHERE s.draft_id=r.draft_id AND s.id>r.id)"
    ).fetchall()
    with conn:
        for _, draft_id in stuck:
            _record(
                conn, draft_id, now.isoformat(timespec="seconds"), "failed", error="outcome unknown; check Instagram"
            )
            conn.execute("UPDATE drafts SET status='failed' WHERE draft_id=? AND status!='published'", (draft_id,))
    return len(stuck)


def due(conn, now: datetime) -> list:
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM publish_runs r WHERE r.draft_id=d.draft_id AND r.outcome='failed') AS fails "
        "FROM drafts d WHERE d.status IN ('approved','pending') AND d.scheduled_for IS NOT NULL ORDER BY d.scheduled_for"
    ).fetchall()
    found = []
    for row in rows:
        when = posts_at(row["scheduled_for"], row["created_at"])
        if will_post(row["status"], row["review_score"]) and when <= now <= when + STALE_AFTER:
            if row["fails"] < MAX_ATTEMPTS:
                found.append(row)
    return found


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def _check_public(url: str, size: int) -> None:
    try:
        with urlopen(Request(url, headers={"User-Agent": "personal-ai-instagram/1.0"}), timeout=15) as response:
            if len(response.read()) != size:
                raise PublishError("public slide URL served different bytes")
    except HTTPError as exc:
        raise PublishError(f"public slide URL not reachable (HTTP {exc.code})") from None
    except (URLError, TimeoutError) as exc:
        raise PublishError(f"public slide URL not reachable ({type(exc).__name__})") from None


def publish_draft(
    client: GraphClient,
    row,
    media_dir: Path,
    public_dir: Path,
    public_url: str,
    *,
    check: Callable[[str, int], None] = _check_public,
    sleep: Callable[[float], None] = time.sleep,
    on_publish: Callable[[], None] = lambda: None,
) -> str:
    """Containers for each slide, a carousel container, then media_publish (never retried). Returns the media ID."""
    slides = json.loads(row["slides"])
    folder_in = media_dir / row["draft_id"]
    if folder_in.is_symlink():
        raise PublishError("a slide image is missing")
    images = []
    for number in range(1, len(slides) + 1):
        path = folder_in / f"slide-{number}.jpg"
        if not path.exists() and (folder_in / f"slide-{number}.png").exists():
            raise PublishError("Instagram only accepts JPEG slides")
        if path.is_symlink() or not path.is_file():
            raise PublishError("a slide image is missing")
        data = path.read_bytes()
        if not _is_jpeg(data):
            raise PublishError("Instagram only accepts JPEG slides")
        images.append(data)
    if not 2 <= len(images) <= 20:
        raise PublishError("a carousel needs 2-20 slides")
    if public_dir.is_symlink() or (public_dir.exists() and not public_dir.is_dir()):
        raise PublishError("public media folder is not a directory")
    public_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    index = public_dir / "index.html"
    if not index.exists() and not index.is_symlink():
        index.write_text("")  # No directory listing while a post's folder exists.
    folder = public_dir / secrets.token_hex(16)
    try:
        folder.mkdir(mode=0o755)
        urls = []
        for number, data in enumerate(images, 1):
            name = f"{number}.jpg"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            with os.fdopen(os.open(folder / name, flags, 0o644), "wb") as stream:
                stream.write(data)
            url = f"{public_url.rstrip('/')}/{folder.name}/{name}"
            check(url, len(data))
            urls.append(url)
        user = client.user_id
        children = [client.post(f"{user}/media", {"image_url": u, "is_carousel_item": "true"})["id"] for u in urls]
        caption = row["caption"] + ("\n\n" + row["hashtags"] if row["hashtags"] else "")
        carousel = client.post(
            f"{user}/media", {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}
        )["id"]
        for _ in range(STATUS_POLLS):
            status = client.get(carousel, {"fields": "status_code"}).get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the carousel ({status})")
            sleep(3)
        else:
            raise PublishError("Instagram was still processing the carousel")
        on_publish()
        return str(client.post(f"{user}/media_publish", {"creation_id": carousel}, retry=False)["id"])
    except KeyError:
        raise PublishError("Instagram returned no ID") from None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def tick(conn, env: dict, media_dir: Path, now: datetime, client: GraphClient | None = None, **kwargs) -> str:
    """Publish every due draft. Off unless INSTAGRAM_AUTO_PUBLISH=1 and the token and public folder are set."""
    public_dir, public_url = env.get("INSTAGRAM_PUBLIC_DIR"), env.get("INSTAGRAM_PUBLIC_URL", "")
    ready = env.get("INSTAGRAM_AUTO_PUBLISH") == "1" and public_dir and public_url.startswith("https://")
    if client is None and ready and env.get("INSTAGRAM_ACCESS_TOKEN") and env.get("INSTAGRAM_USER_ID"):
        from .cli import graph_client

        client = graph_client(env)
    if not ready or client is None:
        set_state(conn, "off", now)
        return "publish_off"
    set_state(conn, "on", now)
    recover(conn, now)
    published = failed = 0
    for row in due(conn, now):
        stamp = now.isoformat(timespec="seconds")

        sent = []

        def started(draft_id=row["draft_id"], stamp=stamp, sent=sent):
            # Re-check under a write lock: a Reject during preparation must win.
            conn.execute("BEGIN IMMEDIATE")
            try:
                now_row = conn.execute(
                    "SELECT status, review_score FROM drafts WHERE draft_id=?", (draft_id,)
                ).fetchone()
                if not will_post(now_row["status"], now_row["review_score"]):
                    raise PublishError("rejected before posting")
                _record(conn, draft_id, stamp, "started")
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            sent.append(True)

        try:
            media_id = publish_draft(client, row, media_dir, Path(public_dir), public_url, on_publish=started, **kwargs)
        except (PublishError, GraphAPIError, OSError, ValueError) as exc:
            error = str(exc) if isinstance(exc, (PublishError, GraphAPIError)) else type(exc).__name__
            with conn:
                if sent:
                    error = f"{error}; outcome unknown, check Instagram"
                _record(conn, row["draft_id"], stamp, "failed", error=error)
                if sent or row["fails"] + 1 >= MAX_ATTEMPTS:
                    conn.execute("UPDATE drafts SET status='failed' WHERE draft_id=?", (row["draft_id"],))
            failed += 1
            continue
        with conn:
            _record(conn, row["draft_id"], stamp, "published", media_id=media_id)
            conn.execute(
                "UPDATE drafts SET status='published', media_id=? WHERE draft_id=?", (media_id, row["draft_id"])
            )
        try:
            with conn:
                storage.link_published(conn, row["draft_id"], media_id)
        except sqlite3.IntegrityError:
            pass  # linked once sync has stored the post
        published += 1
    return f"published {published}, failed {failed}"
