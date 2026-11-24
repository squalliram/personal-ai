"""Owner-requested operational state check; no Instagram calls."""

import base64
import os
import shlex
import subprocess
from pathlib import Path

REMOTE = r"""
import json, pathlib, sqlite3, sys
from datetime import datetime, timezone
checkout = pathlib.Path(sys.argv[1])
with sqlite3.connect((checkout / "data/instagram.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as conn:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status,scheduled_for,reel,media_id FROM drafts WHERE draft_id=?", ("a7b7040197355203",)).fetchone()
    assert row is not None
    reel = json.loads(row["reel"] or "{}")
    print(json.dumps({"draft_id":"a7b7040197355203", "status":row["status"], "scheduled_for":row["scheduled_for"], "media_id":row["media_id"], "render":reel.get("render"), "photos":reel.get("photos"), "utc_now":datetime.now(timezone.utc).isoformat()}))
"""


def main():
    encoded = base64.b64encode(REMOTE.encode()).decode()
    command = shlex.join(
        [
            "/opt/alt/python312/bin/python3",
            "-c",
            f"import base64;exec(base64.b64decode('{encoded}'))",
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
        timeout=60,
    )


if __name__ == "__main__":
    main()
