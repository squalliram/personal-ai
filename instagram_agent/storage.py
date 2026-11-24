from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import clips, uploads

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DATABASE = Path("data/instagram.sqlite3")


def connect(path: Path = DEFAULT_DATABASE, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(uploads.TABLE)
    conn.execute(clips.TABLE)
    _add_columns(conn)
    return conn


# Columns added after the first release, for databases made before them.
LATER_COLUMNS = {
    "token_state": {"token_fingerprint": "TEXT"},
    "posts": {"media_product_type": "TEXT"},
    "post_metrics": {"avg_watch_ms": "INTEGER", "total_watch_ms": "INTEGER"},
    "story_insights": {"refresh_complete": "INTEGER NOT NULL DEFAULT 1"},
    "post_reports": {
        "follows": "INTEGER",
        "profile_visits": "INTEGER",
        "replies": "INTEGER",
        "navigation": "INTEGER",
        "reported_at": "TEXT",
    },
    "drafts": {
        "kind": "TEXT NOT NULL DEFAULT 'carousel' CHECK (kind IN ('carousel', 'reel'))",
        "reel": "TEXT",
    },
}


def _add_columns(conn: sqlite3.Connection) -> None:
    with conn:
        for table, columns in LATER_COLUMNS.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, spec in columns.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def upsert_post(conn: sqlite3.Connection, media: dict, synced_at: str) -> None:
    conn.execute(
        "INSERT INTO posts(media_id,posted_at,media_type,permalink,caption,likes,comments,synced_at,"
        "media_product_type) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET caption=excluded.caption, "
        "likes=excluded.likes, comments=excluded.comments, synced_at=excluded.synced_at, "
        "media_product_type=COALESCE(excluded.media_product_type, media_product_type)",
        (
            media["id"],
            media["timestamp"],
            media.get("media_type"),
            media.get("permalink"),
            media.get("caption"),
            media.get("like_count"),
            media.get("comments_count"),
            synced_at,
            media.get("media_product_type"),
        ),
    )


def upsert_metrics(conn: sqlite3.Connection, media_id: str, captured_on: str, values: dict[str, int]) -> None:
    columns = (
        "reach",
        "likes",
        "comments",
        "saved",
        "shares",
        "views",
        "total_interactions",
        "avg_watch_ms",
        "total_watch_ms",
    )
    conn.execute(
        f"INSERT INTO post_metrics(media_id,captured_on,{','.join(columns)}) VALUES(?,?,{','.join('?' * len(columns))}) "
        f"ON CONFLICT(media_id,captured_on) DO UPDATE SET {','.join(f'{c}=COALESCE(excluded.{c},{c})' for c in columns)}",
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


def link_published_drafts(conn: sqlite3.Connection) -> None:
    """Approved examples whose auto-published post has now been synced into `posts`."""
    conn.execute(
        "UPDATE examples SET media_id=(SELECT d.media_id FROM drafts d WHERE d.draft_id=examples.draft_id) "
        "WHERE decision='approved' AND media_id IS NULL AND EXISTS (SELECT 1 FROM drafts d JOIN posts p "
        "ON p.media_id=d.media_id WHERE d.draft_id=examples.draft_id AND d.status='published')"
    )


def link_published(conn: sqlite3.Connection, draft_id: str, media_id: str) -> None:
    conn.execute("UPDATE examples SET media_id=? WHERE draft_id=? AND decision='approved'", (media_id, draft_id))


def insert_draft(conn: sqlite3.Connection, draft: dict, created_on: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO drafts(draft_id,created_on,created_at,category,topic,angle,slides,caption,hashtags,"
        "review_score,review_notes,scheduled_for,kind,reel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            draft["draft_id"],
            created_on,
            created_at,
            draft["category"],
            draft["topic"],
            draft.get("angle"),
            json.dumps([{k: s[k] for k in ("text", "file") if k in s} for s in draft["slides"]], ensure_ascii=False),
            draft["caption"],
            " ".join(draft["hashtags"]),
            draft.get("review_score"),
            draft.get("review_notes"),
            draft.get("scheduled_for"),
            draft.get("kind", "carousel"),
            json.dumps(draft["reel"], ensure_ascii=False) if draft.get("reel") else None,
        ),
    )


def drafts_on(conn: sqlite3.Connection, day: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM drafts WHERE created_on=?", (day,)).fetchone()[0]


def record_run(
    conn: sqlite3.Connection, run_on: str, started_at: str, outcome: str, drafts: int = 0, error=None
) -> None:
    conn.execute(
        "INSERT INTO daily_runs(run_on,started_at,outcome,drafts,error) VALUES(?,?,?,?,?)",
        (run_on, started_at, outcome, drafts, error),
    )


def runs_on(conn: sqlite3.Connection, day: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM daily_runs WHERE run_on=? AND outcome NOT IN ('skipped','not_configured')", (day,)
    ).fetchone()[0]


_LATEST_RUN_STARTED = (
    "SELECT 1 FROM publish_runs r WHERE r.draft_id=? AND r.outcome='started' AND r.id="
    "(SELECT MAX(id) FROM publish_runs s WHERE s.draft_id=r.draft_id)"
)


def publishing_started(conn: sqlite3.Connection, draft_id: str) -> bool:
    """True once the publisher has begun the final publish call for this draft."""
    return conn.execute(_LATEST_RUN_STARTED, (draft_id,)).fetchone() is not None


def hold_reel(conn: sqlite3.Connection, draft_id: str, hold: bool, version: int | None = None) -> str:
    """Hold a pending reel for approval, or release it: ok, unchanged, not_found, not_pending or hold_changed.

    `version` is the hold version the page showed. Each change bumps it, so a delayed request from an
    out-of-date page is refused unless it asks for the hold already stored."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT kind, status, reel FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None or row[0] != "reel":
            return "not_found"
        if row[1] != "pending" or publishing_started(conn, draft_id):
            return "not_pending"
        try:
            reel = json.loads(row[2] or "{}")
        except ValueError:
            reel = {}
        reel = reel if isinstance(reel, dict) else {}
        current = reel.get("hold") is True
        stored = reel.get("hold_version")
        stored = stored if isinstance(stored, int) and not isinstance(stored, bool) else 0
        if current == hold:
            return "unchanged"
        if version is not None and version != stored:
            return "hold_changed"
        reel["hold"] = bool(hold)
        reel["hold_version"] = stored + 1
        conn.execute("UPDATE drafts SET reel=? WHERE draft_id=?", (json.dumps(reel), draft_id))
    return "ok"


def decide_draft(conn: sqlite3.Connection, draft_id: str, decision: str, decided_at: str) -> bool:
    """Move a pending draft to approved/rejected and remember it. False if it was not pending."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved or rejected")
    with conn:
        changed = conn.execute(
            "UPDATE drafts SET status=?, decided_at=? WHERE draft_id=? AND status='pending' AND NOT EXISTS "
            "(SELECT 1 FROM publish_runs r WHERE r.draft_id=drafts.draft_id AND r.outcome='started' AND r.id="
            "(SELECT MAX(id) FROM publish_runs s WHERE s.draft_id=r.draft_id))",
            (decision, decided_at, draft_id),
        ).rowcount
        if not changed:
            return False
        row = conn.execute("SELECT topic, caption, hashtags FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        record_decision(
            conn, draft_id, decision, decided_at, topic=row[0], caption=row[1], hashtags=(row[2] or "").split()
        )
    return True


def token_state(conn) -> dict:
    """Non-secret token lifecycle metadata. The access token itself is never stored here."""
    row = conn.execute(
        "SELECT token_fingerprint,expires_at,last_refreshed_at,last_attempt_at,last_outcome,last_error "
        "FROM token_state WHERE id=1"
    ).fetchone()
    return (
        dict(row)
        if row
        else {
            "token_fingerprint": None,
            "expires_at": None,
            "last_refreshed_at": None,
            "last_attempt_at": None,
            "last_outcome": None,
            "last_error": None,
        }
    )


def record_token_status(conn, outcome: str) -> None:
    """Remember a configuration state without recording an API attempt."""
    with conn:
        conn.execute(
            "INSERT INTO token_state(id,last_outcome) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET "
            "token_fingerprint=NULL,expires_at=NULL,last_refreshed_at=NULL,last_attempt_at=NULL,"
            "last_outcome=excluded.last_outcome,last_error=NULL",
            (outcome,),
        )


def record_token_success(conn, expires_at: str, stamp: str, *, fingerprint: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO token_state(id,token_fingerprint,expires_at,last_refreshed_at,last_attempt_at,last_outcome) "
            "VALUES(1,?,?,?,?,'refreshed') ON CONFLICT(id) DO UPDATE SET "
            "token_fingerprint=excluded.token_fingerprint,expires_at=excluded.expires_at,"
            "last_refreshed_at=excluded.last_refreshed_at,last_attempt_at=excluded.last_attempt_at,"
            "last_outcome='refreshed',last_error=NULL",
            (fingerprint, expires_at, stamp, stamp),
        )


def record_token_failure(conn, stamp: str, error: str, *, fingerprint: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO token_state(id,token_fingerprint,last_attempt_at,last_outcome,last_error) "
            "VALUES(1,?,?,'failed',?) ON CONFLICT(id) DO UPDATE SET "
            "expires_at=CASE WHEN token_fingerprint=excluded.token_fingerprint THEN expires_at END,"
            "last_refreshed_at=CASE WHEN token_fingerprint=excluded.token_fingerprint THEN last_refreshed_at END,"
            "token_fingerprint=excluded.token_fingerprint,last_attempt_at=excluded.last_attempt_at,"
            "last_outcome='failed',last_error=excluded.last_error",
            (fingerprint, stamp, error[:300]),
        )
