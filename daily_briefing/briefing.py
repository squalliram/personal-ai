from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LONDON)


def calendar_overlaps(events: list[dict]) -> list[tuple[dict, dict]]:
    timed = sorted(
        ((event, _parse(event.get("start")), _parse(event.get("end"))) for event in events),
        key=lambda entry: entry[1] or datetime.max.replace(tzinfo=LONDON),
    )
    overlaps = []
    for index, (left, start, end) in enumerate(timed):
        if not start or not end:
            continue
        for right, right_start, _ in timed[index + 1:]:
            if not right_start or right_start >= end:
                break
            overlaps.append((left, right))
    return overlaps


def rank_tasks(tasks: list[dict], today: date) -> list[dict]:
    """Rank only supplied fields; unknown priority/deadline has no invented value."""
    priority = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    ranked = []
    for task in tasks:
        due = task.get("due")
        urgency = 0
        if due:
            try:
                days = (date.fromisoformat(due[:10]) - today).days
                urgency = 4 if days < 0 else 3 if days == 0 else 2 if days <= 2 else 1
            except ValueError:
                pass
        copy = dict(task)
        copy["_score"] = urgency * 10 + priority.get(str(task.get("priority", "")).lower(), 0)
        ranked.append(copy)
    return sorted(ranked, key=lambda task: (-task["_score"], task.get("title", "")))


def render(report_date: date, generated_at: datetime, events: list[dict], tasks: list[dict], errors: list[str]) -> str:
    overlaps = calendar_overlaps(events)
    ranked = rank_tasks(tasks, report_date)
    lines = [f"# Daily Intelligence Briefing — {report_date.isoformat()}", "", f"Generated: {generated_at.astimezone(LONDON).isoformat()}", "", "## Calendar commitments"]
    if events:
        seen = set()
        for event in events:
            key = event.get("id") or (event.get("title"), event.get("start"), event.get("end"))
            if key in seen:
                continue
            seen.add(key)
            when = _parse(event.get("start"))
            time = when.strftime("%a %H:%M") if when else "All day / time unavailable"
            location = f" — {event['location']}" if event.get("location") else ""
            link = f" ([source]({event['url']}))" if event.get("url") else ""
            lines.append(f"- {time}: {event.get('title', 'Untitled event')}{location}{link}")
    else:
        lines.append("- No calendar events retrieved.")
    lines += ["", "## Overlapping commitments"]
    lines += [f"- {a.get('title', 'Untitled')} overlaps {b.get('title', 'Untitled')}" for a, b in overlaps] or ["- None detected."]
    lines += ["", "## Outstanding work (source data)"]
    if ranked:
        for task in ranked:
            deadline = f" — due {task['due']}" if task.get("due") else ""
            link = f" ([source]({task['url']}))" if task.get("url") else ""
            lines.append(f"- {task.get('title', 'Untitled task')}{deadline}{link}")
    else:
        lines.append("- No tasks retrieved.")
    lines += ["", "## AI-recommended priorities", "These are suggestions, not commitments."]
    lines += [f"{i}. {task.get('title', 'Untitled task')}" for i, task in enumerate(ranked[:3], 1)] or ["- No recommendation possible: no tasks retrieved."]
    if errors:
        lines += ["", "## Connector failures"] + [f"- {error}" for error in errors]
    return "\n".join(lines) + "\n"
