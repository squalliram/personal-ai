"""Read-only adapters and evidence-based health for existing Personal AI data sources.

Every database is opened with SQLite ``mode=ro``. Never call the energy storage, report, dashboard
or CLI writers from here: on a legacy database they rebuild derived tables.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from octopus_energy.mcp_server import RefreshRequiredError, dispatch
from octopus_energy.refresh import validate_status

from .config import Settings

LONDON = ZoneInfo("Europe/London")
HEALTH_STATES = (
    "operational",
    "running",
    "degraded",
    "stale",
    "failed",
    "refresh_required",
    "not_configured",
    "not_implemented",
    "unverified",
)
ENERGY_BASIS = {
    "consumption_kwh": "measured",
    "total_cost_gbp": "estimated",
    "anomalies": "inferred",
    "tariffs": "source_record",
}
_REPORT_NAME = re.compile(r"\d{4}-\d\d-\d\d\.md")


def connect_read_only(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Database unavailable")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _health(
    source_id: str, name: str, domain: str, kind: str, status: str, summary: str, now: datetime, **evidence
) -> dict:
    assert status in HEALTH_STATES
    return {
        "id": source_id,
        "name": name,
        "domain": domain,
        "kind": kind,
        "status": status,
        "summary": summary,
        "evidence": evidence,
        "checked_at": now.isoformat(timespec="seconds"),
    }


# Energy -----------------------------------------------------------------------------------------


def energy_snapshot(settings: Settings, now: datetime) -> dict:
    path = settings.energy_db
    result = {
        "source": "octopus_energy",
        "state": "not_configured",
        "basis": ENERGY_BASIS,
        "actual_bills": {
            "available": False,
            "reason": "No bill source is integrated. Costs are estimates from stored tariff rates.",
        },
        "notice": "Whole-home meter data cannot identify individual appliances.",
        "totals": [],
        "daily": [],
        "monthly": [],
        "tariffs": [],
        "anomalies": [],
        "sync_history": [],
        "last_successful_sync_at": None,
    }
    if path.is_symlink() or not path.is_file():
        return result
    try:
        with closing(connect_read_only(path)) as conn:
            result["sync_history"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT started_at,ended_at,status,electricity_records,gas_records,error_category "
                    "FROM sync_history ORDER BY id DESC LIMIT 10"
                )
            ]
            success = conn.execute(
                "SELECT ended_at FROM sync_history WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            result["last_successful_sync_at"] = success["ended_at"] if success else None
    except (OSError, sqlite3.Error):
        result["state"] = "unavailable"
        return result
    today = now.date()
    try:
        result["totals"] = dispatch("get_energy_summary", {}, path)
        result["daily"] = dispatch(
            "get_daily_consumption",
            {"start": (today - timedelta(days=14)).isoformat(), "end": (today + timedelta(days=1)).isoformat()},
            path,
        )
        result["monthly"] = dispatch("get_monthly_bill_comparison", {}, path)[:12]
        result["tariffs"] = dispatch("get_tariff_details", {}, path)
        result["anomalies"] = sorted(
            dispatch("detect_energy_anomalies", {}, path), key=lambda row: row["date"], reverse=True
        )[:10]
        result["state"] = "available"
    except RefreshRequiredError:
        result["state"] = "refresh_required"
    except (OSError, sqlite3.Error):
        result["state"] = "unavailable"
    return result


def energy_health(snapshot: dict, settings: Settings, now: datetime) -> dict:
    history = snapshot["sync_history"]
    latest = history[0] if history else {}
    evidence = {
        "latest_sync_status": latest.get("status"),
        "latest_sync_ended_at": latest.get("ended_at"),
        "latest_error_category": latest.get("error_category"),
        "last_successful_sync_at": snapshot["last_successful_sync_at"],
        "latest_daily_date": max((row["date"] for row in snapshot["daily"]), default=None),
    }
    state = snapshot["state"]
    if state == "not_configured":
        status, summary = "not_configured", "No energy database exists at the configured path."
    elif state == "unavailable":
        status, summary = "failed", "The energy database could not be read."
    elif state == "refresh_required":
        status = "refresh_required"
        summary = (
            "The database uses the legacy derived-cost schema. Read-only views stay unavailable until an "
            "approved sync with the current code migrates it."
        )
    elif not latest:
        status, summary = "unverified", "No sync has been recorded."
    elif latest["status"] == "failed":
        status, summary = "failed", "The latest Octopus sync failed."
    elif latest["status"] in ("partial", "no_data"):
        status, summary = "degraded", "The latest Octopus sync returned incomplete data; earlier data was kept."
    elif latest["status"] == "running":
        status, summary = "running", "An Octopus sync is recorded as running."
    else:
        success = _instant(snapshot["last_successful_sync_at"])
        if success is None or now - success > timedelta(hours=settings.energy_stale_hours):
            status = "stale"
            summary = f"No successful sync in the last {settings.energy_stale_hours} hours."
        else:
            status, summary = "operational", "The latest Octopus sync succeeded recently."
    return _health("octopus_energy", "Octopus Energy", "energy", "local_sqlite", status, summary, now, **evidence)


def energy_refresh_status(settings: Settings) -> tuple[str, dict | None]:
    """Read the payload-free refresh status published by the energy runner."""
    path = settings.energy_status
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "invalid", None
    if not stat.S_ISREG(info.st_mode):
        return "invalid", None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            raw = source.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            return "invalid", None
        return "valid", validate_status(json.loads(raw))
    except (OSError, ValueError, TypeError):
        return "invalid", None


# Daily briefing ---------------------------------------------------------------------------------


def briefing_snapshot(settings: Settings, limit: int = 14) -> dict:
    result = {"state": "not_configured", "runs": [], "latest_report_date": None}
    reports = settings.briefing_reports
    if reports.is_dir() and not reports.is_symlink():
        names = [p.name for p in reports.iterdir() if _REPORT_NAME.fullmatch(p.name) and not p.is_symlink()]
        result["latest_report_date"] = max(names)[:10] if names else None
    path = settings.briefing_db
    if path.is_symlink() or not path.is_file():
        return result
    try:
        with closing(connect_read_only(path)) as conn:
            # Never select `errors`: it can carry connector text, which is untrusted content.
            result["runs"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT report_date,started_at,ended_at,duration_ms,calendar_events,tasks_processed,error_count "
                    "FROM execution_history ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            ]
        result["state"] = "available"
    except (OSError, sqlite3.Error):
        result["state"] = "unavailable"
    return result


def briefing_health(snapshot: dict, now: datetime) -> dict:
    latest = snapshot["runs"][0] if snapshot["runs"] else {}
    evidence = {
        "latest_report_date": latest.get("report_date"),
        "latest_run_ended_at": latest.get("ended_at"),
        "latest_error_count": latest.get("error_count"),
        "latest_report_file_date": snapshot["latest_report_date"],
    }
    if snapshot["state"] == "unavailable":
        status, summary = "failed", "The briefing run history could not be read."
    elif not latest:
        status, summary = "not_configured", "No briefing run has been recorded."
    elif latest["error_count"]:
        status, summary = "degraded", f"The latest run reported {latest['error_count']} connector error(s)."
    elif latest["report_date"] != now.date().isoformat():
        status = "stale"
        summary = "No briefing run is recorded for today. The briefing is not scheduled; run scripts/start.sh."
    else:
        status, summary = "operational", "Today's briefing ran without connector errors."
    return _health("daily_briefing", "Daily briefing", "productivity", "local_sqlite", status, summary, now, **evidence)


# Registry ---------------------------------------------------------------------------------------

NOT_IMPLEMENTED = (
    ("career", "Career and job monitoring", "career", "No job-monitoring workflow exists in this repository."),
    ("finance", "Finance and mortgage", "finance", "No mortgage or finance data source exists in this repository."),
    (
        "family",
        "Family organisation",
        "family",
        "No family calendar, school or travel source exists in this repository.",
    ),
    ("health_fitness", "Health and fitness", "health", "No badminton, swimming, sleep or nutrition source exists."),
    (
        "caldav",
        "CalDAV calendar",
        "calendar",
        "A CalDAV integration was described, but no CalDAV code exists in this repository. Its location needs confirming.",
    ),
)


def integrations(settings: Settings, now: datetime, energy: dict, briefing: dict) -> list[dict]:
    latest = briefing["runs"][0] if briefing["runs"] else {}
    bridge = (
        "No direct connector exists in this repository. Data arrives through a read-only connector snapshot "
        "passed to the daily briefing, so connector health is not observable here."
    )
    found = [
        energy_health(energy, settings, now),
        briefing_health(briefing, now),
        _health(
            "calendar_snapshot",
            "Calendar (connector snapshot)",
            "calendar",
            "snapshot_bridge",
            "unverified",
            bridge,
            now,
            latest_events_in_briefing=latest.get("calendar_events"),
        ),
        _health(
            "notion_snapshot",
            "Notion tasks (connector snapshot)",
            "tasks",
            "snapshot_bridge",
            "unverified",
            bridge,
            now,
            latest_tasks_in_briefing=latest.get("tasks_processed"),
        ),
    ]
    found += [
        _health(source_id, name, domain, "none", "not_implemented", summary, now)
        for source_id, name, domain, summary in NOT_IMPLEMENTED
    ]
    return found


MODULES = (
    ("overview", "Overview", "available", 1, ("octopus_energy", "daily_briefing"),
     "Integration health, briefing run status and energy summary. Priorities and recommendations arrive in Phases 2 and 4."),
    ("my-day", "My Day", "planned", 2, ("daily_briefing", "calendar_snapshot", "notion_snapshot"),
     "Will show the daily briefing safely once the snapshot contract is validated."),
    ("tasks", "Tasks", "planned", 2, ("notion_snapshot",),
     "Will show Notion tasks from the read-only snapshot, de-duplicated and ranked from supplied fields only."),
    ("calendar", "Calendar", "planned", 2, ("calendar_snapshot", "caldav"),
     "Will show events from the read-only snapshot. The CalDAV source needs locating first."),
    ("energy", "Energy", "available", 1, ("octopus_energy",),
     "Measured consumption, estimated costs, stored tariffs and statistical anomalies. Actual bills are not integrated."),
    ("career", "Career", "not_implemented", 3, ("career",), "No job-monitoring workflow exists yet."),
    ("finance", "Finance", "not_implemented", 3, ("finance",), "No mortgage or finance source exists yet."),
    ("family", "Family", "not_implemented", 3, ("family",), "No family source exists yet."),
    ("health", "Health", "not_implemented", 3, ("health_fitness",), "No health or fitness source exists yet."),
    ("agents", "AI Agents", "partial", 5, ("daily_briefing", "octopus_energy"),
     "Agent definitions live in agents/. Execution evidence appears under Automations."),
    ("automations", "Automations", "available", 1, ("octopus_energy", "daily_briefing"),
     "Read-only registry with execution evidence. Enable, disable and run-now controls arrive in Phase 5."),
    ("integrations", "Integrations", "available", 1, (), "Evidence-based health for every known source."),
    ("settings", "Settings", "available", 1, (), "Session, permission level and audit log."),
)  # fmt: skip


def modules(found: list[dict]) -> list[dict]:
    by_id = {item["id"]: item["status"] for item in found}
    return [
        {
            "id": module_id,
            "name": name,
            "state": state,
            "phase": phase,
            "summary": summary,
            "sources": [{"id": source, "status": by_id.get(source, "not_implemented")} for source in sources],
        }
        for module_id, name, state, phase, sources, summary in MODULES
    ]


# Instagram --------------------------------------------------------------------------------------

DRAFT_ID = re.compile(r"[0-9a-f]{16}")
DRAFT_COLUMNS = (
    "draft_id,created_on,created_at,status,category,topic,angle,slides,caption,hashtags,"
    "review_score,review_notes,scheduled_for,decided_at"
)


def instagram_drafts(settings: Settings, limit: int = 30) -> dict:
    """Daily drafts awaiting review and recent decisions. Scores and publish times are suggestions."""
    result = {"source": "instagram", "state": "not_configured", "basis": "inferred", "drafts": [], "runs": []}
    path = settings.instagram_db
    if path is None or path.is_symlink() or not path.is_file():
        return result
    try:
        with closing(connect_read_only(path)) as conn:
            rows = conn.execute(f"SELECT {DRAFT_COLUMNS} FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,))
            rows = rows.fetchall()
            runs = conn.execute(
                "SELECT run_on,started_at,outcome,drafts,error FROM daily_runs ORDER BY id DESC LIMIT 10"
            ).fetchall()
    except (OSError, sqlite3.Error):
        result["state"] = "unavailable"
        return result
    for row in rows:
        try:
            slides = json.loads(row["slides"])
        except (TypeError, ValueError):
            slides = []
        result["drafts"].append(
            {
                **dict(row),
                "slides": [{"text": str(s.get("text", ""))} for s in slides if isinstance(s, dict)],
                "hashtags": (row["hashtags"] or "").split(),
            }
        )
    result["runs"] = [dict(run) for run in runs]
    result["state"] = "available"
    return result
