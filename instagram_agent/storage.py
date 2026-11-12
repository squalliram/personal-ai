from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DATABASE = Path("data/instagram.sqlite3")


def connect(path: Path = DEFAULT_DATABASE) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def upsert_post(conn: sqlite3.Connection, media: dict, synced_at: str) -> None:
    conn.execute(
        "INSERT INTO posts(media_id,posted_at,media_type,permalink,caption,likes,comments,synced_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET caption=excluded.caption, "
        "likes=excluded.likes, comments=excluded.comments, synced_at=excluded.synced_at",
        (
            media["id"],
            media["timestamp"],
            media.get("media_type"),
            media.get("permalink"),
            media.get("caption"),
            media.get("like_count"),
            media.get("comments_count"),
            synced_at,
        ),
    )


def upsert_metrics(conn: sqlite3.Connection, media_id: str, captured_on: str, values: dict[str, int]) -> None:
    columns = ("reach", "likes", "comments", "saved", "shares", "views", "total_interactions")
    conn.execute(
        f"INSERT INTO post_metrics(media_id,captured_on,{','.join(columns)}) VALUES(?,?,{','.join('?' * len(columns))}) "
        f"ON CONFLICT(media_id,captured_on) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in columns)}",
        (media_id, captured_on, *(values.get(c) for c in columns)),
    )


def replace_audience_hours(conn: sqlite3.Connection, hours: dict[int, int], captured_at: str) -> None:
    if not hours:
        return
    conn.execute("DELETE FROM audience_hours")
    conn.executemany(
        "INSERT INTO audience_hours(hour,followers_online,captured_at) VALUES(?,?,?)",
        [(hour, count, captured_at) for hour, count in sorted(hours.items())],
    )


def add_benchmarks(conn: sqlite3.Connection, usernames: list[str], source: str) -> int:
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO benchmark_accounts(username,source) VALUES(?,?) ON CONFLICT(username) DO NOTHING",
        [(u, source) for u in usernames],
    )
    return conn.total_changes - before


def update_benchmark(conn: sqlite3.Connection, username: str, found: dict, synced_at: str) -> None:
    if not found:
        conn.execute(
            "UPDATE benchmark_accounts SET status='unavailable', synced_at=? WHERE username=?", (synced_at, username)
        )
        return
    conn.execute(
        "UPDATE benchmark_accounts SET followers=?, media_count=?, status='ok', synced_at=? WHERE username=?",
        (found.get("followers_count"), found.get("media_count"), synced_at, username),
    )
    for media in (found.get("media") or {}).get("data", []):
        conn.execute(
            "INSERT INTO benchmark_posts(media_id,username,posted_at,media_type,permalink,caption,likes,comments,views,synced_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET likes=excluded.likes, "
            "comments=excluded.comments, views=excluded.views, caption=excluded.caption, synced_at=excluded.synced_at",
            (
                media["id"],
                username,
                media.get("timestamp"),
                media.get("media_type"),
                media.get("permalink"),
                media.get("caption"),
                media.get("like_count"),
                media.get("comments_count"),
                media.get("view_count"),
                synced_at,
            ),
        )


def mark_benchmark_failed(conn: sqlite3.Connection, username: str, synced_at: str) -> None:
    conn.execute(
        "UPDATE benchmark_accounts SET synced_at=?, "
        "status=CASE WHEN status='ok' THEN 'ok' ELSE 'unavailable' END WHERE username=?",
        (synced_at, username),
    )


def record_decision(
    conn: sqlite3.Connection,
    draft_id: str,
    decision: str,
    decided_at: str,
    *,
    topic: str | None = None,
    caption: str | None = None,
    hashtags: list[str] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO examples(draft_id,decision,decided_at,topic,caption,hashtags) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(draft_id) DO UPDATE SET decision=excluded.decision, decided_at=excluded.decided_at",
        (draft_id, decision, decided_at, topic, caption, " ".join(hashtags or [])),
    )


def link_published(conn: sqlite3.Connection, draft_id: str, media_id: str) -> None:
    conn.execute("UPDATE examples SET media_id=? WHERE draft_id=? AND decision='approved'", (media_id, draft_id))
