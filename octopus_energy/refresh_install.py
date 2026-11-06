"""Install the private refresh configuration and one owned cron entry."""

import argparse
import json
import os
import pwd
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = b"# personal-ai-energy-refresh"


def _crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True, timeout=10)
    if result.returncode == 0:
        return result.stdout
    user = pwd.getpwuid(os.getuid()).pw_name.encode()
    absent = (b"no crontab for " + user, b"crontab: no crontab for " + user)
    if result.returncode == 1 and not result.stdout and result.stderr.strip() in absent:
        return b""
    raise ValueError("Existing crontab could not be verified")


def _marked_crontab(previous, app):
    retained = []
    for line in previous.splitlines(keepends=True):
        body = line.rstrip(b"\r\n").rstrip(b" \t")
        if body.endswith(MARKER) and not body.lstrip().startswith(b"#"):
            continue
        retained.append(line)
    prefix = b"".join(retained)
    if prefix and not prefix.endswith(b"\n"):
        prefix += b"\n"
    command = f"* * * * * cd {shlex.quote(str(app))} && ./scripts/refresh-energy.sh >/dev/null 2>&1 "
    return prefix + command.encode() + MARKER + b"\n"


def _replace_config(path, body):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_refresh(app, target, interval=60):
    """Preserve unrelated cron bytes and restore config if installation fails."""
    app, target = Path(app), Path(target)
    if (
        not app.is_absolute()
        or not target.is_absolute()
        or any(character in str(app) + str(target) for character in ("\0", "\n", "\r", "%"))
        or not app.is_dir()
        or not target.is_dir()
        or type(interval) is not int
        or interval < 1
    ):
        raise ValueError("Invalid refresh installation configuration")
    app, target = app.resolve(), target.resolve()
    runner = app / "scripts/refresh-energy.sh"
    data = app / "data"
    private = data / "energy-refresh"
    if not runner.is_file() or not os.access(runner, os.X_OK) or data.is_symlink() or private.is_symlink():
        raise ValueError("Private refresh paths could not be verified")
    if private.is_relative_to(target) or target.is_relative_to(private):
        raise ValueError("Refresh state must stay outside the published directory")
    previous_cron = _crontab()  # An unavailable crontab must not change config.
    desired_cron = _marked_crontab(previous_cron, app)
    data.mkdir(exist_ok=True)
    private.mkdir(mode=0o700, exist_ok=True)
    if stat.S_IMODE(private.stat().st_mode) != 0o700 or private.stat().st_uid != os.getuid():
        raise ValueError("Refresh state directory must be private and owned by the current user")
    config = private / "config.json"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise ValueError("Refresh configuration is not a regular file")
    previous_config = config.read_bytes() if config.exists() else None
    body = (
        json.dumps({"DASHBOARD_TARGET": str(target), "ENERGY_REFRESH_INTERVAL_MINUTES": interval}, indent=2).encode()
        + b"\n"
    )
    _replace_config(config, body)
    try:
        if desired_cron != previous_cron:
            result = subprocess.run(["crontab", "-"], input=desired_cron, capture_output=True, timeout=10)
            if result.returncode != 0:
                raise ValueError("Marked refresh cron could not be installed")
    except BaseException:
        if previous_config is None:
            config.unlink(missing_ok=True)
        else:
            _replace_config(config, previous_config)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-path", type=Path, default=Path.cwd())
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    try:
        install_refresh(args.app_path, args.target, args.interval)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Energy refresh installation failed safely; cron/configuration not confirmed.", file=sys.stderr)
        return 1
    print("Energy refresh configuration and one marked cron entry installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
