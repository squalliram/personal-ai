"""Rankings and suggestions from the stored data. Every score here is a heuristic suggestion."""

from __future__ import annotations

import sqlite3
import statistics
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def hashtags(text: str | None) -> list[str]:
    """Tags as Instagram reads them, lower-cased: letters, digits, underscores and combining marks."""
    text, tags, i = text or "", [], 0
    while (i := text.find("#", i)) != -1:
        end = i + 1
        while end < len(text) and (text[end] == "_" or unicodedata.category(text[end])[0] in "LNM"):
            end += 1
        if end > i + 1 and (i == 0 or text[i - 1].isspace()):
            tag = text[i:end].lower()
            if tag not in tags:
                tags.append(tag)
        i = end
    return tags


def _instant(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


def _since(days: int, now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


def benchmark_rankings(conn: sqlite3.Connection) -> list[dict]:
    """Accounts by reach proxy: followers, then median likes + comments per post."""
    ranked = []
    for account in conn.execute("SELECT username, followers FROM benchmark_accounts WHERE status='ok'"):
        engagement = [
            (row["likes"] or 0) + (row["comments"] or 0)
            for row in conn.execute("SELECT likes, comments FROM benchmark_posts WHERE username=?", (account[0],))
        ]
        median = statistics.median(engagement) if engagement else 0
        followers = account["followers"] or 0
        ranked.append(
            {
                "username": account["username"],
                "followers": followers,
                "median_engagement": median,
                "engagement_rate": round(median / followers, 4) if followers else None,
            }
        )
    return sorted(ranked, key=lambda a: (a["followers"], a["median_engagement"]), reverse=True)


def top_benchmark_posts(conn: sqlite3.Connection, *, days: int = 30, limit: int = 20, now=None) -> list[dict]:
    """Recent posts that beat their own account's median, the outlier multiple the skill pack uses."""
    since = _since(days, now)
    medians, posts = {}, []
    for row in conn.execute(
        "SELECT p.*, a.followers FROM benchmark_posts p JOIN benchmark_accounts a USING(username) "
        "WHERE a.status='ok' AND p.posted_at IS NOT NULL"
    ):
        engagement = (row["likes"] or 0) + (row["comments"] or 0)
        medians.setdefault(row["username"], []).append(engagement)
        if _instant(row["posted_at"]) >= since:
            posts.append((row, engagement))
    results = []
    for row, engagement in posts:
        baseline = statistics.median(medians[row["username"]]) or 1
        results.append(
            {
                "username": row["username"],
                "permalink": row["permalink"],
                "media_type": row["media_type"],
                "posted_at": row["posted_at"],
                "likes": row["likes"],
                "comments": row["comments"],
                "outlier": round(engagement / baseline, 2),
                "caption": row["caption"],
                "hashtags": hashtags(row["caption"]),
            }
        )
    return sorted(results, key=lambda p: (p["outlier"], p["likes"] or 0), reverse=True)[:limit]


def trending_hashtags(conn: sqlite3.Connection, *, days: int = 30, limit: int = 20, now=None) -> list[dict]:
    """Tags weighted by how far the posts using them beat their account's median."""
    weights: dict[str, dict] = {}
    for post in top_benchmark_posts(conn, days=days, limit=10_000, now=now):
        for tag in post["hashtags"]:
            entry = weights.setdefault(tag, {"tag": tag, "score": 0.0, "posts": 0, "accounts": set()})
            entry["score"] += post["outlier"]
            entry["posts"] += 1
            entry["accounts"].add(post["username"])
    ranked = sorted(weights.values(), key=lambda t: (len(t["accounts"]) > 1, t["score"]), reverse=True)
    return [{**t, "score": round(t["score"], 2), "accounts": len(t["accounts"])} for t in ranked[:limit]]


def post_performance(conn: sqlite3.Connection) -> list[dict]:
    """Each own post's latest snapshot, scored as weighted interactions per account reached."""
    rows = conn.execute(
        "SELECT p.media_id, p.posted_at, p.permalink, p.media_type, m.* FROM posts p JOIN post_metrics m USING(media_id) "
        "WHERE m.captured_on = (SELECT MAX(captured_on) FROM post_metrics WHERE media_id=p.media_id) "
        "ORDER BY p.posted_at DESC"
    )
    results = []
    for row in rows:
        weighted = (row["likes"] or 0) + 2 * (row["comments"] or 0) + 3 * ((row["saved"] or 0) + (row["shares"] or 0))
        results.append(
            {
                "media_id": row["media_id"],
                "posted_at": row["posted_at"],
                "permalink": row["permalink"],
                "reach": row["reach"],
                "likes": row["likes"],
                "comments": row["comments"],
                "saved": row["saved"],
                "shares": row["shares"],
                "score": round(weighted / row["reach"], 4) if row["reach"] else None,
            }
        )
    return results


def best_hours(
    conn: sqlite3.Connection, *, top: int = 3, audience_tz: str = "UTC", min_posts: int = 5, now: datetime | None = None
) -> list[int]:
    """London hours to publish: followers online, blended with own reach by hour once there are enough posts."""
    audience = {}
    zone = ZoneInfo(audience_tz)
    today = (now or datetime.now(timezone.utc)).astimezone(LONDON).date()
    for row in conn.execute("SELECT hour, followers_online FROM audience_hours"):
        local = datetime(today.year, today.month, today.day, row[0], tzinfo=zone).astimezone(LONDON).hour
        audience[local] = row[1]
    peak = max(audience.values(), default=0) or 1
    blended = {hour: count / peak for hour, count in audience.items()}
    reach_by_hour: dict[int, list[int]] = {}
    for post in post_performance(conn):
        if post["reach"]:
            reach_by_hour.setdefault(_instant(post["posted_at"]).astimezone(LONDON).hour, []).append(post["reach"])
    if sum(len(v) for v in reach_by_hour.values()) >= min_posts:
        best = max(statistics.mean(v) for v in reach_by_hour.values())
        for hour, reaches in reach_by_hour.items():
            own = statistics.mean(reaches) / best
            blended[hour] = (blended[hour] + own) / 2 if hour in blended else own
    return [hour for hour, _ in sorted(blended.items(), key=lambda item: (-item[1], item[0]))[:top]]


def learned_examples(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Approved posts ranked by how they performed, and rejected drafts to steer away from."""
    scores = {p["media_id"]: p["score"] for p in post_performance(conn)}
    approved, rejected = [], []
    for row in conn.execute("SELECT * FROM examples ORDER BY decided_at DESC"):
        item = {
            "draft_id": row["draft_id"],
            "topic": row["topic"],
            "caption": row["caption"],
            "hashtags": row["hashtags"],
        }
        if row["decision"] == "rejected":
            rejected.append(item)
        else:
            approved.append({**item, "media_id": row["media_id"], "score": scores.get(row["media_id"])})
    known = [a["score"] for a in approved if a["score"] is not None]
    baseline = statistics.median(known) if known else None
    for item in approved:
        item["weight"] = round(item["score"] / baseline, 2) if baseline and item["score"] is not None else 1.0
    approved.sort(key=lambda a: a["weight"], reverse=True)
    return {"approved": approved, "rejected": rejected}
