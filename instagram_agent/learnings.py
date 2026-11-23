"""What has worked on @instafoodielove so far, from the stored 24h/72h post reports.

Each measured post gets a lift: its reach and engagement (and watch time for reels) against the median
of posts of the same format at the same checkpoint. Features (hook length, category, hashtags,
posting time and so on) whose posts beat or trail that median are findings. Every finding carries its
sample size and small samples are labelled. This is data for the Create and Review agents and the
Command Centre; it is never evidence for a venue, dish, rating or price.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .analysis import LONDON, hashtags
from .graph import CONVERSION_METRICS
from .post_report import parse_time

WINDOW_DAYS = 90
SMALL_SAMPLE = 10  # posts measured in total
SMALL_GROUP = 5  # posts behind one finding
MIN_GROUP = 2
ABOVE, BELOW = 1.15, 0.85
FINDINGS_SHOWN = 8
TAGS_SHOWN = 4
GROWTH_METRICS = (*CONVERSION_METRICS, "shares", "saved")
MIN_CONVERSION_GROUP = 3


def _engagement(row: dict) -> float | None:
    if not row.get("reach"):
        return None
    weighted = (row.get("likes") or 0) + 2 * (row.get("comments") or 0)
    return (weighted + 3 * ((row.get("saved") or 0) + (row.get("shares") or 0))) / row["reach"]


def _ratio(value, median) -> float | None:
    return value / median if isinstance(value, (int, float)) and median else None


def _lift(row: dict, peers: list[dict]) -> float | None:
    """Mean of reach, engagement and (reels) watch-time ratios against the peers' medians."""
    ratios = []
    for measure in (lambda r: r.get("reach"), _engagement, lambda r: r.get("avg_watch_ms")):
        values = [v for v in (measure(p) for p in peers) if isinstance(v, (int, float))]
        ratio = _ratio(measure(row), statistics.median(values) if values else None)
        if ratio is not None:
            ratios.append(ratio)
    return round(statistics.mean(ratios), 2) if ratios else None


def _reports(conn: sqlite3.Connection, now: datetime | None) -> list[dict]:
    since = (now or datetime.now(timezone.utc)) - timedelta(days=WINDOW_DAYS)
    try:
        rows = conn.execute(
            "SELECT r.*, d.category, d.topic, d.slides, d.caption, d.hashtags, d.reel, d.kind FROM post_reports r "
            "LEFT JOIN drafts d ON d.draft_id=r.draft_id WHERE r.captured_at IS NOT NULL ORDER BY r.posted_at"
        ).fetchall()
        owner = {
            row[0]
            for table in ("uploads", "clips")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            for row in conn.execute(f"SELECT draft_id FROM {table} WHERE draft_id IS NOT NULL")
        }
    except sqlite3.Error:  # an older database, read-only, without the reports table yet
        return []
    reports = []
    for row in rows:
        posted = parse_time(row["posted_at"])
        if posted is not None and posted >= since:
            reports.append({**dict(row), "posted": posted, "owner": row["draft_id"] in owner})
    return reports


def measured(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Each reported post in the window at its latest checkpoint, with what the draft looked like."""
    return _latest(_reports(conn, now))


def _latest(reports: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for report in reports:  # every report is compared with all reports at its checkpoint, older posts' too
        same_time = [r for r in reports if r["checkpoint"] == report["checkpoint"]]
        report["overall_lift"] = _lift(report, same_time)
        report["lift"] = _lift(report, [r for r in same_time if r["format"] == report["format"]])
        if report["media_id"] not in latest or report["checkpoint"] > latest[report["media_id"]]["checkpoint"]:
            latest[report["media_id"]] = report
    return list(latest.values())


def _words(text) -> int:
    return len(str(text or "").split())


def _band(value: int, bands: tuple) -> str:
    for limit, label in bands:
        if value <= limit:
            return label
    return bands[-1][1]


def _hook(post: dict) -> str | None:
    """The first words readers see: slide 1 or the reel's hook, or for owner photos (whose slide text is
    alt text) the caption's first line."""
    if post.get("owner") and post.get("kind") != "reel":
        return str(post.get("caption") or "").strip().split("\n")[0].strip() or None
    try:
        slides = json.loads(post.get("slides") or "[]")
        return str(slides[0].get("text") or "") or None
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def features(post: dict) -> list[tuple[str, str]]:
    """(feature, value) pairs a post can be grouped by."""
    found = [("format", post["format"] or "post")]
    if post.get("category"):
        found.append(("category", post["category"]))
    if post.get("topic") is not None:
        found.append(("made from", "your own photos or clip" if post["owner"] else "AI-generated"))
    hour = post["posted"].astimezone(LONDON).hour
    found.append(
        ("posting time (London)", "before 12:00" if hour < 12 else "12:00-16:59" if hour < 17 else "17:00 or later")
    )
    hook = _hook(post)
    if hook:
        found.append(
            ("hook length", _band(_words(hook), ((4, "4 words or fewer"), (6, "5-6 words"), (99, "7+ words"))))
        )
    if post.get("caption"):
        bands = ((79, "under 80 words"), (150, "80-150 words"), (10**6, "over 150 words"))
        found.append(("caption length", _band(_words(post["caption"]), bands)))
    tags = (post.get("hashtags") or "").split()
    if post.get("topic") is not None:
        found.append(("hashtag count", _band(len(tags), ((5, "0-5"), (12, "6-12"), (99, "13+")))))
    for tag in hashtags(" ".join(tags)):
        found.append(("hashtag", tag))
    try:
        seconds = (json.loads(post.get("reel") or "{}") or {}).get("seconds")
    except (TypeError, ValueError):
        seconds = None
    if isinstance(seconds, (int, float)) and post["format"] == "reel":
        found.append(("reel length", _band(seconds, ((14.9, "under 15 s"), (30, "15-30 s"), (10**6, "over 30 s")))))
    return found


