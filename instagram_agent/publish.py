"""Publish due reels to @instafoodielove, followed by one Story of the same video.

A draft is due at max(scheduled_for, created_at + VETO_LEAD) when it was approved, or is still
pending and passed review (unless it's a reel the owner held for approval). Rejecting it in the
Command Centre before then stops it. Meta fetches the video from a public URL, so it is copied
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
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from notifications import command_centre_link, notify

from . import recipe_series, storage, video
from .analysis import LONDON
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


class Withdrawn(PublishError):
    """Rejected or held between being picked and the final publish call: skipped, not failed."""


class StorySkipped(PublishError):
    """A published reel's video cannot be reused as a Story."""


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def posts_at(scheduled_for: str | None, created_at: str) -> datetime | None:
    if not scheduled_for:
        return None
    return max(_instant(scheduled_for), _instant(created_at) + VETO_LEAD)


def kind(row) -> str:
    return row["kind"] if "kind" in row.keys() and row["kind"] else "carousel"


def held(row) -> bool:
    """True for a reel the owner held for their approval."""
    keys = row.keys()
    if "kind" not in keys or "reel" not in keys or kind(row) != "reel":
        return False
    try:
        return json.loads(row["reel"] or "{}").get("hold") is True
    except (TypeError, ValueError, AttributeError):
        return False


def will_post(status: str, review_score: int | None, is_held: bool = False) -> bool:
    if is_held:
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


def _scheduled_reels(conn) -> list:
    return conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM publish_runs r WHERE r.draft_id=d.draft_id AND r.outcome='failed') AS fails "
        "FROM drafts d WHERE d.status IN ('approved','pending') AND d.kind='reel' AND d.scheduled_for IS NOT NULL "
        "ORDER BY d.scheduled_for, d.draft_id"
    ).fetchall()


def _eligible(row) -> bool:
    return rendered(row) and row["fails"] < MAX_ATTEMPTS and will_post(row["status"], row["review_score"], held(row))


def due(conn, now: datetime) -> list:
    found = []
    for row in _scheduled_reels(conn):
        when = posts_at(row["scheduled_for"], row["created_at"])
        if _eligible(row) and when <= now <= when + STALE_AFTER:
            found.append(row)
    return found


def recipe_slot_taken(conn, now: datetime) -> bool:
    today = now.astimezone(LONDON).date()
    for row in _scheduled_reels(conn):
        when = posts_at(row["scheduled_for"], row["created_at"])
        if _eligible(row) and when.astimezone(LONDON).date() == today and now <= when + STALE_AFTER:
            return True
    return False


def _reschedule_series_reels(
    conn, env: dict, now: datetime, *, day_full: bool = False, in_series: bool = False
) -> None:
    rows = _scheduled_reels(conn)
    local = now.astimezone(LONDON)
    today = local.date()
    first_open = today + timedelta(days=1) if day_full else today
    occupied = {
        slot.date()
        for row in rows
        if _eligible(row) and (slot := _instant(row["scheduled_for"]).astimezone(LONDON)).date() >= first_open
    }
    with conn:
        for row in rows:
            slot = _instant(row["scheduled_for"]).astimezone(LONDON)
            earlier_series_slot = slot.date() < today and recipe_series.active(conn, slot)
            off_day = earlier_series_slot and not recipe_series.posting_day(env, slot)
            eligible = _eligible(row)
            within_grace = now <= posts_at(row["scheduled_for"], row["created_at"]) + STALE_AFTER
            overnight = in_series and earlier_series_slot and eligible and within_grace
            overflow = day_full and slot.date() == today and eligible and within_grace
            if not (off_day or overnight or overflow):
                continue
            target = datetime.combine(first_open, slot.time(), LONDON)
            while (
                target + STALE_AFTER < now
                or target.date() in occupied
                or (recipe_series.active(conn, target) and not recipe_series.posting_day(env, target))
            ):
                target += timedelta(days=1)
            conn.execute(
                "UPDATE drafts SET scheduled_for=? WHERE draft_id=? AND scheduled_for=? AND status IN ('approved','pending')",
                (target.isoformat(timespec="seconds"), row["draft_id"], row["scheduled_for"]),
            )
            if eligible:
                occupied.add(target.date())


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
    story: bool = False,
) -> str:
    """A REELS container from the clip's public URL, polled until Meta has processed it, then media_publish."""
    reel = json.loads(row["reel"] or "{}")
    name = str(reel.get("file", ""))
    if name not in ("reel.mp4", "reel.mov"):
        raise PublishError("the reel's video is missing")
    source = media_dir / row["draft_id"] / name
    if source.parent.is_symlink() or source.is_symlink() or not source.is_file():
        raise PublishError("the reel's video is missing")
    facts, problems = video.read(source)
    if problems or not isinstance(facts.get("seconds"), (int, float)):
        raise PublishError("video duration unavailable; upload a valid clip for the reel and Story")
    if story and facts["seconds"] > 60:
        raise StorySkipped("Story videos must be at most 60 seconds")
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
        if story:
            params = {"media_type": "STORIES", "video_url": url}
        cover = reel.get("cover_ms")
        if not story and isinstance(cover, int) and not isinstance(cover, bool) and cover > 0:
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
    raise PublishError("Feed photo and carousel publishing is disabled; use a reel")


