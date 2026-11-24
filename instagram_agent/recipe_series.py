from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .analysis import LONDON

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_DAYS = frozenset((0, 2, 4))
WINDOW = timedelta(weeks=4)
KEY = "recipe_series_start"
RECIPE_NOTE = re.compile(r"\b(?:recipe|home[ -]?made|meal prep|i cooked|we cooked)\b", re.IGNORECASE)
GUIDANCE = (
    "For the quick home recipe series, open with the finished dish and name it in the hook. "
    "Use only cooking times supported by the owner note; never promise a total shorter than a step. "
    "End with one specific reason to follow for more recipes, and no other ask. "
    "No people or body parts in any frame, including hands, silhouettes and reflections."
)


def start(conn, now: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO publisher_state(key,value,updated_at) VALUES(?,?,?)",
            (KEY, now.astimezone(LONDON).date().isoformat(), now.isoformat(timespec="seconds")),
        )


def _start_date(conn) -> date | None:
    row = conn.execute("SELECT value FROM publisher_state WHERE key=?", (KEY,)).fetchone()
    return date.fromisoformat(row[0]) if row else None


def started(conn) -> bool:
    return _start_date(conn) is not None


def active(conn, now: datetime) -> bool:
    first = _start_date(conn)
    return first is not None and first <= now.astimezone(LONDON).date() < first + WINDOW


def posting_day(env: dict, now: datetime) -> bool:
    names = {name.strip().lower() for name in env.get("INSTAGRAM_RECIPE_SERIES_DAYS", "").split(",")}
    days = {DAYS.index(name) for name in names} if len(names) == 3 and names.issubset(DAYS) else DEFAULT_DAYS
    return now.astimezone(LONDON).weekday() in days


def already_published(conn, now: datetime) -> bool:
    first = datetime.combine(now.astimezone(LONDON).date(), time.min, LONDON)
    return (
        conn.execute(
            "SELECT 1 FROM publish_runs WHERE outcome='published' AND julianday(started_at) >= julianday(?) "
            "AND julianday(started_at) < julianday(?) LIMIT 1",
            (first.isoformat(), (first + timedelta(days=1)).isoformat()),
        ).fetchone()
        is not None
    )


def is_recipe(note: str) -> bool:
    return bool(RECIPE_NOTE.search(note))
