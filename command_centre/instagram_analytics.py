"""Bounded, read-only Instagram chart data. Missing measurements stay missing.

Publishing records, synced media and reports are merged by media ID, never by
draft ID: the reel and its matching Story are two separate pieces of media.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

WINDOW_DAYS = 90
MAX_MEDIA = 300
METRICS = ("views", "reach", "likes", "comments", "saved", "shares", "follows", "profile_visits", "replies")


def _time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _rows(conn, query) -> list[dict]:
    try:
        return [dict(row) for row in conn.execute(query)]
    except sqlite3.OperationalError:
        return []  # Older read-only databases may not have these tables/columns.


def _format(value) -> str:
    value = str(value or "").lower()
    return "story" if value in ("story", "stories") else "reel" if value in ("reel", "reels") else "post"


def performance(conn: sqlite3.Connection, now: datetime) -> dict:
    since = now - timedelta(days=WINDOW_DAYS)
    media = {}

    def add(row, *, basis="instagram"):
        posted = _time(row.get("posted_at"))
        if posted is None or not since <= posted <= now or not row.get("media_id"):
            return None
        item = media.setdefault(
            row["media_id"],
            {
                "media_id": row["media_id"],
                "posted_at": posted.isoformat(timespec="seconds"),
                "time_basis": basis,
                "format": _format(row.get("format") or row.get("media_product_type")),
                "topic": row.get("topic") or (row.get("caption") or "")[:100] or "Published media",
                "permalink": row.get("permalink"),
                "draft_id": row.get("draft_id"),
                "snapshots": {},
            },
        )
        if item["time_basis"] == "publish_record" and basis == "instagram":
            item["posted_at"] = posted.isoformat(timespec="seconds")
            item["time_basis"] = basis
        for key in ("permalink", "draft_id", "topic"):
            if row.get(key):
                item[key] = row[key]
        if row.get("format") or row.get("media_product_type"):
            item["format"] = _format(row.get("format") or row.get("media_product_type"))
        return item

    # SELECT * tolerates columns absent on older installations. No migrations or API calls.
    posts = _rows(conn, "SELECT * FROM posts ORDER BY posted_at DESC LIMIT 1000")
    for row in posts:
        add(row)
    for row in _rows(conn, "SELECT * FROM story_insights ORDER BY posted_at DESC LIMIT 1000"):
        add({**row, "format": "story"})
    for row in _rows(
        conn,
        "SELECT r.media_id,r.started_at AS posted_at,r.draft_id,d.topic,d.kind AS format "
        "FROM publish_runs r JOIN drafts d USING(draft_id) WHERE r.outcome='published' "
        "ORDER BY r.started_at DESC LIMIT 1000",
    ):
        add(row, basis="publish_record")
    for row in _rows(conn, "SELECT * FROM post_reports ORDER BY posted_at DESC LIMIT 2000"):
        item = add(row)
        if item is None or not row.get("captured_at") or row.get("checkpoint") not in ("24h", "72h"):
            continue
        item["snapshots"][row["checkpoint"]] = {
            "captured_at": row["captured_at"],
            **{
                name: row.get(name) if isinstance(row.get(name), (int, float)) and row[name] >= 0 else None
                for name in METRICS
            },
        }
    items = sorted(media.values(), key=lambda item: item["posted_at"], reverse=True)
    syncs = [row["synced_at"] for row in posts if _time(row.get("synced_at"))]
    return {
        "window_days": WINDOW_DAYS,
        "limit": MAX_MEDIA,
        "truncated": len(items) > MAX_MEDIA,
        "last_synced_at": max(syncs, default=None),
        "items": items[:MAX_MEDIA],
    }