def publish_story_once(conn, client, row, media_dir, public_dir, public_url, env, now, **kwargs):
    """Claim durably before any Story API call; uncertain outcomes are never retried."""
    draft_id = row["draft_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        reel = json.loads(current["reel"] or "{}")
        if kind(current) != "reel" or current["status"] != "published" or "story" in reel:
            conn.rollback()
            return
        reel["story"] = {"state": "started", "started_at": now.isoformat(timespec="seconds")}
        conn.execute("UPDATE drafts SET reel=? WHERE draft_id=?", (json.dumps(reel), draft_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    try:
        media_id = publish_reel(client, current, media_dir, public_dir, public_url, story=True, **kwargs)
        result = {"state": "published", "media_id": media_id, "permalink": permalink(client, media_id)}
    except StorySkipped as exc:
        result = {"state": "skipped", "reason": str(exc)}
        notify(
            "instagram",
            "ok",
            "Reel is live; Story skipped because the video exceeds 60 seconds",
            command_centre_link(env, "instagram"),
            key=f"instagram-story-skipped:{draft_id}",
            env=env,
        )
    except Exception as exc:
        result = {"state": "failed", "error": type(exc).__name__}
        notify(
            "instagram",
            "failed",
            "Reel is live, but its Story failed; not retried",
            command_centre_link(env, "instagram"),
            env=env,
        )
    with conn:
        latest = json.loads(conn.execute("SELECT reel FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()[0])
        latest["story"] = result
        conn.execute("UPDATE drafts SET reel=? WHERE draft_id=?", (json.dumps(latest), draft_id))


def permalink(client: GraphClient, media_id: str) -> str | None:
    """The post's Instagram link, best effort."""
    try:
        value = client.get(media_id, {"fields": "permalink"}).get("permalink")
    except Exception:  # the post is already live: nothing here may stop the tick
        return None
    return value if isinstance(value, str) and value.startswith("https://") else None


def valid_public_url(url: str) -> bool:
    if not url.startswith("https://") or "?" in url or "#" in url:
        return False
    if any(ord(char) < 33 or ord(char) == 127 for char in url):
        return False
    try:
        parts = urlsplit(url)
        return bool(parts.hostname and parts.port != 0 and "@" not in parts.netloc)
    except ValueError:
        return False


def ready(env: dict) -> bool:
    """The switch and the public folder are set. Posting also needs the token and user ID (`configured`)."""
    public_dir, public_url = env.get("INSTAGRAM_PUBLIC_DIR"), env.get("INSTAGRAM_PUBLIC_URL", "")
    return bool(env.get("INSTAGRAM_AUTO_PUBLISH") == "1" and public_dir and valid_public_url(public_url))


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
    legacy = conn.execute(
        "SELECT draft_id FROM drafts WHERE status IN ('pending','approved') AND COALESCE(kind,'carousel')!='reel'"
    ).fetchall()
    for old in legacy:
        notify(
            "instagram",
            "failed",
            f"Legacy draft {old['draft_id']} skipped: only reels can publish",
            page,
            key=f"instagram-legacy-skipped:{old['draft_id']}",
            env=env,
        )
    series = recipe_series.active(conn, now, including_launch_day=True)
    off_day = series and not recipe_series.posting_day(env, now)
    day_full = series and not off_day and recipe_series.already_published(conn, now)
    if recipe_series.started(conn):
        _reschedule_series_reels(conn, env, now, day_full=day_full, in_series=series)
    if off_day or day_full:
        return "published 0, failed 0"
    for row in due(conn, now):
        if kind(row) != "reel":
            continue
        if series and _instant(row["scheduled_for"]).astimezone(LONDON).date() != now.astimezone(LONDON).date():
            continue
        stamp = now.isoformat(timespec="seconds")

        sent = []

        def started(draft_id=row["draft_id"], stamp=stamp, sent=sent):
            # Re-check under a write lock: a Reject during preparation must win.
            conn.execute("BEGIN IMMEDIATE")
            try:
                now_row = conn.execute(
                    "SELECT status, review_score, kind, reel FROM drafts WHERE draft_id=?", (draft_id,)
                ).fetchone()
                if kind(now_row) != "reel" or not will_post(now_row["status"], now_row["review_score"], held(now_row)):
                    raise Withdrawn("rejected or held before posting")
                _record(conn, draft_id, stamp, "started")
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            sent.append(True)

        try:
            media_id = publish_reel(client, row, media_dir, Path(public_dir), public_url, on_publish=started, **kwargs)
        except Withdrawn:
            continue
        except (PublishError, GraphAPIError, OSError, ValueError) as exc:
            error = str(exc) if isinstance(exc, (PublishError, GraphAPIError)) else type(exc).__name__
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                now_row = conn.execute("SELECT status, kind, reel FROM drafts WHERE draft_id=?", (row["draft_id"],))
                now_row = now_row.fetchone()
                withdrawn = now_row and (
                    now_row["status"] == "rejected" or now_row["status"] == "pending" and held(now_row)
                )
                if not sent and withdrawn:
                    continue  # rejected or held during preparation: the owner decided, not a failure
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
        if series:
            _reschedule_series_reels(conn, env, now, day_full=True)
        try:
            with conn:
                storage.link_published(conn, row["draft_id"], media_id)
        except sqlite3.IntegrityError:
            pass  # linked once sync has stored the post
        link = permalink(client, media_id) or page
        notify("instagram", "ok", f"Instagram post is live: {row['topic']}", link, env=env)
        try:
            publish_story_once(conn, client, row, media_dir, Path(public_dir), public_url, env, now, **kwargs)
        except Exception:
            notify("instagram", "failed", "Reel is live; Story recording failed, check Instagram", page, env=env)
        published += 1
        if series:
            break
    return f"published {published}, failed {failed}"
