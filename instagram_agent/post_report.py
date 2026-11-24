"""Read-only reports on each auto-published post at about 24 and 72 hours, sent through `notifications.notify`.

Each checkpoint reads the post's insights (a stored live snapshot for Stories), compares them with the
account's recent median for the same format and sends one Inbox + Telegram alert. A failed fetch is
retried on later ticks and reported once if it keeps failing. Nothing here writes to Instagram.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from notifications import command_centre_link, notify, safe_link

from . import storage
from .analysis import LONDON, hashtags
from .graph import CONVERSION_METRICS, MEDIA_FIELDS, GraphAPIError, GraphClient

# Checkpoint name, age at which it is due, and how long after that it may still be taken.
CHECKPOINTS = (("24h", timedelta(hours=24), timedelta(hours=48)), ("72h", timedelta(hours=72), timedelta(days=4)))
METRICS = (
    "reach",
    "likes",
    "comments",
    "saved",
    "shares",
    "views",
    "total_interactions",
    "avg_watch_ms",
    "total_watch_ms",
    *CONVERSION_METRICS,
    "replies",
    "navigation",
)
SHOWN = (("reach", "reach"), ("views", "views"), ("likes", "likes"), ("comments", "comments"), ("saved", "saves"),
         ("shares", "shares"), ("avg_watch_ms", "avg watch"), ("follows", "follows"),
         ("profile_visits", "profile visits"))  # fmt: skip
MAX_ATTEMPTS = 4
PER_TICK = 6
BASELINE_DAYS = 90
MIN_SAME_CHECKPOINT = 3  # fewer earlier reports at this checkpoint: compare with lifetime totals instead
SMALL_SAMPLE = 5
WORKED, DID_NOT = 1.2, 0.8
TITLE_LIMIT = 300


def parse_time(value) -> datetime | None:
    """ISO times as Meta (`+0000`) or this project (`+00:00`, `Z`) writes them."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z"):
        try:
            return datetime.strptime(value.replace("Z", "+0000"), fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def media_format(product_type: str | None, media_type: str | None) -> str:
    if product_type == "REELS":
        return "reel"
    return {"CAROUSEL_ALBUM": "carousel", "IMAGE": "photo", "VIDEO": "video"}.get(media_type or "", "post")


def caption_ask(caption: str) -> str | None:
    last = re.split(r"[.!?]+\s+|\n+", caption.strip())[-1]
    ask = re.match(
        r"\W*(?:please\s+)?(follow|save|share|comment|"
        r"send(?=\s+(?:it|this(?:\s+one)?|(?:this|the)\s+(?:post|reel|video|carousel|clip|photo))"
        r"\s+to\s+@?\w))\b",
        last,
        re.IGNORECASE,
    )
    if ask:
        action = ask[1].lower()
        return "share" if action == "send" else action
    return None


def creative_snapshot(conn, post, fmt: str, posted: datetime | None) -> dict:
    previous = conn.execute(
        "SELECT creative FROM post_reports WHERE media_id=? AND creative IS NOT NULL ORDER BY captured_at LIMIT 1",
        (post["media_id"],),
    ).fetchone()
    if previous:
        return json.loads(previous["creative"])
    draft = conn.execute(
        "SELECT caption,hashtags,slides,reel FROM drafts WHERE draft_id=?", (post["draft_id"],)
    ).fetchone()
    reel = json.loads(draft["reel"] or "{}") if draft else {}
    slides = json.loads(draft["slides"] or "[]") if draft else []
    caption = draft["caption"] if draft and fmt != "story" else ""
    return {
        "publish_slot": posted.astimezone(LONDON).isoformat(timespec="seconds") if posted else None,
        "hook": reel.get("hook")
        or (slides[0].get("text") if slides and fmt in ("carousel", "reel") else None)
        or (caption.splitlines()[0].strip() if caption else None),
        "duration_seconds": reel.get("seconds") if fmt in ("reel", "story") else None,
        "cta": caption_ask(caption),
        "hashtags": (hashtags(draft["hashtags"] or "") if fmt != "story" else []) if draft else None,
    }


def published(conn: sqlite3.Connection) -> list:
    """Published drafts and Stories, with their publish times."""
    return conn.execute(
        "SELECT d.draft_id, d.media_id, d.topic, d.kind, MIN(r.started_at) AS published_at, "
        "NULL AS story_captured_at, NULL AS story_metrics, NULL AS story_navigation FROM drafts d "
        "JOIN publish_runs r ON r.draft_id=d.draft_id AND r.outcome='published' AND r.media_id=d.media_id "
        "WHERE d.status='published' AND d.media_id IS NOT NULL GROUP BY d.draft_id "
        "UNION ALL SELECT s.draft_id, s.media_id, COALESCE(d.topic,'Instagram Story'), 'story', s.posted_at, "
        "s.captured_at, s.metrics, s.navigation_breakdown FROM story_insights s LEFT JOIN drafts d USING(draft_id)"
    ).fetchall()


def due(conn: sqlite3.Connection, now: datetime) -> list[tuple]:
    """(post, checkpoint) pairs whose checkpoint has come and has not been reported, plus exhausted checkpoints
    whose failure alert is not yet stored (retried even after the collection window closes)."""
    rows = conn.execute("SELECT media_id, checkpoint, attempts, notified_at FROM post_reports").fetchall()
    done = {(row[0], row[1]) for row in rows if row[3] is not None}
    unalerted = {(row[0], row[1]) for row in rows if row[3] is None and (row[2] or 0) >= MAX_ATTEMPTS}
    found = []
    for post in published(conn):
        at = parse_time(post["published_at"])
        if at is None:
            continue
        for name, after, grace in CHECKPOINTS:
            key = (post["media_id"], name)
            if key in unalerted or (after <= now - at < after + grace and key not in done):
                found.append((post, name))
    return sorted(found, key=lambda item: item[0]["published_at"])


def fetch(client: GraphClient, media_id: str) -> tuple[dict, dict[str, int]]:
    """The post's fields and insights, both read-only."""
    from .cli import _insights

    media = client.get(media_id, {"fields": MEDIA_FIELDS})
    media = {**media, "id": media_id}
    values = _insights(client, media)
    for metric in CONVERSION_METRICS:
        try:
            values.update(client.media_insights(media_id, metric))
        except GraphAPIError:
            continue
    return media, values


def _lifetime_rows(conn: sqlite3.Connection, fmt: str, exclude: str, since: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT p.media_id, p.posted_at, p.media_type, p.media_product_type, m.* FROM posts p JOIN post_metrics m "
        "USING(media_id) WHERE m.captured_on=(SELECT MAX(captured_on) FROM post_metrics WHERE media_id=p.media_id) "
        "AND p.media_id!=?",
        (exclude,),
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if media_format(row["media_product_type"], row["media_type"]) == fmt
        and (parse_time(row["posted_at"]) or since) >= since
    ]


def baseline(conn: sqlite3.Connection, fmt: str, checkpoint: str, exclude: str, now: datetime) -> dict:
    """Median of each metric over this format's recent posts: same checkpoint when there are enough, else lifetime."""
    since = now - timedelta(days=BASELINE_DAYS)
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM post_reports WHERE format=? AND checkpoint=? AND captured_at IS NOT NULL AND media_id!=?",
            (fmt, checkpoint, exclude),
        )
        if (parse_time(row["posted_at"]) or since) >= since
    ]
    basis = "checkpoint"
    if fmt != "story" and len(rows) < MIN_SAME_CHECKPOINT:
        lifetime = _lifetime_rows(conn, fmt, exclude, since)
        if len(lifetime) > len(rows):
            rows, basis = lifetime, "lifetime"
    medians = {}
    for metric in METRICS:
        values = [row[metric] for row in rows if isinstance(row.get(metric), int)]
        if values:
            medians[metric] = statistics.median(values)
    return {"posts": len(rows), "basis": basis, "medians": medians}


