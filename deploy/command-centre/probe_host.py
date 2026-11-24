"""Read-only hosting inventory. Never print environment values or raw API output."""

# Bootstrap with the host's Python 3.6 before discovering a supported application interpreter.
# ruff: noqa: UP021, UP022

import glob
import json
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path


def inspect_command(args, timeout):
    try:
        return subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "")


def cpanel_registration(data):
    if isinstance(data, dict):
        records = list(data.values())
    elif isinstance(data, list):
        records = data
    else:
        return None
    if not records:
        return False
    recognized = False
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("base_uri"), str):
            continue
        recognized = True
        if record["base_uri"].strip("/") == "command-centre" and record.get("domain", "iramkhan.com") == "iramkhan.com":
            return True
    return False if recognized else None


def app_diagnostics(app):
    result = {"mapping": {}, "error_categories": [], "permissions": {}}
    home = Path.home()
    for label, path in (
        ("app", app),
        ("webroot", home / "public_html"),
        ("mount", home / "public_html/command-centre"),
        ("mount_mapping", home / "public_html/command-centre/.htaccess"),
    ):
        try:
            result["permissions"][label] = oct(stat.S_IMODE(path.stat().st_mode))
        except OSError:
            pass
    for label, path in (
        ("domain", home / "public_html/.htaccess"),
        ("mount", home / "public_html/command-centre/.htaccess"),
    ):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        result["mapping"][label] = {
            "app_root_matches": str(app) in body,
            "directives": sorted(set(re.findall(r"(?m)^\s*(Passenger\w+|AuthType|Require)\b", body))),
            "passenger_disabled": bool(re.search(r"(?im)^\s*PassengerEnabled\s+off\b", body)),
        }
    categories = set()
    for path in (app / "stderr.log", app / "passenger.log", home / "public_html/error_log"):
        try:
            with path.open("rb") as source:
                source.seek(max(0, path.stat().st_size - 64 * 1024))
                body = source.read().decode("utf-8", "replace")
        except OSError:
            continue
        for category in (
            "ModuleNotFoundError",
            "ImportError",
            "PermissionError",
            "RuntimeError",
            "SyntaxError",
            "Permission denied",
            "cannot load such file",
            "Could not spawn",
            "No such file or directory",
        ):
            if category in body:
                categories.add(category)
    result["error_categories"] = sorted(categories)
    return result


def story_insights(checkout):
    database = checkout / "data" / "instagram.sqlite3"
    if not database.is_file():
        return {"status": "database_missing"}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT media_id, strftime('%Y-%m-%dT%H:%M:%SZ', posted_at), "
            "strftime('%Y-%m-%dT%H:%M:%SZ', captured_at), metrics, navigation_breakdown "
            "FROM story_insights WHERE datetime(captured_at)>=? "
            "AND datetime(captured_at)>=datetime(posted_at) "
            "AND datetime(captured_at)<datetime(posted_at,'+24 hours') "
            "AND (datetime(posted_at)=? OR datetime(posted_at)>datetime('now','-24 hours')) "
            "ORDER BY datetime(posted_at)=? DESC, datetime(captured_at) DESC LIMIT 5",
            ("2026-11-24 16:40:49", "2026-11-23 18:25:14", "2026-11-23 18:25:14"),
        ).fetchall()
    metrics = ("reach", "views", "replies", "navigation", "follows", "profile_visits", "shares", "total_interactions")
    actions = ("tap_forward", "tap_back", "tap_exit", "swipe_forward")
    result = []
    for media_id, posted_at, captured_at, metrics_json, actions_json in rows:
        values, breakdown = json.loads(metrics_json), json.loads(actions_json)
        counts = {key: values[key] for key in metrics if type(values.get(key)) is int}
        if not counts or not re.fullmatch(r"[0-9]+", media_id):
            continue
        result.append(
            {
                "media_id": media_id,
                "posted_at": posted_at,
                "captured_at": captured_at,
                "metrics": counts,
                "navigation_breakdown": {key: breakdown[key] for key in actions if type(breakdown.get(key)) is int},
            }
        )
    return {"status": "captured" if result else "no_live_snapshot", "rows": result}


def main():
    checkout = Path(sys.argv[1])
    app = checkout.parent / "command-centre-app"
    interpreters = {}
    candidates = [
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3",
        *glob.glob("/opt/alt/python*/bin/python3"),
    ]
    for name in candidates:
        executable = shutil.which(name)
        if executable:
            result = inspect_command([executable, "--version"], 10)
            interpreters[name] = result.stdout.strip() if result.returncode == 0 else "unavailable"
    report = {
        "interpreters": interpreters,
        "passenger_config": bool(shutil.which("passenger-config")),
        "cloudlinux_selector": bool(shutil.which("cloudlinux-selector")),
        "passenger_directory": Path("/usr/share/passenger").is_dir(),
        "app_directory_exists": app.is_dir(),
        "entrypoint_exists": (app / "passenger_wsgi.py").is_file(),
        "energy_database_exists": (checkout / "data/octopus_energy.sqlite3").is_file(),
        "passenger_registry": "unavailable",
        "command_centre_registered": None,
        "selector_registry": "unavailable",
        "selector_enabled": None,
        "selector_python312_enabled": None,
        "selector_command_centre_registered": None,
        "app_diagnostics": app_diagnostics(app),
        "story_insights": story_insights(checkout),
    }
    uapi = shutil.which("uapi") or (
        "/usr/local/cpanel/bin/uapi" if Path("/usr/local/cpanel/bin/uapi").is_file() else None
    )
    if uapi:
        result = inspect_command([uapi, "--output=json", "PassengerApps", "list_applications"], 30)
        try:
            response = json.loads(result.stdout).get("result", {})
            if response.get("status") == 1:
                data = response.get("data", {})
                # Emit a boolean only: the registry can contain protected environment values.
                report["passenger_registry"] = "accessible"
                report["command_centre_registered"] = cpanel_registration(data)
            else:
                report["passenger_registry"] = "not_accessible_or_feature_disabled"
        except (ValueError, AttributeError):
            report["passenger_registry"] = "unreadable_response"
    selector = shutil.which("cloudlinux-selector")
    if selector:
        result = inspect_command([selector, "get", "--json", "--interpreter", "python", "--domain", "iramkhan.com"], 60)
        try:
            response = json.loads(result.stdout)
            if response.get("result") == "success":
                report["selector_registry"] = "accessible"
                report["selector_enabled"] = response.get("selector_enabled")
                versions = response.get("available_versions", {})
                report["selector_python312_enabled"] = any(
                    version.startswith("3.12") and info.get("status") == "enabled" for version, info in versions.items()
                )

                def registered(node):
                    if isinstance(node, dict):
                        if (
                            node.get("domain") == "iramkhan.com"
                            and str(node.get("app_uri", "")).strip("/") == "command-centre"
                        ):
                            report["selector_app_status"] = (
                                node.get("app_status")
                                if node.get("app_status") in ("started", "stopped")
                                else "unknown"
                            )
                            report["selector_startup_matches"] = node.get("startup_file") == "passenger_wsgi.py"
                            return True
                        return any(registered(value) for key, value in node.items() if key != "env_vars")
                    return isinstance(node, list) and any(registered(value) for value in node)

                report["selector_command_centre_registered"] = registered(response)
            else:
                report["selector_registry"] = "not_accessible_or_feature_disabled"
        except (ValueError, AttributeError):
            report["selector_registry"] = "unreadable_response"
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
