from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SeriesDay:
    date: date
    in_series: bool
    posting_day: bool
    number: int | None


@dataclass(frozen=True)
class SeriesCalendar:
    weekdays: frozenset[int]
    launched_on: date | None = None
    first_day: date | None = None

    def day(self, now: datetime) -> SeriesDay:
        local = now.astimezone(LONDON).date()
        posting_day = local.weekday() in self.weekdays
        first = self.first_day
        if self.launched_on is None or first is None or not self.launched_on <= local < first + WINDOW:
            return SeriesDay(local, False, posting_day, None)
        return SeriesDay(local, True, posting_day, (local - first).days + 1 if local >= first else None)


def calendar(conn, env: dict) -> SeriesCalendar:
    names = {name.strip().lower() for name in env.get("INSTAGRAM_RECIPE_SERIES_DAYS", "").split(",")}
    days = frozenset(DAYS.index(name) for name in names) if len(names) == 3 and names.issubset(DAYS) else DEFAULT_DAYS
    launch = conn.execute("SELECT value,updated_at FROM publisher_state WHERE key=?", (KEY,)).fetchone()
    if launch is None:
        return SeriesCalendar(days)
    launched_on = datetime.fromisoformat(launch["updated_at"]).astimezone(LONDON).date()
    first = date.fromisoformat(launch["value"])
    if launched_on < first and _published_on(conn, launched_on):
        first = launched_on
    return SeriesCalendar(days, launched_on, first)


def already_published(conn, now: datetime) -> bool:
    return _published_on(conn, now.astimezone(LONDON).date())


def _published_on(conn, day: date) -> bool:
    first = datetime.combine(day, time.min, LONDON)
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
