"""Owner-approved three-track music import, with checksums and no Instagram calls."""

import base64
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

TRACKS = (
    ("morning-lap", "Morning Lap", "fed494b2c6894dad359a05130316d107fe616df28d68d68c6d159b6a03e38591"),
    ("back-pocket", "Back Pocket", "2b657df670c3a5986f99f1d87e11fd12a32fabdfeaa0ac245e3eeb8c565e652b"),
    ("afterhours-atlas", "Afterhours Atlas", "a12732e550e52233a4e0a2cd3ed206055f0c0fa3412f36af3310e9565c9abfcb"),
)
REMOTE = r"""
import base64, hashlib, json, pathlib, sqlite3, sys
from datetime import datetime, timezone
checkout = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(checkout))
from instagram_agent import music
media = checkout / "data/instagram/drafts"
database = (checkout / "data/instagram.sqlite3").resolve().as_uri()
tracks = json.load(sys.stdin)
assert len(tracks) == 3
for track in tracks:
    data = base64.b64decode(track["data"], validate=True)
    assert hashlib.sha256(data).hexdigest() == track["sha256"]
    assert 0 < len(data) <= music.MAX_TRACK_BYTES and music.kind(data) == "m4a"
added = []
with sqlite3.connect(database + "?mode=rw", uri=True) as conn:
    conn.row_factory = sqlite3.Row
    conn.execute("SELECT track_id,title,source,licence,ext,bytes FROM music LIMIT 1").fetchall()
    music.for_reel(conn, media)
    for track in tracks:
        data = base64.b64decode(track["data"], validate=True)
        existing = conn.execute("SELECT track_id FROM music WHERE source=?", (track["source"],)).fetchall()
        assert len(existing) <= 1
        if existing:
            track_id = existing[0]["track_id"]
        else:
            track_id = music.save(conn, media, track["title"], "CC0 1.0 Universal (public domain dedication); commercial use, no attribution", track["source"], data, datetime.now(timezone.utc))
        path = music.usable_file(conn, media, track_id)
        assert path and hashlib.sha256(path.read_bytes()).hexdigest() == track["sha256"]
        added.append({"track_id":track_id,"title":track["title"],"sha256":track["sha256"]})
with sqlite3.connect(database + "?mode=ro", uri=True) as conn:
    usable = music.for_reel(conn, media)
    expected = {"3883c68e84a5d709", *[t["track_id"] for t in added]}
    assert expected <= {t["track_id"] for t in usable}
    print(json.dumps({"added_or_verified":added,"usable_tracks":[{"track_id":t["track_id"],"title":t["title"]} for t in usable],"all_four_usable":True}))
"""


def main():
    tracks = []
    for slug, title, digest in TRACKS:
        data = subprocess.check_output(
            [
                "curl",
                "-fsSL",
                "--max-time",
                "45",
                "--max-filesize",
                "20971520",
                f"https://wsos.app/media/music/{slug}.m4a",
            ]
        )
        assert hashlib.sha256(data).hexdigest() == digest
        tracks.append(
            {
                "title": title,
                "source": f"https://wsos.app/music/{slug}",
                "sha256": digest,
                "data": base64.b64encode(data).decode(),
            }
        )
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
        input=json.dumps(tracks).encode(),
        check=True,
        timeout=180,
    )


if __name__ == "__main__":
    main()
