"""Command centre settings. Only COMMAND_CENTRE_* values are read; other secrets in .env are ignored."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .security import Permission

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "COMMAND_CENTRE_"


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return {}
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith(PREFIX):
            values[key] = value.strip().strip("\"'")
    return values


@dataclass(frozen=True)
class Settings:
    root: Path
    energy_db: Path
    energy_status: Path
    briefing_db: Path
    briefing_reports: Path
    audit_db: Path
    password_hash: str | None
    allowed_hosts: tuple[str, ...] = ()
    sessions_db: Path | None = None
    permission_ceiling: Permission = Permission.RECOMMEND
    inspect_crontab: bool = True
    energy_stale_hours: int = 48
    session_idle_seconds: int = 30 * 60
    session_absolute_seconds: int = 12 * 60 * 60
    session_remember_seconds: int = 30 * 24 * 60 * 60
    instagram_db: Path | None = None
    instagram_media: Path | None = None
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str | None = None


def load_settings(root: Path = ROOT, environ: Mapping[str, str] | None = None) -> Settings:
    environ = os.environ if environ is None else environ
    values = {**_read_env_file(root / ".env"), **{k: v for k, v in environ.items() if k.startswith(PREFIX)}}

    def path(name: str, default: str) -> Path:
        value = Path(values.get(PREFIX + name) or default).expanduser()
        return value if value.is_absolute() else root / value

    def optional_path(name: str) -> Path | None:
        value = values.get(PREFIX + name)
        if not value:
            return None
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else root / candidate

    return Settings(
        root=root,
        energy_db=path("ENERGY_DB", "data/octopus_energy.sqlite3"),
        energy_status=path("ENERGY_STATUS", "reports/energy/dashboard/status.json"),
        briefing_db=path("BRIEFING_DB", "data/daily_briefing.sqlite3"),
        briefing_reports=path("BRIEFING_REPORTS", "reports/daily"),
        audit_db=path("AUDIT_DB", "data/command_centre.sqlite3"),
        password_hash=values.get(PREFIX + "PASSWORD_HASH") or None,
        allowed_hosts=tuple(
            host.strip() for host in values.get(PREFIX + "ALLOWED_HOSTS", "").split(",") if host.strip()
        ),
        sessions_db=optional_path("SESSIONS_DB"),
        permission_ceiling=Permission.parse(values.get(PREFIX + "PERMISSION", "recommend")),
        inspect_crontab=values.get(PREFIX + "INSPECT_CRONTAB", "1").lower() not in ("0", "false", "no"),
        instagram_db=path("INSTAGRAM_DB", "data/instagram.sqlite3"),
        instagram_media=path("INSTAGRAM_MEDIA", "data/instagram/drafts"),
        openai_api_key=(values.get(PREFIX + "OPENAI_API_KEY") or "").strip() or None,
        openai_model=(values.get(PREFIX + "OPENAI_MODEL") or "").strip() or None,
    )
