"""Read the accounts @instafoodielove follows from Instagram's "Download your information" export."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def _username(entry: dict) -> str | None:
    for item in entry.get("string_list_data") or []:
        if USERNAME_RE.match(item.get("value") or ""):
            return item["value"].lower()
        parts = urlsplit(item.get("href") or "")
        if parts.hostname in {"instagram.com", "www.instagram.com"}:
            path = [segment for segment in parts.path.split("/") if segment]
            name = path[1] if path[:1] == ["_u"] and len(path) > 1 else path[0] if path else ""
            if USERNAME_RE.match(name):
                return name.lower()
    title = entry.get("title") or ""
    return title.lower() if USERNAME_RE.match(title) else None


def usernames(path: Path) -> list[str]:
    """Usernames from following.json, in either of the layouts Instagram has used."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("relationships_following", []) if isinstance(data, dict) else data
    found = []
    for entry in entries if isinstance(entries, list) else []:
        name = _username(entry) if isinstance(entry, dict) else None
        if name and name not in found:
            found.append(name)
    return found