def _number(metric: str, value: int) -> str:
    return f"{value / 1000:.1f}s" if metric == "avg_watch_ms" else f"{value:,}"


def _plural(fmt: str, n: int) -> str:
    return fmt + ("" if n == 1 else "es" if fmt.endswith("ch") else "s")


def summarise(topic: str, fmt: str, checkpoint: str, posted_at: datetime | None, values: dict, base: dict) -> str:
    """One short what-worked / what-didn't line set, at most TITLE_LIMIT characters."""
    medians, ratios = base["medians"], {}
    for metric, _ in SHOWN:
        if isinstance(values.get(metric), int) and medians.get(metric):
            ratios[metric] = values[metric] / medians[metric]
    when = f", posted {posted_at.astimezone(LONDON):%d %b %H:%M}" if posted_at else ""
    head = f"{checkpoint} report: {{topic}} ({fmt}{when})."
    labels = dict(SHOWN)
    worked = [labels[m] for m, r in sorted(ratios.items(), key=lambda i: -i[1]) if r >= WORKED]
    weak = [labels[m] for m, r in sorted(ratios.items(), key=lambda i: i[1]) if r <= DID_NOT]
    if not base["posts"]:
        verdict = f"No earlier {_plural(fmt, 2)} to compare with yet."
    else:
        verdict = (f"Worked: {', '.join(worked)}. " if worked else "") + (f"Didn't: {', '.join(weak)}." if weak else "")
        verdict = verdict.strip() or "In line with the median."
        small = ", small sample" if base["posts"] < SMALL_SAMPLE else ""
        kind = f"{checkpoint} median" if base["basis"] == "checkpoint" else "lifetime median"
        verdict += f" (vs {kind} of {base['posts']} {_plural(fmt, base['posts'])}{small})"

    def stats(metrics) -> str:
        parts = []
        for metric in metrics:
            if isinstance(values.get(metric), int):
                ratio = f" ({ratios[metric]:.1f}x)" if metric in ratios else ""
                parts.append(f"{labels[metric]} {_number(metric, values[metric])}{ratio}")
            elif metric in CONVERSION_METRICS:
                parts.append(f"{labels[metric]} unavailable")
        return (", ".join(parts) + ".") if parts else "Instagram returned no numbers yet."

    for metrics in ([m for m, _ in SHOWN], ["reach", "saved", "shares", *CONVERSION_METRICS], list(CONVERSION_METRICS)):
        for limit in (60, 30):
            short = topic if len(topic) <= limit else topic[: limit - 1].rstrip() + "…"
            text = f"{head.format(topic=short)} {stats(metrics)} {verdict}"
            if len(text) <= TITLE_LIMIT:
                return text
    return text[: TITLE_LIMIT - 1] + "…"


