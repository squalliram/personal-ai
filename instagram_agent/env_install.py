"""Upsert the Instagram pipeline's provider keys into the server's `.env`.

Values arrive as KEY=VALUE lines on stdin (never argv, so they stay out of process
listings). Only ALLOWED keys are accepted, empty values are skipped, every other line
of the file is kept, and the result is written atomically with mode 0600.
"""

import argparse
import os
import re
import sys
from pathlib import Path

ALLOWED = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")
SAFE_VALUE = re.compile(r"[A-Za-z0-9._~+/=-]+")


def parse(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in ALLOWED:
            raise ValueError("unexpected input line")
        if value:
            if not SAFE_VALUE.fullmatch(value):
                raise ValueError(f"{key} has unexpected characters")
            values[key] = value
    return values


def upsert(path: Path, values: dict[str, str]) -> bool:
    if not values:
        return False
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in remaining.items())
    content = "\n".join(out) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)
    path = Path(args.env_file)
    if path.is_symlink():
        print("refusing a symlinked env file", file=sys.stderr)
        return 1
    try:
        changed = upsert(path, parse(sys.stdin.read()))
    except ValueError as exc:
        print(f"env_install: {exc}", file=sys.stderr)
        return 1
    print("instagram env: updated" if changed else "instagram env: unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
