"""Home page signals: what needs attention (from the automations) and suggestions grounded in energy data.

Pure functions over the shared snapshot. Every insight carries the numbers it rests on and is labelled a
suggestion. Nothing here writes, schedules or calls a model, and no source text is copied into a message.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
FUELS = ("electricity", "gas")
HISTORY_DAYS = 70
CHART_DAYS = 14
WEEK_CHANGE = 0.15
WEEK_DROP = 0.20
MONTH_CHANGE = 0.15
MIN_KWH_CHANGE = 1.5
MONTH_MIN_DAYS = 7
ALWAYS_ON_SHARE = 0.30
INSTAGRAM_READY_HOUR = 12
STALE_READINGS_DAYS = 3
MAX_INSIGHTS = 4
SEVERITY = {"failed": "critical", "degraded": "warning", "stale": "warning", "refresh_required": "warning"}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "action": 2, "info": 3}
INSIGHT_ORDER = {"warning": 0, "info": 1, "good": 2}
FUEL_NAME = {"electricity": "Electricity", "gas": "Gas"}


def _day(value: date) -> str:
    return f"{value:%a} {value.day} {value:%b}"


def _pct(change: float) -> str:
    return f"{abs(change) * 100:.0f}%"


def _local(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(LONDON) if parsed.tzinfo else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# Energy -----------------------------------------------------------------------------------------


def _complete_through(fuel: str, dashboard: dict | None, today: date) -> date:
    """Last London day the meter readings fully cover; never today, which is still in progress."""
    latest = _local(((dashboard or {}).get("latest_reading_at") or {}).get(fuel))
    yesterday = today - timedelta(days=1)
    return min(latest.date() - timedelta(days=1), yesterday) if latest else yesterday


def _sums(series: dict[date, dict], days: list[date]) -> tuple[float | None, float | None]:
    rows = [series.get(day) for day in days]
    if any(row is None or row["kwh"] is None for row in rows):
        return None, None
    kwh = sum(row["kwh"] for row in rows)
    cost = None if any(row["cost"] is None for row in rows) else round(sum(row["cost"] for row in rows), 2)
    return round(kwh, 3), cost


def _span(end: date, days: int) -> list[date]:
    return [end - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def _peak_hour(hourly: list, fuel: str) -> dict | None:
    hours = {
        row["hour"]: row["kwh"]
        for row in hourly
        if isinstance(row, dict) and row.get("fuel") == fuel and _number(row.get("kwh")) is not None
        and isinstance(row.get("hour"), int) and 0 <= row["hour"] < 24
    }  # fmt: skip
    if len(hours) < 24:
        return None
    hour = max(hours, key=hours.get)
    return {"hour": hour, "kwh": round(hours[hour], 3), "hours": hours}


def energy_fuels(energy: dict, history: list[dict], dashboard: dict | None, now: datetime) -> dict:
    """Per-fuel chart series and week totals for the home page, from measured daily rows."""
    if energy["state"] != "available":
        return {}
    today = now.astimezone(LONDON).date()
    hourly = (dashboard or {}).get("hourly") or []
    found = {}
    for fuel in FUELS:
        rows = [row for row in history if row.get("fuel") == fuel and isinstance(row.get("date"), str)]
        if not rows:
            found[fuel] = {"state": "no_data", "reason": f"No {fuel} readings are stored."}
            continue
        if all(_number(row.get("consumption_kwh")) is None for row in rows):
            reason = (
                "Gas usage is hidden until the gas meter's units are confirmed as kWh (GAS_UNITS)."
                if fuel == "gas"
                else "No measured electricity usage is available."
            )
            found[fuel] = {"state": "unavailable", "reason": reason}
            continue
        series = {}
        for row in rows:
            try:
                key = date.fromisoformat(row["date"])
            except ValueError:
                continue
            series[key] = {"kwh": _number(row.get("consumption_kwh")), "cost": _number(row.get("total_cost_gbp"))}
        through = _complete_through(fuel, dashboard, today)
        this_kwh, this_cost = _sums(series, _span(through, 7))
        last_kwh, last_cost = _sums(series, _span(through - timedelta(days=7), 7))
        change = (this_kwh - last_kwh) / last_kwh if this_kwh is not None and last_kwh else None
        peak = _peak_hour(hourly, fuel)
        found[fuel] = {
            "state": "available",
            "basis": {"kwh": "measured", "cost": "estimated"},
            "through": through.isoformat(),
            "days": [
                {"date": day.isoformat(), **series.get(day, {"kwh": None, "cost": None})}
                for day in _span(through, CHART_DAYS)
            ],
            "week": {
                "kwh": this_kwh,
                "cost": this_cost,
                "previous_kwh": last_kwh,
                "previous_cost": last_cost,
                "change": round(change, 4) if change is not None else None,
            },
            "peak_hour": {"hour": peak["hour"], "kwh": peak["kwh"]} if peak else None,
            "_series": series,
            "_peak": peak,
        }
    return found


def _insight(fuel: str, kind: str, severity: str, title: str, detail: str, suggestion: str, **evidence) -> dict:
    return {
        "id": f"{fuel}-{kind}",
        "fuel": fuel,
        "kind": "suggestion",
        "severity": severity,
        "title": title,
        "detail": detail,
        "suggestion": suggestion,
        "href": "#energy",
        "evidence": evidence,
    }


CHECK = {
    "electricity": "Think about what ran more: heating, laundry, cooking or anything left on.",
    "gas": "Check the heating schedule and thermostat; colder days explain some of a rise, not all of it.",
}


def _week_insight(fuel: str, info: dict) -> dict | None:
    week, name, through = info["week"], FUEL_NAME[fuel], date.fromisoformat(info["through"])
    change = week["change"]
    if change is None or abs(week["kwh"] - week["previous_kwh"]) < MIN_KWH_CHANGE:
        return None
    cost = ""
    if week["cost"] is not None and week["previous_cost"] is not None and week["cost"] != week["previous_cost"]:
        spent = week["cost"] - week["previous_cost"]
        cost = f" (about £{abs(spent):.2f} {'more' if spent > 0 else 'less'}, estimated)"
    detail = (
        f"{week['kwh']:.1f} kWh in the 7 days to {_day(through)} against {week['previous_kwh']:.1f} kWh "
        f"the week before{cost}."
    )
    evidence = {k: week[k] for k in ("kwh", "previous_kwh", "cost", "previous_cost", "change")}
    if change >= WEEK_CHANGE:
        return _insight(fuel, "week", "warning", f"{name} up {_pct(change)} this week", detail, CHECK[fuel], **evidence)
    if change <= -WEEK_DROP:
        return _insight(
            fuel, "week", "good", f"{name} down {_pct(change)} this week", detail,
            "Whatever changed is working; keep it up.", **evidence,
        )  # fmt: skip
    return None


def _month_insight(fuel: str, info: dict) -> dict | None:
    series, through, name = info["_series"], date.fromisoformat(info["through"]), FUEL_NAME[fuel]
    if through.day < MONTH_MIN_DAYS:
        return None
    previous_month_end = through.replace(day=1) - timedelta(days=1)
    days = [through.replace(day=d) for d in range(1, through.day + 1)]
    before = [previous_month_end.replace(day=min(d.day, previous_month_end.day)) for d in days]
    before = sorted(set(before))
    kwh, cost = _sums(series, days)
    previous_kwh, previous_cost = _sums(series, before)
    if kwh is None or not previous_kwh:
        return None
    # Per-day averages, so a 31-day month is not compared against 28 days of February.
    daily, previous_daily = kwh / len(days), previous_kwh / len(before)
    change = daily / previous_daily - 1
    if abs(change) < MONTH_CHANGE or abs(daily - previous_daily) * len(days) < MIN_KWH_CHANGE:
        return None
    month, last = f"{through:%B}", f"{previous_month_end:%B}"
    projected = None
    if cost is not None:
        projected = round(cost / through.day * monthrange(through.year, through.month)[1], 2)
    if cost is not None and previous_cost is not None:
        detail = (
            f"£{cost:.2f} for {kwh:.1f} kWh over 1–{through.day} {month}, {_pct(change)} "
            f"{'more' if change > 0 else 'less'} usage a day than the same days of {last} (£{previous_cost:.2f})."
        )
    else:
        detail = (
            f"{kwh:.1f} kWh over 1–{through.day} {month}, {_pct(change)} {'more' if change > 0 else 'less'} "
            f"a day than the same days of {last} ({previous_daily:.1f} kWh a day)."
        )
    title = f"{name} on track for about £{projected:.0f} in {month}" if projected else f"{name} this month"
    evidence = {
        "kwh": kwh,
        "previous_kwh": previous_kwh,
        "cost": cost,
        "previous_cost": previous_cost,
        "change": round(change, 4),
        "projected_cost": projected,
        "days": through.day,
    }
    if change > 0:
        return _insight(fuel, "month", "warning", title, detail, CHECK[fuel], **evidence)
    return _insight(
        fuel, "month", "good", title, detail, "Usage is lower than last month; no action needed.", **evidence
    )


def _unusual_insight(fuel: str, info: dict, anomalies: list[dict]) -> dict | None:
    series, through = info["_series"], date.fromisoformat(info["through"])
    recent = []
    for row in anomalies:
        try:
            day = date.fromisoformat(row.get("date", ""))
        except (TypeError, ValueError):
            continue
        if (
            row.get("fuel") == fuel
            and through - timedelta(days=6) <= day <= through
            and _number(row.get("consumption_kwh"))
        ):
            recent.append((day, float(row["consumption_kwh"])))
    usual = [series[d]["kwh"] for d in _span(through, 28) if d in series and series[d]["kwh"] is not None]
    if not recent or len(usual) < 14:
        return None
    day, kwh = max(recent)
    typical = median(usual)
    if typical <= 0:
        return None
    return _insight(
        fuel,
        "unusual",
        "warning",
        f"Unusual {fuel} day on {_day(day)}",
        f"{kwh:.1f} kWh, {kwh / typical:.1f}× your typical {typical:.1f} kWh a day over the last 4 weeks.",
        "Think about what ran that day. If nothing stands out, check for an appliance fault or a heating override.",
        date=day.isoformat(),
        kwh=round(kwh, 3),
        typical_kwh=round(typical, 3),
    )


def _always_on_insight(info: dict) -> dict | None:
    peak = info["_peak"]
    if not peak:
        return None
    hours = peak["hours"]
    baseline = sum(hours[h] for h in range(1, 5)) / 4
    average = sum(hours.values()) / 24
    if average <= 0:
        return None
    share = baseline / average
    if share < ALWAYS_ON_SHARE:
        return None
    return _insight(
        "electricity",
        "always-on",
        "info",
        f"Always-on load is about {baseline * 24:.1f} kWh a day",
        f"Between 01:00 and 05:00 the home still draws {baseline:.2f} kWh an hour, {share * 100:.0f}% of your "
        f"average hour. Usage peaks at {peak['hour']:02d}:00 ({peak['kwh']:.2f} kWh).",
        "Walk round at night: fridges, routers and heaters are expected; old freezers, dehumidifiers or "
        "devices on standby are worth switching off.",
        baseline_kwh_per_hour=round(baseline, 3),
        average_kwh_per_hour=round(average, 3),
        share=round(share, 3),
        peak_hour=peak["hour"],
    )


def energy_insights(energy: dict, fuels: dict) -> list[dict]:
    found = []
    for fuel, info in fuels.items():
        if info["state"] != "available":
            continue
        found += [_unusual_insight(fuel, info, energy.get("anomalies") or [])]
        week = _week_insight(fuel, info)
        month = _month_insight(fuel, info)
        # One trend per fuel: the week unless the month says more.
        found.append(week if week and (not month or week["severity"] == "warning") else month)
        if fuel == "electricity":
            found.append(_always_on_insight(info))
    found = [item for item in found if item]
    found.sort(key=lambda item: INSIGHT_ORDER[item["severity"]])
    return found[:MAX_INSIGHTS]


def public_fuels(fuels: dict) -> dict:
    return {fuel: {k: v for k, v in info.items() if not k.startswith("_")} for fuel, info in fuels.items()}


# Needs attention --------------------------------------------------------------------------------


def _item(severity: str, source: str, fact: str, href: str, **evidence) -> dict:
    kind = "action" if severity == "action" else "fact"
    return {"severity": severity, "kind": kind, "source": source, "fact": fact, "href": href, "evidence": evidence}


def _instagram_items(instagram: dict, job_flagged: bool, now: datetime) -> list[dict]:
    if instagram.get("state") != "available":
        return []
    local = now.astimezone(LONDON)
    drafts = instagram["drafts"]
    items = []
    failed = [d for d in drafts if d.get("publish_error") and d.get("status") not in ("rejected", "published")]
    if failed:
        items.append(
            _item(
                "critical", "instagram",
                f"{len(failed)} Instagram post{'s' if len(failed) != 1 else ''} failed to publish. "
                "Open the draft to see why, then post it by hand if you still want it.",
                "#instagram", failed_publishes=len(failed),
            )
        )  # fmt: skip
    pending = [d for d in drafts if d.get("status") == "pending"]
    if pending:
        fact = f"Review {len(pending)} Instagram draft{'s' if len(pending) != 1 else ''} awaiting your approval."
        upcoming = sorted(
            when
            for d in pending
            if d.get("will_post") and (when := _local(d.get("posts_at"))) and when > local
        )  # fmt: skip
        if instagram.get("auto_publish") and upcoming:
            fact += f" The next posts automatically at {upcoming[0]:%H:%M} on {_day(upcoming[0].date())} unless you reject it."
        items.append(
            _item(
                "action", "instagram", fact, "#instagram", pending_drafts=len(pending),
                next_auto_post_at=upcoming[0].isoformat(timespec="minutes") if instagram.get("auto_publish") and upcoming else None,
            )
        )  # fmt: skip
    today = local.date().isoformat()
    made_today = sum(1 for d in drafts if d.get("created_on") == today)
    if not job_flagged and instagram.get("latest_attempt") and local.hour >= INSTAGRAM_READY_HOUR and not made_today:
        items.append(
            _item(
                "warning", "instagram-daily",
                "No Instagram drafts were made today; the daily run normally finishes by 12:00.",
                "#automations", drafts_today=0,
            )
        )  # fmt: skip
    return items


def instagram_schedule(instagram: dict, now: datetime) -> dict:
    """Drafts made today and the next slot a draft is due to post in, for the home widget."""
    local = now.astimezone(LONDON)
    upcoming = [
        when
        for d in instagram["drafts"]
        if d.get("status") in ("pending", "approved") and (when := _local(d.get("posts_at"))) and when > local
    ]
    return {
        "drafts_today": sum(1 for d in instagram["drafts"] if d.get("created_on") == local.date().isoformat()),
        "next_slot": min(upcoming).isoformat(timespec="minutes") if upcoming else None,
    }


def attention(data: dict, now: datetime) -> list[dict]:
    """What needs the owner, from the automations' own execution evidence. Facts only, no suggestions."""
    items = []
    flagged = set()
    for job in data["automations"]:
        status, latest = job["status"], job.get("last_execution") or {}
        severity = SEVERITY.get(status)
        if job["id"] == "energy-mcp" or (job["id"] == "daily-briefing" and status == "stale"):
            continue  # on-demand with no log, and a manual job that is not expected to run every day
        if job["id"] == "instagram-daily" and status == "not_configured" and latest.get("outcome") == "not_configured":
            severity = "warning"
        if severity:
            flagged.add(job["id"])
            items.append(_item(severity, job["id"], f"{job['name']}: {job['summary']}", "#automations", status=status))
    health = {item["id"]: item for item in data["integrations"]}
    energy = health.get("octopus_energy")
    if energy and "energy-refresh" not in flagged:
        severity = SEVERITY.get(energy["status"])
        if severity:
            items.append(_item(severity, "octopus_energy", f"{energy['name']}: {energy['summary']}", "#energy",
                               status=energy["status"]))  # fmt: skip
        elif energy["status"] == "operational":
            latest = energy["evidence"].get("latest_daily_date")
            today = now.astimezone(LONDON).date()
            if latest is None or date.fromisoformat(latest) < today - timedelta(days=STALE_READINGS_DAYS):
                since = f"stop at {_day(date.fromisoformat(latest))}" if latest else "are missing for the last 14 days"
                items.append(
                    _item(
                        "warning", "octopus_energy",
                        f"Octopus syncs succeed but meter readings {since}. Check the meter's smart data in the Octopus app.",
                        "#energy", latest_daily_date=latest,
                    )
                )  # fmt: skip
    items += _instagram_items(data["instagram"], "instagram-daily" in flagged, now)
    items.sort(key=lambda item: SEVERITY_ORDER[item["severity"]])
    return items


def system_status(items: list[dict], automations: list[dict]) -> dict:
    """The one compact status element: derived from Needs attention, counted over automations with evidence."""
    severities = {item["severity"] for item in items}
    evidenced = [job for job in automations if job["status"] not in ("unverified", "not_configured")]
    state = "fault" if "critical" in severities else "degraded" if "warning" in severities else "nominal"
    if state == "nominal" and not evidenced:
        state = "standby"
    return {
        "state": state,
        "issues": sum(1 for item in items if item["severity"] in ("critical", "warning")),
        "actions": sum(1 for item in items if item["severity"] == "action"),
        "automations_monitored": len(evidenced),
        "automations_healthy": sum(1 for job in evidenced if job["status"] in ("operational", "running")),
    }
