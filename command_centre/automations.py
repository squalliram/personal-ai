"""Read-only automation registry. Nothing here installs, removes or triggers a job."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta

from octopus_energy.refresh_install import MARKER

from .config import Settings
from .sources import _instant, energy_refresh_status

CronReader = Callable[[], tuple[str, str]]
PHASE_5 = {"available": False, "reason": "Arrives in Phase 5 with explicit approval and audit."}
CONTROLS = {"enable": PHASE_5, "disable": PHASE_5, "run_now": PHASE_5}


def read_crontab() -> tuple[str, str]:
    """Return (state, text). The text stays in memory: only marker counts leave this module."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unavailable", ""
    if result.returncode == 0:
        return "available", result.stdout.decode("utf-8", "replace")
    if b"no crontab" in result.stderr.lower():
        return "absent", ""
    return "unavailable", ""


def marked_entries(text: str) -> int:
    marker = MARKER.decode()
    return sum(1 for line in text.splitlines() if line.rstrip().endswith(marker) and not line.lstrip().startswith("#"))


def _energy_refresh(settings: Settings, now: datetime, cron: tuple[str, str], energy: dict) -> dict:
    cron_state, cron_text = cron
    count = marked_entries(cron_text) if cron_state == "available" else (0 if cron_state == "absent" else None)
    state, refresh = energy_refresh_status(settings)
    alerts = []
    if count is not None and count > 1:
        status, summary = "degraded", f"{count} marked energy cron entries exist on this host; expected one."
        alerts.append("duplicate_cron_entries")
    elif refresh is None:
        status = "unverified"
        summary = (
            "No valid refresh status file on this host. Production runs on the cPanel host, which this "
            "machine cannot inspect."
        )
    elif refresh["result"] == "failed":
        status, summary = "failed", f"The latest refresh failed ({refresh['error_category']})."
    elif refresh["result"] == "running":
        status, summary = "running", "A refresh is running."
    elif refresh["result"] in ("partial", "no_data"):
        status, summary = "degraded", "The latest refresh returned incomplete data."
    else:
        success = _instant(refresh["last_success_at"])
        if success is None or now - success > timedelta(hours=settings.energy_stale_hours):
            status, summary = "stale", f"No successful refresh in the last {settings.energy_stale_hours} hours."
        else:
            status, summary = "operational", "The latest refresh succeeded recently."
    return {
        "id": "energy-refresh",
        "name": "Energy refresh",
        "purpose": "Collect Octopus readings and tariffs, then publish the protected energy dashboard.",
        "trigger": "Cron every minute; collects only when due (default 60 minutes) or requested from the dashboard.",
        "installed_by": "GitHub deployment workflow on the cPanel host (scripts/install-energy-refresh.sh).",
        "integrations": ["octopus_energy"],
        "status": status,
        "summary": summary,
        "alerts": alerts,
        "last_execution": {
            "attempt_at": refresh["last_attempt_at"] if refresh else None,
            "success_at": refresh["last_success_at"] if refresh else None,
            "result": refresh["result"] if refresh else None,
            "error_category": refresh["error_category"] if refresh else None,
            "status_file": state,
        },
        "next_execution": None,
        "next_execution_note": "Not derivable: each cron tick decides whether collection is due.",
        "local_cron": {"state": cron_state, "marked_entries": count},
        "stop_control": {
            "script": "scripts/stop-energy.sh",
            "effective": False,
            "note": "The script is a no-op. It does not remove or pause the cron entry.",
        },
        "execution_history": energy["sync_history"],
        "controls": CONTROLS,
    }


def list_automations(
    settings: Settings, now: datetime, cron_reader: CronReader, energy: dict, briefing: dict, health: dict
) -> list[dict]:
    cron = cron_reader() if settings.inspect_crontab else ("not_inspected", "")
    briefing_status = health["daily_briefing"]
    return [
        _energy_refresh(settings, now, cron, energy),
        {
            "id": "daily-briefing",
            "name": "Daily briefing",
            "purpose": "Build a Markdown briefing from a read-only Calendar and Notion snapshot.",
            "trigger": "Manual: scripts/start.sh --snapshot snapshot.json. Not scheduled.",
            "installed_by": None,
            "integrations": ["calendar_snapshot", "notion_snapshot"],
            "status": briefing_status["status"],
            "summary": briefing_status["summary"],
            "alerts": [],
            "last_execution": briefing["runs"][0] if briefing["runs"] else None,
            "next_execution": None,
            "next_execution_note": "No schedule exists.",
            "local_cron": None,
            "stop_control": {
                "script": "scripts/stop.sh",
                "effective": "not_applicable",
                "note": "The briefing runs once in the foreground; there is no scheduled job to stop.",
            },
            "execution_history": briefing["runs"],
            "controls": CONTROLS,
        },
        {
            "id": "energy-mcp",
            "name": "Energy MCP tools",
            "purpose": "Eight read-only energy tools for AI agents over MCP stdio.",
            "trigger": "On demand, started by an MCP client.",
            "installed_by": None,
            "integrations": ["octopus_energy"],
            "status": "unverified",
            "summary": "On-demand tool with no execution log, so its health cannot be confirmed.",
            "alerts": [],
            "last_execution": None,
            "next_execution": None,
            "next_execution_note": "Runs only when a client starts it.",
            "local_cron": None,
            "stop_control": None,
            "execution_history": [],
            "controls": CONTROLS,
        },
    ]
