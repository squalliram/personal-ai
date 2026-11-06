"""Typed contract and validation for connector snapshots.

The connector bridge is external code; this module is the only place that
interprets its JSON. Validation never raises and never logs snapshot
content: malformed items are dropped and reported as counted,
content-free error strings, so a scheduled run still produces a briefing
and an auditable run-log row instead of crashing silently.

Field rules mirror what ``briefing.render`` consumes: strings or null
only, and ``start``/``end``/``due`` must parse with the same ISO 8601
parser the renderer uses. Unknown keys pass through unchanged so the
connector can add fields without a lockstep release.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Snapshot:
    events: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _iso_ok(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _clean_items(raw: object, kind: str, errors: list[str]) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"Snapshot input failure: {kind} is not a list")
        return []
    cleaned = []
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        if any(value is not None and not isinstance(value, str) for value in item.values()):
            dropped += 1
            continue
        if any(item.get(key) and not _iso_ok(item[key]) for key in ("start", "end", "due")):
            dropped += 1
            continue
        cleaned.append(item)
    if dropped:
        errors.append(f"Snapshot input failure: dropped {dropped} invalid {kind}")
    return cleaned


def parse(data: object) -> Snapshot:
    """Validate decoded JSON. Anything unusable becomes a counted error."""
    if not isinstance(data, dict):
        return Snapshot(errors=["Snapshot input failure: root is not an object"])
    errors = []
    events = _clean_items(data.get("events"), "events", errors)
    tasks = _clean_items(data.get("tasks"), "tasks", errors)
    raw_errors = data.get("errors", [])
    if isinstance(raw_errors, list):
        kept = [entry for entry in raw_errors if isinstance(entry, str)]
        if len(kept) != len(raw_errors):
            errors.append("Snapshot input failure: dropped non-text connector errors")
    else:
        kept = []
        errors.append("Snapshot input failure: errors is not a list")
    return Snapshot(events=events, tasks=tasks, errors=[*kept, *errors])


def load(path: str | Path) -> Snapshot:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Snapshot(errors=[f"Snapshot input failure: {type(exc).__name__}"])
    return parse(data)