def _growth_features(post: dict) -> list[tuple[str, str]]:
    features_found = features(post)
    caption = post.get("caption") or ""
    mentions = dict.fromkeys(re.findall(r"(?<![\w.])@([a-z0-9_][a-z0-9_.]*)", caption.lower()))
    features_found.extend(("caption mention", f"@{name.rstrip('.')}") for name in mentions)
    sentences = re.split(r"[.!?]+\s+|\n+", caption.strip())
    ask = re.match(r"\W*(?:please\s+)?(follow|save|share|send|comment)\b", sentences[-1], re.IGNORECASE)
    if ask:
        action = ask[1].lower()
        features_found.append(("caption ask", "share" if action == "send" else action))
    features_found.append(("London posting hour", str(post["posted"].astimezone(LONDON).hour)))
    return features_found


def growth_findings(reports: list[dict]) -> dict[str, list[dict]]:
    """Rank features by their measured events per reach, without mixing signals or post ages."""
    found = {}
    for metric in GROWTH_METRICS:
        cohorts, formats = defaultdict(list), defaultdict(list)
        rates = {}
        for post in reports:
            value = post.get(metric)
            if value is None or not post.get("reach"):
                continue
            rate = value / post["reach"]
            rates[post["media_id"], post["checkpoint"]] = rate
            cohorts[post["checkpoint"]].append(rate)
            formats[post["checkpoint"], post["format"]].append(rate)
        groups = defaultdict(dict)
        for post in sorted(reports, key=lambda row: row["checkpoint"]):
            rate = rates.get((post["media_id"], post["checkpoint"]))
            if rate is None:
                continue
            for feature, value in set(_growth_features(post)):
                peers = (
                    cohorts[post["checkpoint"]] if feature == "format" else formats[post["checkpoint"], post["format"]]
                )
                if len(peers) >= MIN_CONVERSION_GROUP and (mean := statistics.mean(peers)):
                    tag_format = (post["format"] or "post") if feature == "hashtag" else None
                    groups[feature, value, tag_format][post["media_id"]] = rate / mean
        ranked = []
        for (feature, value, tag_format), lifts in groups.items():
            if len(lifts) < MIN_CONVERSION_GROUP:
                continue
            lift = round(statistics.mean(lifts.values()), 2)
            if lift >= ABOVE:
                ranked.append(
                    {
                        "feature": feature,
                        "value": value,
                        "posts": len(lifts),
                        "vs_peer_rate": lift,
                        "small_sample": len(lifts) < SMALL_GROUP,
                        **({"format": tag_format} if tag_format else {}),
                    }
                )
        if ranked:
            found[metric] = sorted(ranked, key=lambda f: (-f["vs_peer_rate"], -f["posts"], f["feature"], f["value"]))
    return found


def growth_hours(conn: sqlite3.Connection, now: datetime) -> list[int]:
    findings = growth_findings(_reports(conn, now))
    return list(
        dict.fromkeys(
            int(row["value"])
            for metric in CONVERSION_METRICS
            for row in findings.get(metric, [])
            if row["feature"] == "London posting hour" and not row["small_sample"]
        )
    )


