from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from notifications import command_centre_link, notify

from . import learnings
from .analysis import LONDON
from .pillar_config import PILLARS, POSTING_DAYS, RESERVE

MANUAL = ("Idea", "Scripted", "Filmed", "Edited")


def monday(now):
    day = now.astimezone(LONDON).date()
    return day - timedelta(days=day.weekday())


def ensure_week(conn, now: datetime) -> None:
    first = monday(now)
    week = first.isoformat()
    if conn.execute("SELECT 1 FROM content_plan WHERE week=? LIMIT 1", (week,)).fetchone():
        return
    replacement = dict(
        conn.execute("SELECT old_pillar,new_pillar FROM content_pillar_replacements WHERE effective_on<=?", (week,))
    )
    rotation = [replacement.get(p, p) for p in PILLARS if p not in RESERVE]
    start = conn.execute("SELECT MIN(week) FROM content_plan").fetchone()[0]
    index = (first - date.fromisoformat(start)).days // 7 if start else 0
    # Run one pillar in the middle slot for three weeks so it can be measured in three successive reports.
    anchor = rotation[(index // 3) % len(rotation)]
    others = [p for p in rotation if p != anchor]
    recent = conn.execute("SELECT pillar FROM content_plan WHERE week<? ORDER BY slot DESC LIMIT 1", (week,)).fetchone()
    offset = index * 2 % len(others)
    if recent and others[offset] == recent[0]:
        offset = (offset + 1) % len(others)
    selected = (others[offset], anchor, others[(offset + 1) % len(others)])
    hour = next((h for h in learnings.growth_hours(conn, now) if h >= 12), 16)
    with conn:
        for pillar, day in zip(selected, POSTING_DAYS, strict=True):
            slot = datetime.combine(first + timedelta(days=day), time(hour), LONDON)
            conn.execute(
                "INSERT OR IGNORE INTO content_plan(week,pillar,slot) VALUES(?,?,?)",
                (week, pillar, slot.isoformat(timespec="minutes")),
            )


def slot_today(conn, now):
    local = now.astimezone(LONDON)
    return conn.execute(
        "SELECT * FROM content_plan WHERE substr(slot,1,10)=? AND julianday(slot)>=julianday(?) "
        "AND NOT EXISTS(SELECT 1 FROM drafts WHERE plan_id=content_plan.id AND status!='rejected')",
        (local.date().isoformat(), now.isoformat()),
    ).fetchone()


def started(conn):
    return conn.execute("SELECT 1 FROM content_plan LIMIT 1").fetchone() is not None


def posting_day(now):
    return now.astimezone(LONDON).weekday() in POSTING_DAYS


@dataclass(frozen=True)
class PlanDay:
    date: date
    in_series: bool
    posting_day: bool


@dataclass(frozen=True)
class PostingCalendar:
    launched_on: date

    def day(self, now: datetime) -> PlanDay:
        day = now.astimezone(LONDON).date()
        return PlanDay(day, day >= self.launched_on, posting_day(now))


def calendar(conn):
    first = conn.execute("SELECT MIN(week) FROM content_plan").fetchone()[0]
    return PostingCalendar(date.fromisoformat(first)) if first else None


def _reel_metadata(reel: str | None) -> dict:
    try:
        metadata = json.loads(reel or "{}")
        return metadata if isinstance(metadata, dict) else {}
    except ValueError:
        return {}


def alternate(reel: str | None) -> bool:
    return _reel_metadata(reel).get("pillar_match") is False


def rows(conn, now, auto_publish=False):
    from .publish import held, rendered, will_post

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_plan'").fetchone():
        return []
    week = monday(now)
    results = conn.execute(
        "SELECT p.*, d.draft_id, d.topic, d.status AS draft_status, d.scheduled_for, d.reel, d.kind, d.review_score "
        "FROM content_plan p LEFT JOIN drafts d ON d.draft_id=("
        "SELECT draft_id FROM drafts WHERE plan_id=p.id AND status!='rejected' ORDER BY created_at DESC LIMIT 1) "
        "WHERE p.week>=? ORDER BY p.slot",
        ((week - timedelta(weeks=4)).isoformat(),),
    )
    entries = []
    for row in results:
        entry = dict(row)
        status = entry.pop("status")
        draft = {"reel": entry.pop("reel"), "kind": entry.pop("kind")}
        metadata = _reel_metadata(draft["reel"])
        score = entry.pop("review_score")
        draft_status = entry.pop("draft_status")

        if draft_status == "published":
            status = "Posted"
        elif draft_status == "approved":
            status = "Scheduled" if auto_publish and entry["scheduled_for"] and rendered(draft) else "Approved"
        entry.update(
            status=status,
            alternate=metadata.get("pillar_match") is False,
            needs_approval=draft_status == "pending"
            and (status == "Edited" or metadata.get("render") == "done")
            and rendered(draft)
            and not (auto_publish and will_post(draft_status, score, held(draft))),
            pillar_name=PILLARS[entry["pillar"]],
            next_status=MANUAL[MANUAL.index(status) + 1] if status in MANUAL[:-1] else None,
        )
        entries.append(entry)
    return entries


def advance(conn, entry_id: int, status: str) -> str | None:
    if status not in MANUAL[:-1]:
        return None
    target = MANUAL[MANUAL.index(status) + 1]
    with conn:
        changed = conn.execute(
            "UPDATE content_plan SET status=? WHERE id=? AND status=? "
            "AND NOT EXISTS(SELECT 1 FROM drafts WHERE plan_id=content_plan.id AND status IN ('approved','published'))",
            (target, entry_id, status),
        ).rowcount
    return target if changed else None


def notify_progress(conn, env, now, auto_publish):
    page = command_centre_link(env, "instagram")
    for row in rows(conn, now, auto_publish):
        if row["status"] != "Idea":
            notify(
                "instagram",
                "ok",
                f"Script ready: {row['pillar_name']}",
                page,
                key=f"instagram-plan-script:{row['id']}",
                env=env,
                now=now,
            )
        if row["needs_approval"]:
            notify(
                "instagram",
                "ok",
                f"Reel needs your approval: {row['pillar_name']}",
                page,
                key=f"instagram-plan-approval:{row['draft_id']}",
                env=env,
                now=now,
            )
