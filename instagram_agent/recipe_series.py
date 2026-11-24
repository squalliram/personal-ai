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


def start(conn, now: datetime, *, defer_first_day: bool = False) -> None:
    first = now.astimezone(LONDON).date() + timedelta(days=int(defer_first_day))
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO publisher_state(key,value,updated_at) VALUES(?,?,?)",
            (KEY, first.isoformat(), now.isoformat(timespec="seconds")),
        )


def _launch(conn):
    return conn.execute("SELECT value,updated_at FROM publisher_state WHERE key=?", (KEY,)).fetchone()


def started(conn) -> bool:
    return _launch(conn) is not None


def active(conn, now: datetime, *, including_launch_day: bool = False) -> bool:
    launch = _launch(conn)
    if launch is None:
        return False
    first = date.fromisoformat(launch["value"])
    begins = datetime.fromisoformat(launch["updated_at"]).astimezone(LONDON).date() if including_launch_day else first
    return begins <= now.astimezone(LONDON).date() < first + WINDOW


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
