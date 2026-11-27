"""Upsert the Instagram pipeline's provider keys and publishing settings into the server's `.env`.

Values arrive as KEY=VALUE lines on stdin (never argv, so they stay out of process
listings). Only ALLOWED keys are accepted, empty values are skipped, every other line
of the file is kept, and the result is written atomically with mode 0600. Selected
existing values can be preserved so deployment bootstraps but never replaces them.
"""

import argparse
import fcntl
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from octopus_energy.cli import parse_env

ALLOWED = (
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_USER_ID",
    "INSTAGRAM_AUTO_PUBLISH",
    "INSTAGRAM_PUBLIC_DIR",
    "INSTAGRAM_PUBLIC_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "COMMAND_CENTRE_URL",
    "JARVIS_ALERT_WEBHOOK_URL",
    "JARVIS_ALERT_WEBHOOK_SECRET",
)
SAFE_VALUE = re.compile(r"[A-Za-z0-9._~+/=-]+|https://[A-Za-z0-9._~/-]+")
# Bot tokens contain ':' and group chat IDs start with '-'; only these keys allow them.
KEY_VALUES = {
    "TELEGRAM_BOT_TOKEN": re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}"),
    "TELEGRAM_CHAT_ID": re.compile(r"-?[0-9]{1,20}"),
    "COMMAND_CENTRE_URL": re.compile(r"https://[A-Za-z0-9._~/-]+"),
    "JARVIS_ALERT_WEBHOOK_URL": re.compile(
        r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*)?"
    ),
    "JARVIS_ALERT_WEBHOOK_SECRET": re.compile(r"[A-Za-z0-9._~+/=:-]{8,256}"),
}


class EnvChangedError(ValueError):
    """The file no longer matches the values expected by the caller."""


def parse(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in ALLOWED:
            raise ValueError("unexpected input line")
        if value:
            if not KEY_VALUES.get(key, SAFE_VALUE).fullmatch(value):
                raise ValueError(f"{key} has unexpected characters")
            values[key] = value
    return values


@contextmanager
def _write_lock(path: Path):
    """Serialize the complete read-modify-replace sequence across deploys and cron."""
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb+") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def upsert(
    path: Path,
    values: dict[str, str],
    *,
    preserve: set[str] | None = None,
    if_current: dict[str, str] | None = None,
) -> bool:
    if not values:
        return False
    preserve = preserve or set()
    with _write_lock(path):
        if path.is_symlink():
            raise ValueError("refusing a symlinked env file")
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        existing = parse_env(original) if preserve or if_current else {}
        if if_current and any(existing.get(key) != value for key, value in if_current.items()):
            raise EnvChangedError("env file changed")
        values = {
            key: value for key, value in values.items() if key not in preserve or not existing.get(key, "").strip()
        }
        if not values:
            if path.exists():
                os.chmod(path, 0o600)
            return False
        remaining = dict(values)
        out = []
        for line in original.splitlines():
            key = line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#") and key in values:
                if key in remaining:
                    out.append(f"{key}={remaining.pop(key)}")
            else:
                out.append(line)
        out.extend(f"{key}={value}" for key, value in remaining.items())
        content = "\n".join(out) + "\n"
        if original == content:
            os.chmod(path, 0o600)
            return False
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--preserve-existing", action="append", choices=ALLOWED, default=[])
    args = parser.parse_args(argv)
    path = Path(args.env_file)
    if path.is_symlink():
        print("refusing a symlinked env file", file=sys.stderr)
        return 1
    try:
        changed = upsert(path, parse(sys.stdin.read()), preserve=set(args.preserve_existing))
    except (OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"env_install: {message}", file=sys.stderr)
        return 1
    print("instagram env: updated" if changed else "instagram env: unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
