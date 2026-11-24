"""Read-only checks for one controlled first automatic Instagram post."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import drafts, publish, render, storage, video
from .analysis import LONDON
from .drafts import DRAFT_ID_RE
from .graph import GraphAPIError

EXPECTED_USERNAME = "instafoodielove"


def _eligible_drafts(conn, now: datetime) -> list[str]:
    """Drafts the normal cron could post, including future slots and reels waiting to render."""
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM publish_runs r WHERE r.draft_id=d.draft_id AND r.outcome='failed') "
        "AS fails FROM drafts d WHERE d.status IN ('approved','pending') AND d.scheduled_for IS NOT NULL "
        "ORDER BY d.scheduled_for,d.draft_id"
    ).fetchall()
    eligible = []
    for row in rows:
        if publish.kind(row) != "reel":
            continue
        try:
            slot = row["scheduled_for"]
            if not publish.rendered(row):
                if json.loads(row["reel"] or "{}").get("render") != "pending":
                    continue
                slot = render.slot_after_render(row, now)
            when = publish.posts_at(slot, row["created_at"])
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            when
            and now <= when + publish.STALE_AFTER
            and publish.will_post(row["status"], row["review_score"], publish.held(row))
            and bool(row["decided_at"])
            and row["fails"] < publish.MAX_ATTEMPTS
            and not storage.publishing_started(conn, row["draft_id"])
        ):
            eligible.append(row["draft_id"])
    return eligible


def _public_error(env: dict) -> str | None:
    directory, public_url = env.get("INSTAGRAM_PUBLIC_DIR"), env.get("INSTAGRAM_PUBLIC_URL", "")
    if not directory or not public_url:
        return "Set INSTAGRAM_PUBLIC_DIR and INSTAGRAM_PUBLIC_URL"
    if not publish.valid_public_url(public_url):
        return "Public media URL must start with https:// and have a valid host and port, without credentials, query or fragment"
    path = Path(directory)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        return "Public media folder is not a safe directory"
    return None


def _media_error(row, media_dir: Path) -> str | None:
    if publish.kind(row) != "reel":
        return "Legacy photo and carousel drafts cannot publish; choose a rendered reel"
    try:
        reel = json.loads(row["reel"] or "{}")
    except (TypeError, ValueError):
        return "Draft reel data is invalid"
    if not isinstance(reel, dict) or not publish.rendered(row):
        return "Reel rendering is not complete"
    caption, hashtags = row["caption"] or "", row["hashtags"] or ""
    caption_length = len(caption) + (2 + len(hashtags) if hashtags else 0)
    if caption_length > drafts.MAX_CAPTION:
        return "Caption and hashtags exceed 2200 characters"
    folder = media_dir / row["draft_id"]
    if folder.is_symlink():
        return "Draft media folder is not safe"
    name = reel.get("file")
    if name not in ("reel.mp4", "reel.mov"):
        return "Rendered reel video is missing"
    path = folder / name
    if path.is_symlink() or not path.is_file():
        return "Rendered reel video is missing or unsafe"
    facts, problems = video.check(path)
    if problems:
        return "Rendered reel video is invalid"
    if not isinstance(facts.get("seconds"), (int, float)) or facts["seconds"] > video.MAX_SECONDS:
        return "Reel video duration must be at most 90 seconds"
    return None


def run(conn, env: dict, media_dir: Path, now: datetime, *, draft_id: str, client=None) -> dict:
    """Return safe checklist results. Meta access is limited to one account GET; never call post."""
    report = {"ok": False, "draft_id": draft_id, "auto_publish": False, "checks": [], "eligible_drafts": []}

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    automatic = env.get("INSTAGRAM_AUTO_PUBLISH") == "1"
    report["auto_publish"] = automatic
    check(
        "automatic_publishing",
        True,
        "ON; the normal cron can publish eligible drafts" if automatic else "OFF; preflight changes no settings",
    )
    token, user_id = env.get("INSTAGRAM_ACCESS_TOKEN", ""), env.get("INSTAGRAM_USER_ID", "")
    configured = bool(token and user_id and user_id.isdigit())
    check(
        "credentials",
        configured,
        "Token and numeric user ID are present" if configured else "Token or user ID is missing",
    )
    if configured:
        try:
            if client is None:
                from .cli import graph_client

                client = graph_client(env)
            account = client.get(user_id, {"fields": "id,username"})
            account_ok = (
                isinstance(account, dict)
                and account.get("id") == user_id
                and isinstance(account.get("username"), str)
                and account["username"].casefold() == EXPECTED_USERNAME
            )
            check(
                "account",
                account_ok,
                f"Account ID and username match @{EXPECTED_USERNAME}"
                if account_ok
                else f"Account ID or username does not match @{EXPECTED_USERNAME}",
            )
        except GraphAPIError as exc:
            check("account", False, str(exc))
        except ValueError as exc:
            check("account", False, type(exc).__name__)

    row = None
    if DRAFT_ID_RE.fullmatch(draft_id):
        row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    check("selected_draft", row is not None, "Selected draft exists" if row else "Selected draft was not found")
    if row:
        eligible = (
            bool(row["decided_at"])
            and publish.rendered(row)
            and publish.will_post(row["status"], row["review_score"], publish.held(row))
        )
        check(
            "draft_eligibility",
            eligible,
            "Owner approved this draft and visual review is complete"
            if eligible
            else "Explicit owner approval and completed visual review are required",
        )
        try:
            when = publish.posts_at(row["scheduled_for"], row["created_at"])
        except (TypeError, ValueError):
            when = None
        schedule_ok = when is not None and now <= when + publish.STALE_AFTER
        check(
            "publish_window",
            schedule_ok,
            f"Publish time {when.astimezone(LONDON).isoformat(timespec='minutes')} is not more than 3 hours past"
            if schedule_ok
            else "Publish time is missing, invalid, or more than 3 hours past",
        )
        try:
            media_error = _media_error(row, Path(media_dir))
        except OSError:
            media_error = "Draft media could not be read"
        check("draft_media", media_error is None, media_error or "Caption and rendered reel video are ready")
    public_error = _public_error(env)
    check("public_media", public_error is None, public_error or "HTTPS public media configuration is safe")

    eligible_drafts = _eligible_drafts(conn, now)
    report["eligible_drafts"] = eligible_drafts
    check(
        "single_candidate",
        eligible_drafts == [draft_id],
        "Exactly the selected draft is eligible"
        if eligible_drafts == [draft_id]
        else "Reject or resolve every other eligible draft",
    )
    report["ok"] = all(item["ok"] for item in report["checks"])
    return report
