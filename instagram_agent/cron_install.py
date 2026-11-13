"""Install one owned cron entry that runs the daily Instagram drafts tick every 15 minutes."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from octopus_energy.refresh_install import _crontab

MARKER = b"# personal-ai-instagram-daily"


def marked_crontab(previous: bytes, app: Path) -> bytes:
    retained = [
        line
        for line in previous.splitlines(keepends=True)
        if not (line.rstrip(b"\r\n \t").endswith(MARKER) and not line.lstrip().startswith(b"#"))
    ]
    prefix = b"".join(retained)
    if prefix and not prefix.endswith(b"\n"):
        prefix += b"\n"
    command = f"*/15 * * * * cd {shlex.quote(str(app))} && ./scripts/instagram-daily.sh >/dev/null 2>&1 "
    return prefix + command.encode() + MARKER + b"\n"


def install(app: Path) -> None:
    if not app.is_absolute() or any(c in str(app) for c in ("\0", "\n", "\r", "%")) or not app.is_dir():
        raise ValueError("Invalid application path")
    app = app.resolve()
    runner = app / "scripts/instagram-daily.sh"
    if not runner.is_file() or not os.access(runner, os.X_OK):
        raise ValueError("Daily runner could not be verified")
    previous = _crontab()
    desired = marked_crontab(previous, app)
    if desired != previous:
        result = subprocess.run(["crontab", "-"], input=desired, capture_output=True, timeout=10)
        if result.returncode != 0:
            raise ValueError("Marked cron entry could not be installed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-path", type=Path, default=Path.cwd())
    try:
        install(parser.parse_args().app_path)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Instagram daily cron installation failed safely; cron not confirmed.", file=sys.stderr)
        return 1
    print("One marked Instagram daily cron entry installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
