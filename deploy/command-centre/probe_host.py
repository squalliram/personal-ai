"""Read-only hosting inventory. Never print environment values or raw API output."""

# Bootstrap with the host's Python 3.6 before discovering a supported application interpreter.
# ruff: noqa: UP021, UP022

import glob
import json
import shutil
import subprocess
import sys
from pathlib import Path


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
            result = subprocess.run(
                [executable, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=10,
            )
            interpreters[name] = result.stdout.strip() or result.stderr.strip()
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
    }
    uapi = shutil.which("uapi") or (
        "/usr/local/cpanel/bin/uapi" if Path("/usr/local/cpanel/bin/uapi").is_file() else None
    )
    if uapi:
        result = subprocess.run(
            [uapi, "--output=json", "PassengerApps", "list_applications"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        try:
            response = json.loads(result.stdout).get("result", {})
            if response.get("status") == 1:
                data = response.get("data", {})
                # Emit a boolean only: the registry can contain protected environment values.
                report["passenger_registry"] = "accessible"
                report["command_centre_registered"] = "/command-centre" in json.dumps(data)
            else:
                report["passenger_registry"] = "not_accessible_or_feature_disabled"
        except (ValueError, AttributeError):
            report["passenger_registry"] = "unreadable_response"
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