def _finding(feature: str, value: str, lifts: list[float]) -> dict:
    lift = round(statistics.median(lifts), 2)
    small = len(lifts) < SMALL_GROUP
    label = f"#{value.lstrip('#')}" if feature == "hashtag" else f"{feature}: {value}"
    sample = f"{len(lifts)} posts{', small sample' if small else ''}"
    text = f"{label[0].upper()}{label[1:]} \u2014 {lift:.1f}x the median ({sample})"
    return {
        "feature": feature,
        "value": value,
        "posts": len(lifts),
        "vs_median": lift,
        "small_sample": small,
        "text": text,
    }


def _post(post: dict) -> dict:
    return {
        "topic": post.get("topic"),
        "hook": _hook(post),
        "format": post["format"],
        "checkpoint": post["checkpoint"],
        "posted_at": post["posted_at"],
        "vs_median": post["lift"],
        "permalink": post.get("permalink"),
    }


def summary(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Findings, best and weakest posts and the latest reports. Works on a read-only connection."""
    checkpoints = _reports(conn, now)
    posts = _latest(checkpoints)
    groups: dict[tuple[str, str], list[float]] = {}
    for post in posts:
        for feature, value in features(post):
            lift = post["overall_lift"] if feature == "format" else post["lift"]
            if lift is not None:
                groups.setdefault((feature, value), []).append(lift)
    distinct: dict[str, set] = {}
    for feature, value in groups:
        distinct.setdefault(feature, set()).add(value)
    findings = [
        _finding(feature, value, lifts)
        for (feature, value), lifts in groups.items()
        if len(lifts) >= MIN_GROUP and (feature == "hashtag" or len(distinct[feature]) > 1)
    ]

    def pick(rows, reverse):
        rows = sorted(rows, key=lambda f: (f["vs_median"], f["posts"]), reverse=reverse)
        tags = [f for f in rows if f["feature"] == "hashtag"][:TAGS_SHOWN]
        return ([f for f in rows if f["feature"] != "hashtag"] + tags)[:FINDINGS_SHOWN]

    ranked = sorted((p for p in posts if p["lift"] is not None), key=lambda p: p["lift"], reverse=True)
    count = len(posts)
    if not count:
        note = "No post has been measured yet: the first report comes 24 hours after a post goes live."
    elif count < SMALL_SAMPLE:
        note = f"Based on {count} post{'' if count == 1 else 's'}: a small sample, so treat these as hints."
    else:
        note = f"Based on {count} posts in the last {WINDOW_DAYS} days."
    try:
        reports = [
            dict(row)
            for row in conn.execute(
                "SELECT r.media_id, r.checkpoint, r.captured_at, r.posted_at, r.permalink, r.summary, r.format, "
                "d.topic FROM post_reports r LEFT JOIN drafts d ON d.draft_id=r.draft_id "
                "WHERE r.captured_at IS NOT NULL ORDER BY r.captured_at DESC LIMIT 10"
            )
        ]
    except sqlite3.Error:
        reports = []
    return {
        "posts_measured": count,
        "small_sample": count < SMALL_SAMPLE,
        "sample_note": note,
        "window_days": WINDOW_DAYS,
        "working": pick([f for f in findings if f["vs_median"] >= ABOVE], True),
        "not_working": pick([f for f in findings if f["vs_median"] <= BELOW], False),
        "best_posts": [_post(p) for p in ranked[:3]] if len(ranked) >= MIN_GROUP else [],
        "weakest_posts": [_post(p) for p in ranked[::-1][:3]] if len(ranked) >= 2 * MIN_GROUP else [],
        "growth_findings": growth_findings(checkpoints),
        "reports": reports,
    }


def for_prompt(found: dict) -> dict:
    """The compact, data-only form the Create and Review agents see."""
    keep = ("feature", "value", "posts", "vs_median", "small_sample")
    post_keys = ("topic", "hook", "format", "vs_median")
    growth = {}
    for metric, rows in found["growth_findings"].items():
        tags_by_format = defaultdict(list)
        for row in rows:
            if row["feature"] == "hashtag":
                tags_by_format[row["format"]].append(row)
        growth[metric] = [row for row in rows if row["feature"] != "hashtag"][:FINDINGS_SHOWN] + [
            row for tags in tags_by_format.values() for row in tags[:TAGS_SHOWN]
        ]
    return {
        "posts_measured": found["posts_measured"],
        "small_sample": found["small_sample"],
        "beat_median": [{k: f[k] for k in keep} for f in found["working"]],
        "below_median": [{k: f[k] for k in keep} for f in found["not_working"]],
        "best_posts": [{k: p[k] for k in post_keys} for p in found["best_posts"]],
        "weakest_posts": [{k: p[k] for k in post_keys} for p in found["weakest_posts"]],
        "growth_findings": growth,
    }
