from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from statistics import median

from notifications import command_centre_link, notify

from .analysis import LONDON
from .content_plan import alternate, monday
from .pillar_config import PILLARS, RESERVE
from .post_report import hook_style


def _ranking(reels, field):
    groups = defaultdict(list)
    for reel in reels:
        if reel[field] is not None and reel["views"] is not None:
            groups[reel[field]].append(reel["views"])
    return sorted(((median(views), name) for name, views in groups.items()), reverse=True)


def _label(row):
    name = row["topic"] or row["pillar"] or row["media_id"]
    return name if len(name) <= 32 else name[:31].rstrip() + "…"


def _figures(row):
    if row is None:
        return "Unavailable"
    v, watch, shares, saves, comments = (row[k] for k in ("views", "avg_watch_ms", "shares", "saved", "comments"))
    watch = f"{watch / 1000:g}s" if watch is not None else None
    return (
        f"{_label(row)} ("
        + ", ".join(
            f"{value if value is not None else 'n/a'} {label}"
            for value, label in zip(
                (v, watch, shares, saves, comments), ("views", "watch", "shares", "saves", "comments"), strict=True
            )
        )
        + ")"
    )


def _extreme(rows, target):
    if target is None:
        return None, _figures(None)
    views = target["views"]
    tied = sum(reel["views"] == views for reel in rows)
    if tied > 1:
        return None, f"{tied} Reels tied at {views} views"
    return target, _figures(target)


def _replacement(conn, week, worst):
    if not worst:
        return None
    earlier = tuple((week - timedelta(weeks=i)).isoformat() for i in (1, 2))
    if (
        conn.execute(
            "SELECT COUNT(*) FROM content_weekly_reports WHERE week IN (?,?) AND worst=?", (*earlier, worst)
        ).fetchone()[0]
        != 2
    ):
        return None
    changes = dict(conn.execute("SELECT old_pillar,new_pillar FROM content_pillar_replacements"))
    if worst in changes:
        return None
    reserve = next((p for p in RESERVE if p not in changes.values()), None)
    if reserve is None:
        return "No unused reserve pillar remains."
    conn.execute(
        "INSERT INTO content_pillar_replacements(old_pillar,new_pillar,effective_on) VALUES(?,?,?)",
        (worst, reserve, (week + timedelta(weeks=1)).isoformat()),
    )
    return f"Replace {PILLARS[worst]} with {PILLARS[reserve]} next week."


def _make(conn, week):
    start = datetime.combine(week, time.min, LONDON)
    end = start + timedelta(weeks=1)
    reels = []
    for row in conn.execute(
        "SELECT r.*, d.topic, d.reel AS draft_reel, p.pillar FROM post_reports r LEFT JOIN drafts d ON d.draft_id=r.draft_id "
        "LEFT JOIN content_plan p ON p.id=d.plan_id WHERE r.checkpoint='24h' AND r.format='reel' "
        "AND r.captured_at IS NOT NULL AND julianday(r.posted_at)>=julianday(?) AND julianday(r.posted_at)<julianday(?)",
        (start.isoformat(), end.isoformat()),
    ):
        reel = dict(row)
        if alternate(reel.pop("draft_reel")):
            reel["pillar"] = None
        creative = json.loads(row["creative"] or "{}")
        reel["hook_style"] = creative.get("hook_style") or hook_style(creative.get("hook"))
        reels.append(reel)
    measured = sorted((r for r in reels if r["views"] is not None), key=lambda r: (-r["views"], r["media_id"]))
    _, top_summary = _extreme(measured, measured[0] if measured else None)
    bottom, bottom_summary = _extreme(measured, measured[-1] if len(measured) > 1 else None)
    hooks, pillars = _ranking(reels, "hook_style"), _ranking(reels, "pillar")
    best_hook = hooks[0][1] if hooks and (len(hooks) == 1 or hooks[0][0] > hooks[1][0]) else None
    best_pillar = pillars[0][1] if pillars and (len(pillars) == 1 or pillars[0][0] > pillars[1][0]) else None
    worst = pillars[-1][1] if len(pillars) > 1 and pillars[-1][0] < pillars[-2][0] else None
    baseline = conn.execute(
        "SELECT followers FROM account_followers WHERE captured_on=?", ((week - timedelta(days=1)).isoformat(),)
    ).fetchone()
    current = conn.execute(
        "SELECT followers FROM account_followers WHERE captured_on=?", ((end.date() - timedelta(days=1)).isoformat(),)
    ).fetchone()
    change = f"{current[0] - baseline[0]:+d}" if baseline and current else "unavailable"
    if len(measured) > 1 and measured[0]["views"] == measured[-1]["views"]:
        results = f"{top_summary}; no unique top/bottom."
    else:
        results = f"Top: {top_summary}. Bottom: {bottom_summary}."
    if best_hook and best_hook != "none":
        hook_advice = f"Repeat the {best_hook} hook in a second test."
    else:
        hook_advice = "Try a choice hook against a direct opening."
    pillar_advice = f"Retest {PILLARS[best_pillar]}." if best_pillar else "Collect 24h data across more pillars."
    edit_advice = (
        "Test a tighter edit on the lowest-viewed concept."
        if bottom and bottom["avg_watch_ms"] is not None
        else "Log watch time and hold post time steady."
    )
    parts = [
        f"Sunday Reel report · week of {week}. {results} Ranked by 24h views (n={len(measured)}).",
        f"Best median 24h views: hook {best_hook or 'unavailable'}; pillar {PILLARS.get(best_pillar, 'unavailable')}. "
        f"Account follower change: {change} (not attributed to Reels).",
        f"Next week: 1) {hook_advice} 2) {pillar_advice} 3) {edit_advice}",
    ]
    if update := _replacement(conn, week, worst):
        parts[2] += " " + update
    return parts, worst


def tick(conn, env, now: datetime) -> None:
    local = now.astimezone(LONDON)
    if local.weekday() == 6 and local.hour >= 18:
        week = monday(now)
        with conn:
            if not conn.execute("SELECT 1 FROM content_weekly_reports WHERE week=?", (week.isoformat(),)).fetchone():
                parts, worst = _make(conn, week)
                conn.execute(
                    "INSERT INTO content_weekly_reports(week,summary,worst) VALUES(?,?,?)",
                    (week.isoformat(), json.dumps(parts), worst),
                )
    page = command_centre_link(env, "instagram")
    pending = conn.execute(
        "SELECT week,summary FROM content_weekly_reports WHERE notified_at IS NULL ORDER BY week"
    ).fetchall()
    for week, summary in pending:
        stored = [
            notify("instagram", "info", title, page, key=f"instagram-sunday:{week}:{index}", env=env, now=now)
            for index, title in enumerate(json.loads(summary), 1)
        ]
        if all(notification_id is not None for notification_id in stored):
            with conn:
                conn.execute(
                    "UPDATE content_weekly_reports SET notified_at=? WHERE week=?",
                    (now.isoformat(timespec="seconds"), week),
                )
