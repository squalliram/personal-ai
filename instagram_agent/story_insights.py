"""Keep eligible Stories' last available insights before Meta expires them."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from time import monotonic

from notifications import safe_link

from .graph import CONVERSION_METRICS, GraphAPIError

METRICS = ("reach", "views", "replies", "navigation", *CONVERSION_METRICS, "shares", "total_interactions")
NAVIGATION = {"metric": "navigation", "breakdown": "story_navigation_action_type"}
LIFETIME = timedelta(hours=24)
FINAL_WINDOW = timedelta(hours=22)
CAPTURE_BUDGET = 30


def parse_metric(payload: dict, metric: str) -> tuple[int | None, dict[str, int]]:
    value, breakdown = None, {}
    for entry in payload.get("data") or []:
        if entry.get("name", metric) != metric:
            continue
        total = entry.get("total_value") or {}
        points = entry.get("values") or []
        number = (points[0] if points else {}).get("value", total.get("value"))
        if type(number) is int:
            value = number
        if metric != "navigation":
            continue
        if isinstance(number, dict):
            breakdown.update({key: count for key, count in number.items() if type(count) is int})
        for group in total.get("breakdowns") or []:
            if group.get("dimension_keys") != [NAVIGATION["breakdown"]]:
                continue
            for result in group.get("results") or []:
                keys, count = result.get("dimension_values") or [], result.get("value")
                if len(keys) == 1 and isinstance(keys[0], str) and type(count) is int:
                    breakdown[keys[0]] = count
    return value, breakdown


def read_metrics(get) -> tuple[dict[str, int], dict[str, int]]:
    """Read metrics independently; one unsupported field must not hide the others."""
    values, breakdown = {}, {}
    for metric in METRICS:
        value, actions = None, {}
        if metric == "navigation":
            value, actions = parse_metric(get(NAVIGATION) or {}, metric)
        if value is None and not actions:
            value, actions = parse_metric(get({"metric": metric}) or {}, metric)
        if actions:
            breakdown = actions
            if value is None:
                value = sum(actions.values())
        if value is not None:
            values[metric] = value
    return values, breakdown


def _drafts_by_story(conn) -> dict[str, str]:
    found = {}
    for row in conn.execute("SELECT draft_id,reel FROM drafts WHERE status='published' AND reel IS NOT NULL"):
        try:
            story = json.loads(row["reel"]).get("story")
        except (AttributeError, ValueError):
            continue
        if isinstance(story, dict) and story.get("state") == "published":
            media_id = story.get("media_id")
            if isinstance(media_id, str):
                found[media_id] = row["draft_id"]
    return found


def _save(conn, media_id, draft_id, posted, permalink, stamp, values, breakdown):
    with conn:
        previous = conn.execute("SELECT * FROM story_insights WHERE media_id=?", (media_id,)).fetchone()
        if previous and previous["captured_at"] and (stamp is None or stamp < previous["captured_at"]):
            return
        if previous:
            values = {**json.loads(previous["metrics"]), **values}
            breakdown = breakdown or json.loads(previous["navigation_breakdown"])
        conn.execute(
            "INSERT INTO story_insights(media_id,draft_id,posted_at,permalink,captured_at,metrics,navigation_breakdown) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET "
            "draft_id=COALESCE(excluded.draft_id,story_insights.draft_id), "
            "permalink=COALESCE(excluded.permalink,story_insights.permalink), captured_at=excluded.captured_at, "
            "metrics=excluded.metrics, navigation_breakdown=excluded.navigation_breakdown",
            (media_id, draft_id, posted.isoformat(), permalink, stamp, json.dumps(values), json.dumps(breakdown)),
        )


def capture(conn, client, now: datetime | None = None) -> int:
    from .post_report import parse_time

    drafts = _drafts_by_story(conn)
    latest = {row[0]: parse_time(row[1]) for row in conn.execute("SELECT media_id,captured_at FROM story_insights")}
    captured = 0
    deadline = monotonic() + CAPTURE_BUDGET
    stories = client.paginated(
        f"{client.user_id}/stories", {"fields": "id,timestamp,permalink", "limit": "100"}, limit=None
    )
    while monotonic() < deadline and (media := next(stories, None)) is not None:
        media_id, posted = media.get("id"), parse_time(media.get("timestamp"))
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not isinstance(media_id, str) or not media_id.isdigit() or not posted:
            continue
        age = current - posted
        if not timedelta(0) <= age < LIFETIME:
            continue
        previous = latest.get(media_id)
        interval = timedelta(hours=1 if age >= FINAL_WINDOW else 6)
        if previous and current - previous < interval and not previous - posted < FINAL_WINDOW <= age:
            continue

        def get(params, media_id=media_id, posted=posted):
            if monotonic() >= deadline:
                return None
            if now is None and datetime.now(timezone.utc) - posted >= LIFETIME:
                return None
            try:
                return client.get(f"{media_id}/insights", params)
            except GraphAPIError:
                return None

        values, breakdown = read_metrics(get)
        stamp = current.isoformat(timespec="seconds") if values else None
        _save(conn, media_id, drafts.get(media_id), posted, safe_link(media.get("permalink")), stamp, values, breakdown)
        latest[media_id] = current
        captured += bool(values)
    return captured