def _attempt_failed(conn, post, checkpoint: str, env: dict, now: datetime, page: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO post_reports(media_id,checkpoint,draft_id,attempts) VALUES(?,?,?,1) "
            "ON CONFLICT(media_id,checkpoint) DO UPDATE SET attempts=attempts+1",
            (post["media_id"], checkpoint, post["draft_id"]),
        )
        attempts = conn.execute(
            "SELECT attempts FROM post_reports WHERE media_id=? AND checkpoint=?", (post["media_id"], checkpoint)
        ).fetchone()[0]
    if attempts >= MAX_ATTEMPTS:
        _gave_up(conn, post, checkpoint, env, now, page)


def _story_summary(post, checkpoint: str, posted: datetime, stamp: datetime, values: dict, base: dict) -> str:
    minutes = int((stamp - posted).total_seconds() // 60)
    head = f"{checkpoint} Story report: {post['topic'][:50]}. Captured live at {minutes // 60}h {minutes % 60}m."
    labels = (
        ("reach", "reach"), ("views", "views"), ("replies", "replies"), ("navigation", "navigation"),
        ("follows", "follows"), ("profile_visits", "profile visits"), ("shares", "shares"),
        ("total_interactions", "interactions"),
    )  # fmt: skip
    stats = ", ".join(
        f"{label} {_number(metric, values[metric])}" if metric in values else f"{label} unavailable"
        for metric, label in labels
        if metric in values or metric in CONVERSION_METRICS
    )
    text = f"{head} {stats}."
    actions = json.loads(post["story_navigation"])
    labels = {"tap_forward": "forward", "tap_back": "back", "tap_exit": "exits", "swipe_forward": "next story"}
    for key, label in labels.items():
        if key in actions:
            extra = f" {label} {actions[key]:,}."
            if len(text) + len(extra) <= TITLE_LIMIT:
                text += extra
    reach, median = values.get("reach"), base["medians"].get("reach")
    if reach is not None and median:
        extra = f" Reach {reach / median:.1f}x the median of {base['posts']} Stories."
        if len(text) + len(extra) <= TITLE_LIMIT:
            text += extra
    return text if len(text) <= TITLE_LIMIT else text[: TITLE_LIMIT - 1] + "…"


def _gave_up(conn, post, checkpoint: str, env: dict, now: datetime, page: str) -> None:
    """One failure alert once fetching is exhausted; retried on later ticks until it is stored."""
    title = f"Instagram {checkpoint} report not made: insights for {post['topic']} could not be read"
    key = f"instagram-post-report-failed:{post['media_id']}:{checkpoint}"
    if notify("instagram", "failed", title, page, key=key, env=env, now=now) is not None:
        with conn:
            conn.execute(
                "UPDATE post_reports SET notified_at=? WHERE media_id=? AND checkpoint=?",
                (now.astimezone(timezone.utc).isoformat(timespec="seconds"), post["media_id"], checkpoint),
            )


def _deliver_report(conn, post, checkpoint: str, text: str, link: str | None, env: dict, now: datetime) -> bool:
    key = f"instagram-post-report:{post['media_id']}:{checkpoint}"
    if notify("instagram", "info", text, link, key=key, env=env, now=now) is None:
        return False
    with conn:
        conn.execute(
            "UPDATE post_reports SET notified_at=? WHERE media_id=? AND checkpoint=?",
            (now.isoformat(timespec="seconds"), post["media_id"], checkpoint),
        )
    return True


def report_one(conn, client: GraphClient | None, post, checkpoint: str, env: dict, now: datetime) -> bool:
    page = command_centre_link(env, "instagram")
    previous = conn.execute(
        "SELECT attempts,captured_at,summary,permalink FROM post_reports WHERE media_id=? AND checkpoint=?",
        (post["media_id"], checkpoint),
    ).fetchone()
    if previous and previous["captured_at"]:
        return _deliver_report(conn, post, checkpoint, previous["summary"], previous["permalink"] or page, env, now)
    if previous and previous["attempts"] >= MAX_ATTEMPTS:
        _gave_up(conn, post, checkpoint, env, now, page)
        return False
    posted = parse_time(post["published_at"])
    if post["kind"] == "story":
        stamp = post["story_captured_at"]
        captured = parse_time(stamp)
        if not captured or not posted:
            _attempt_failed(conn, post, checkpoint, env, now, page)
            return False
        fmt, media, link, values = "story", {}, None, json.loads(post["story_metrics"])
    else:
        try:
            media, values = fetch(client, post["media_id"])
        except (GraphAPIError, ValueError, KeyError, TypeError, AttributeError):
            _attempt_failed(conn, post, checkpoint, env, now, page)
            return False
        fmt = (
            "reel" if post["kind"] == "reel" else media_format(media.get("media_product_type"), media.get("media_type"))
        )
        posted = parse_time(media.get("timestamp")) or posted
        link = safe_link(media.get("permalink"))
        stamp = now.isoformat(timespec="seconds")
    base = baseline(conn, fmt, checkpoint, post["media_id"], now)
    creative = creative_snapshot(conn, post, fmt, posted)
    if fmt == "story":
        text = _story_summary(post, checkpoint, posted, captured, values, base)
    else:
        text = summarise(post["topic"], fmt, checkpoint, posted, values, base)
    with conn:
        if media.get("timestamp"):
            storage.upsert_post(conn, media, stamp)
            storage.upsert_metrics(conn, post["media_id"], now.astimezone(LONDON).date().isoformat(), values)
            storage.link_published_drafts(conn)
        conn.execute(
            f"INSERT INTO post_reports(media_id,checkpoint,draft_id,format,posted_at,permalink,captured_at,reported_at,"
            f"summary,creative,{','.join(METRICS)}) VALUES(?,?,?,?,?,?,?,?,?,?,{','.join('?' * len(METRICS))}) "
            f"ON CONFLICT(media_id,checkpoint) DO UPDATE SET format=excluded.format, posted_at=excluded.posted_at, "
            f"permalink=excluded.permalink, captured_at=excluded.captured_at, reported_at=excluded.reported_at, "
            f"summary=excluded.summary, creative=excluded.creative, " + ", ".join(f"{m}=excluded.{m}" for m in METRICS),
            (
                post["media_id"],
                checkpoint,
                post["draft_id"],
                fmt,
                posted.isoformat(timespec="seconds") if posted else None,
                link,
                stamp,
                now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                text,
                json.dumps(creative),
                *(values.get(m) for m in METRICS),
            ),
        )
    return _deliver_report(conn, post, checkpoint, text, link or page, env, now)


def tick(conn, env: dict, now: datetime | None = None, client: GraphClient | None = None) -> str:
    """Report every checkpoint that has come due, a few per run. Needs the Instagram token and user ID."""
    now = now or datetime.now(timezone.utc)
    pending = due(conn, now)
    if not pending:
        return "reports 0"
    if client is None and any(post["kind"] != "story" for post, _ in pending):
        if not (env.get("INSTAGRAM_ACCESS_TOKEN") and env.get("INSTAGRAM_USER_ID")):
            pending = [(post, checkpoint) for post, checkpoint in pending if post["kind"] == "story"]
            if not pending:
                return "reports off"
        else:
            from .cli import graph_client

            client = graph_client(env)
    sent = failed = 0
    for post, checkpoint in pending[:PER_TICK]:
        if report_one(conn, client, post, checkpoint, env, now):
            sent += 1
        else:
            failed += 1
    return f"reports {sent}" + (f", report errors {failed}" if failed else "")
