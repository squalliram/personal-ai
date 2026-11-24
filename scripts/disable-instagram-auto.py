"""One-off owner-authorized kill switch; no Instagram calls."""

import base64
import os
import shlex
import subprocess
from pathlib import Path

REMOTE = r"""
import fcntl, json, os, pathlib, signal, sqlite3, subprocess, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
checkout = pathlib.Path(sys.argv[1]).resolve()
os.chdir(checkout)
sys.path.insert(0, str(checkout))
from instagram_agent import env_install, publish
from octopus_energy.cli import load_env
env_file = checkout / ".env"
assert env_file.is_file() and not env_file.is_symlink()
before = load_env(env_file).get("INSTAGRAM_AUTO_PUBLISH", "unset")
env_install.upsert(env_file, {"INSTAGRAM_AUTO_PUBLISH": "0"})
print(json.dumps({"action": "server_env_disabled", "before": before, "after": "0"}), flush=True)
signal.alarm(180)
with open(checkout / ".git/energy-dashboard.deploy.lock", "a") as deploy_lock:
    fcntl.flock(deploy_lock, fcntl.LOCK_EX)
    with open(checkout / "data/instagram/drafts/.daily.lock", "a") as daily_lock:
        fcntl.flock(daily_lock, fcntl.LOCK_EX)
        signal.alarm(0)
        env_install.upsert(env_file, {"INSTAGRAM_AUTO_PUBLISH": "0"})
        now = datetime.now(timezone.utc)
        database = checkout / "data/instagram.sqlite3"
        assert database.is_file()
        with sqlite3.connect(database) as conn:
            publish.set_state(conn, "off", now)
        env = {**load_env(env_file), **os.environ}
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        assert cron.returncode in (0, 1)
        cron_switch = [line.split("=", 1)[1].strip().strip("\"'") for line in cron.stdout.splitlines() if line.startswith("INSTAGRAM_AUTO_PUBLISH=")]
        assert all(value != "1" for value in cron_switch), "cron environment overrides switch"
        assert env.get("INSTAGRAM_AUTO_PUBLISH") == "0"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            state = conn.execute("SELECT value FROM publisher_state WHERE key='auto_publish'").fetchone()[0]
            raw_due = [row["draft_id"] for row in publish.due(conn, now)]
            gated_due = raw_due if publish.configured(env) else []
            rows = conn.execute("SELECT draft_id,status,scheduled_for FROM drafts WHERE status IN ('pending','approved') AND scheduled_for IS NOT NULL ORDER BY scheduled_for").fetchall()
            upcoming = []
            for row in rows:
                scheduled = datetime.fromisoformat(row["scheduled_for"].replace("Z", "+00:00"))
                if now <= scheduled <= now + timedelta(days=7):
                    upcoming.append({**dict(row), "scheduled_for_london": scheduled.astimezone(ZoneInfo("Europe/London")).isoformat()})
            assert state == "off" and not publish.ready(env) and not gated_due
            print(json.dumps({"verified_at_london": now.astimezone(ZoneInfo("Europe/London")).isoformat(), "env_switch": env["INSTAGRAM_AUTO_PUBLISH"], "publisher_state": state, "publisher_ready": publish.ready(env), "cron_env_override": cron_switch, "inflight_daily_drained": True, "raw_due_ids": raw_due, "effective_publish_due_ids": gated_due, "pending_approved_next_7_days": upcoming}), flush=True)
"""


command = shlex.join(
    [
        "/opt/alt/python312/bin/python3",
        "-c",
        "import base64;exec(base64.b64decode(" + repr(base64.b64encode(REMOTE.encode()).decode()) + "))",
        os.environ["CPANEL_APP_PATH"],
    ]
)
subprocess.run(
    [
        "ssh",
        "-i",
        str(Path.home() / ".ssh/id_ed25519"),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=20",
        "-p",
        os.environ.get("CPANEL_SSH_PORT") or "22",
        os.environ["CPANEL_SSH_USERNAME"] + "@" + os.environ["CPANEL_SSH_HOST"],
        command,
    ],
    check=True,
    timeout=240,
)
