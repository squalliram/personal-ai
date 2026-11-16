"""Read-only checks for one controlled first automatic Instagram post."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import drafts, publish, storage
from .analysis import LONDON
from .drafts import DRAFT_ID_RE
from .graph import GraphAPIError

EXPECTED_USERNAME = "instafoodielove"
JPEG_MAGIC = b"\xff\xd8\xff"


def _eligible_drafts(conn, now: datetime) -> list[str]:
    """Non-stale pending/approved drafts, including future slots that the normal cron would post."""
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM publish_runs r WHERE r.draft_id=d.draft_id AND r.outcome='failed') "
        "AS fails FROM drafts d WHERE d.status IN ('approved','pending') AND d.scheduled_for IS NOT NULL "
        "ORDER BY d.scheduled_for,d.draft_id"
    ).fetchall()
    eligible = []
    for row in rows:
        try:
            when = publish.posts_at(row["scheduled_for"], row["created_at"])
        except (TypeError, ValueError):
            continue
        if (
            when
            and now <= when + publish.STALE_AFTER
            and publish.will_post(row["status"], row["review_score"])
            and row["fails"] < publish.MAX_ATTEMPTS
            and not storage.publishing_started(conn, row["draft_id"])
        ):
            eligible.append(row["draft_id"])
    return eligible


def _public_error(env: dict) -> str | None:
    directory, public_url = env.get("INSTAGRAM_PUBLIC_DIR"), env.get("INSTAGRAM_PUBLIC_URL", "")
    if not directory or not public_url:
        return "Set INSTAGRAM_PUBLIC_DIR and INSTAGRAM_PUBLIC_URL"
    try:
        parts = urlsplit(public_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.port == 0
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            return "Public media URL must be HTTPS without credentials, query or fragment"
    except ValueError:
        return "Public media URL must be a valid HTTPS URL"
    path = Path(directory)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        return "Public media folder is not a safe directory"
    return None


def _media_error(row, media_dir: Path) -> str | None:
    try:
        slides = json.loads(row["slides"])
    except (TypeError, ValueError):
        return "Draft slide data is invalid"
    if not isinstance(slides, list) or not 2 <= len(slides) <= 20:
        return "A carousel needs 2-20 slides"
    caption, hashtags = row["caption"] or "", row["hashtags"] or ""
    caption_length = len(caption) + (2 + len(hashtags) if hashtags else 0)
    if caption_length > drafts.MAX_CAPTION:
        return "Caption and hashtags exceed 2200 characters"
    folder = media_dir / row["draft_id"]
    if folder.is_symlink():
        return "Draft media folder is not safe"
    for number in range(1, len(slides) + 1):
        path = folder / f"slide-{number}.jpg"
        if path.is_symlink() or not path.is_file():
            return f"Slide {number} is missing or is not JPEG"
        with path.open("rb") as stream:
            if stream.read(len(JPEG_MAGIC)) != JPEG_MAGIC:
                return f"Slide {number} is not JPEG"
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
        eligible = publish.will_post(row["status"], row["review_score"])
        check(
            "draft_eligibility",
            eligible,
            "Draft is approved or passed review"
            if eligible
            else "Draft is rejected, decided, or below 70 without approval",
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
        check("draft_media", media_error is None, media_error or "Caption and all JPEG slides are ready")
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
