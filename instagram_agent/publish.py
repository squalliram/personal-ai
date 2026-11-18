"""Publish due drafts to @instafoodielove as carousels, single photos or reels: the one Instagram write this project makes.

A draft is due at max(scheduled_for, created_at + VETO_LEAD) when it was approved, or is still
pending and passed review (reels from the owner's clips need Approve). Rejecting it in the Command
Centre before then stops it. Meta fetches each slide or video from a public URL, so they are copied
to an unguessable folder under INSTAGRAM_PUBLIC_DIR for the length of the call and removed afterwards.
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

from notifications import command_centre_link, notify

from . import storage
from .drafts import PASS_SCORE
from .graph import GraphAPIError, GraphClient

VETO_LEAD = timedelta(hours=1)
STALE_AFTER = timedelta(hours=3)
MAX_ATTEMPTS = 3
STATUS_POLLS = 10
REEL_POLLS = 5  # Meta: check a video container once a minute for at most 5 minutes
REEL_WAIT = 60


class PublishError(RuntimeError):
    """A safe publishing error: no tokens, URLs or response bodies."""


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def posts_at(scheduled_for: str | None, created_at: str) -> datetime | None:
    if not scheduled_for:
        return None
    return max(_instant(scheduled_for), _instant(created_at) + VETO_LEAD)


def kind(row) -> str:
    return row["kind"] if "kind" in row.keys() and row["kind"] else "carousel"


def will_post(status: str, review_score: int | None, draft_kind: str = "carousel") -> bool:
    if draft_kind == "reel":  # the owner's own clips post only once approved
        return status == "approved"
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
            conn.execute(
                "UPDATE drafts SET status='failed' WHERE draft_id=? AND status NOT IN ('rejected','published')",
                (draft_id,),
            )
    return len(stuck)


def rendered(row) -> bool:
    """False for a reel whose render is pending or failed."""
    if kind(row) != "reel" or "reel" not in row.keys():
        return True
    try:
        state = json.loads(row["reel"] or "{}").get("render")
    except (ValueError, AttributeError):
        return False
    return state in (None, "done")


def due(conn, now: datetime) -> list:
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM publish_runs r WHERE r.draft_id=d.draft_id AND r.outcome='failed') AS fails "
        "FROM drafts d WHERE d.status IN ('approved','pending') AND d.scheduled_for IS NOT NULL ORDER BY d.scheduled_for"
    ).fetchall()
    found = []
    for row in rows:
        when = posts_at(row["scheduled_for"], row["created_at"])
        if not rendered(row):
            continue  # a reel still rendering, or whose render failed, never posts unedited
        if will_post(row["status"], row["review_score"], kind(row)) and when <= now <= when + STALE_AFTER:
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


def _check_public_video(url: str, size: int) -> None:
    try:
        request = Request(url, method="HEAD", headers={"User-Agent": "personal-ai-instagram/1.0"})
        with urlopen(request, timeout=15) as response:
            if response.headers.get("Content-Length") != str(size):
                raise PublishError("public video URL served a different size")
    except HTTPError as exc:
        raise PublishError(f"public video URL not reachable (HTTP {exc.code})") from None
    except (URLError, TimeoutError) as exc:
        raise PublishError(f"public video URL not reachable ({type(exc).__name__})") from None


def _public_folder(public_dir: Path) -> Path:
    if public_dir.is_symlink() or (public_dir.exists() and not public_dir.is_dir()):
        raise PublishError("public media folder is not a directory")
    public_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    index = public_dir / "index.html"
    if not index.exists() and not index.is_symlink():
        index.write_text("")  # No directory listing while a post's folder exists.
    return public_dir / secrets.token_hex(16)


def _quota_left(client: GraphClient) -> None:
    """Refuse when Meta says the 24-hour publishing limit is used up; carry on if it doesn't say."""
    try:
        data = client.get(f"{client.user_id}/content_publishing_limit", {"fields": "quota_usage,config"})["data"][0]
        used, total = data["quota_usage"], data["config"]["quota_total"]
    except (GraphAPIError, KeyError, IndexError, TypeError):
        return
    if isinstance(used, int) and isinstance(total, int) and used >= total:
        raise PublishError(f"Instagram's publishing limit is used up ({used} of {total} in 24 hours)")


