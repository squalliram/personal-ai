"""Read-only hosting inventory. Never print environment values or raw API output."""

# Bootstrap with the host's Python 3.6 before discovering a supported application interpreter.
# ruff: noqa: UP021, UP022

import glob
import json
import re
import shutil
import stat
import subprocess
import sys
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