def publish_reel(
    client: GraphClient,
    row,
    media_dir: Path,
    public_dir: Path,
    public_url: str,
    *,
    check: Callable[[str, int], None] = _check_public_video,
    sleep: Callable[[float], None] = time.sleep,
    on_publish: Callable[[], None] = lambda: None,
) -> str:
    """A REELS container from the clip's public URL, polled until Meta has processed it, then media_publish."""
    reel = json.loads(row["reel"] or "{}")
    name = str(reel.get("file", ""))
    if name not in ("reel.mp4", "reel.mov"):
        raise PublishError("the reel's video is missing")
    source = media_dir / row["draft_id"] / name
    if source.parent.is_symlink() or source.is_symlink() or not source.is_file():
        raise PublishError("the reel's video is missing")
    _quota_left(client)
    folder = _public_folder(public_dir)
    try:
        folder.mkdir(mode=0o755)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with source.open("rb") as stream_in, os.fdopen(os.open(folder / name, flags, 0o644), "wb") as stream_out:
            shutil.copyfileobj(stream_in, stream_out, 1024 * 1024)
        url = f"{public_url.rstrip('/')}/{folder.name}/{name}"
        check(url, source.stat().st_size)
        user = client.user_id
        caption = row["caption"] + ("\n\n" + row["hashtags"] if row["hashtags"] else "")
        params = {"media_type": "REELS", "video_url": url, "caption": caption, "share_to_feed": "true"}
        cover = reel.get("cover_ms")
        if isinstance(cover, int) and not isinstance(cover, bool) and cover > 0:
            params["thumb_offset"] = str(cover)
        container = client.post(f"{user}/media", params)["id"]
        for _ in range(REEL_POLLS):
            sleep(REEL_WAIT)
            status = client.get(container, {"fields": "status_code"}).get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the clip ({status}); export it again")
        else:
            raise PublishError("Instagram was still processing the clip after 5 minutes")
        on_publish()
        return str(client.post(f"{user}/media_publish", {"creation_id": container}, retry=False)["id"])
    except KeyError:
        raise PublishError("Instagram returned no ID") from None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


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
    """Containers for each slide and the carousel (or one image), then media_publish (never retried). Returns the ID."""
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
    if not 1 <= len(images) <= 20:
        raise PublishError("a post needs 1-20 slides")
    folder = _public_folder(public_dir)
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
        caption = row["caption"] + ("\n\n" + row["hashtags"] if row["hashtags"] else "")
        if len(urls) == 1:  # one uploaded photo is a single-image post
            container = client.post(f"{user}/media", {"image_url": urls[0], "caption": caption})["id"]
        else:
            children = [client.post(f"{user}/media", {"image_url": u, "is_carousel_item": "true"})["id"] for u in urls]
            container = client.post(
                f"{user}/media", {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}
            )["id"]
        for _ in range(STATUS_POLLS):
            status = client.get(container, {"fields": "status_code"}).get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the carousel ({status})")
            sleep(3)
        else:
            raise PublishError("Instagram was still processing the carousel")
        on_publish()
        return str(client.post(f"{user}/media_publish", {"creation_id": container}, retry=False)["id"])
    except KeyError:
        raise PublishError("Instagram returned no ID") from None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def permalink(client: GraphClient, media_id: str) -> str | None:
    """The post's Instagram link, best effort."""
    try:
        value = client.get(media_id, {"fields": "permalink"}).get("permalink")
    except Exception:  # the post is already live: nothing here may stop the tick
        return None
    return value if isinstance(value, str) and value.startswith("https://") else None


def ready(env: dict) -> bool:
    """The switch and the public folder are set. Posting also needs the token and user ID (`configured`)."""
    public_dir, public_url = env.get("INSTAGRAM_PUBLIC_DIR"), env.get("INSTAGRAM_PUBLIC_URL", "")
    return bool(env.get("INSTAGRAM_AUTO_PUBLISH") == "1" and public_dir and public_url.startswith("https://"))


def configured(env: dict) -> bool:
    return ready(env) and bool(env.get("INSTAGRAM_ACCESS_TOKEN") and env.get("INSTAGRAM_USER_ID"))


def tick(conn, env: dict, media_dir: Path, now: datetime, client: GraphClient | None = None, **kwargs) -> str:
    """Publish every due draft. Off unless INSTAGRAM_AUTO_PUBLISH=1 and the token and public folder are set."""
    public_dir, public_url = env.get("INSTAGRAM_PUBLIC_DIR"), env.get("INSTAGRAM_PUBLIC_URL", "")
    if client is None and configured(env):
        from .cli import graph_client

        client = graph_client(env)
    if not ready(env) or client is None:
        set_state(conn, "off", now)
        return "publish_off"
    set_state(conn, "on", now)
    page = command_centre_link(env, "instagram")
    stuck = recover(conn, now)
    if stuck:
        notify("instagram", "failed", f"{stuck} Instagram post(s) stopped mid-publish: check Instagram", page, env=env)
    published = failed = 0
    for row in due(conn, now):
        stamp = now.isoformat(timespec="seconds")

        sent = []

        def started(draft_id=row["draft_id"], stamp=stamp, sent=sent):
            # Re-check under a write lock: a Reject during preparation must win.
            conn.execute("BEGIN IMMEDIATE")
            try:
                now_row = conn.execute(
                    "SELECT status, review_score, kind FROM drafts WHERE draft_id=?", (draft_id,)
                ).fetchone()
                if not will_post(now_row["status"], now_row["review_score"], kind(now_row)):
                    raise PublishError("rejected before posting")
                _record(conn, draft_id, stamp, "started")
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            sent.append(True)

        try:
            send = publish_reel if kind(row) == "reel" else publish_draft
            media_id = send(client, row, media_dir, Path(public_dir), public_url, on_publish=started, **kwargs)
        except (PublishError, GraphAPIError, OSError, ValueError) as exc:
            error = str(exc) if isinstance(exc, (PublishError, GraphAPIError)) else type(exc).__name__
            with conn:
                if sent:
                    error = f"{error}; outcome unknown, check Instagram"
                _record(conn, row["draft_id"], stamp, "failed", error=error)
                if sent or row["fails"] + 1 >= MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE drafts SET status='failed' WHERE draft_id=? AND status NOT IN ('rejected','published')",
                        (row["draft_id"],),
                    )
            attempt = "not retried" if sent else f"attempt {row['fails'] + 1} of {MAX_ATTEMPTS}"
            notify("instagram", "failed", f"Instagram post failed: {row['topic']} ({error}; {attempt})", page, env=env)
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
        link = permalink(client, media_id) or page
        notify("instagram", "ok", f"Instagram post is live: {row['topic']}", link, env=env)
        published += 1
    return f"published {published}, failed {failed}"
