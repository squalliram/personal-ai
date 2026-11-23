"""Owner-requested new latte reel and Story with licensed music; no existing draft writes."""

import base64
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from story_now import StoryClient

from instagram_agent import cli, music, publish, render, storage, video
from octopus_energy.cli import load_env

SOURCE_ID = "4025e9735f1332fc"
SOURCE_MEDIA = "18211634860370002"
NEW_ID = hashlib.sha256(b"owner-latte-music-2026-11-23-overcast-cafe").hexdigest()[:16]
MEDIA = Path("data/instagram/drafts")
TITLE = "Overcast Café — WSOS"
LICENCE = (
    "CC0 1.0 Universal (Public Domain Dedication). Copy, modify, distribute and perform, "
    "including commercial social-media videos; no attribution required. "
    "https://creativecommons.org/publicdomain/zero/1.0/"
)
SOURCE = "https://wsos.app/music/overcast-cafe"
TRACK_URL = "https://wsos.app/media/music/overcast-cafe.m4a"
TRACK_SHA256 = "9a88dd11a2dc364d4ae5b59721b130ca79b56c397704a329ba5861484e71d8fb"


def validate_track(data):
    if not 0 < len(data) <= music.MAX_TRACK_BYTES or music.kind(data) != "m4a":
        raise ValueError("invalid track format or size")
    if hashlib.sha256(data).hexdigest() != TRACK_SHA256:
        raise ValueError("track checksum mismatch")


def preflight(conn, env):
    if not publish.configured(env):
        raise ValueError("publishing configuration unavailable")
    public = Path(env["INSTAGRAM_PUBLIC_DIR"])
    index = public / "index.html"
    if public.is_symlink() or not public.is_dir() or index.is_symlink() or not index.is_file():
        raise ValueError("existing public directory and index.html required")
    if conn.execute("SELECT 1 FROM drafts WHERE draft_id=?", (NEW_ID,)).fetchone() or (MEDIA / NEW_ID).exists():
        raise ValueError("one-off draft already exists; refusing rerun")
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (SOURCE_ID,)).fetchone()
    if row is None or row["kind"] != "reel" or row["status"] != "published" or row["media_id"] != SOURCE_MEDIA:
        raise ValueError("expected published source reel missing")
    reel = json.loads(row["reel"] or "{}")
    if reel.get("source") != "photos" or not render.can_render("photos"):
        raise ValueError("photo source or renderer unavailable")
    folder = MEDIA / SOURCE_ID
    if folder.is_symlink():
        raise ValueError("unsafe source folder")
    for number in range(1, int(reel["photos"]) + 1):
        photo = folder / f"slide-{number}.jpg"
        if photo.is_symlink() or not photo.is_file():
            raise ValueError("source photo missing")
    return row, reel


def publish_new(conn, env, row, client, result):
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    attempted = []

    def started():
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = conn.execute("SELECT status FROM drafts WHERE draft_id=?", (NEW_ID,)).fetchone()
            if latest["status"] != "approved":
                raise publish.Withdrawn("new draft withdrawn")
            publish._record(conn, NEW_ID, stamp, "started")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        attempted.append(True)

    try:
        media_id = publish.publish_reel(
            client, row, MEDIA, Path(env["INSTAGRAM_PUBLIC_DIR"]), env["INSTAGRAM_PUBLIC_URL"], on_publish=started
        )
    except Exception as exc:
        with conn:
            publish._record(conn, NEW_ID, stamp, "failed", error=type(exc).__name__)
            conn.execute("UPDATE drafts SET status='failed' WHERE draft_id=?", (NEW_ID,))
        result["reel"] = {"outcome": "unknown" if attempted else "failed", "error": type(exc).__name__}
        raise
    with conn:
        publish._record(conn, NEW_ID, stamp, "published", media_id=media_id)
        conn.execute("UPDATE drafts SET status='published',media_id=? WHERE draft_id=?", (media_id, NEW_ID))
    result["reel"] = {
        "outcome": "published",
        "media_id": media_id,
        "permalink": publish.permalink(client, media_id),
        "error": None,
    }


def execute(conn, env, data, result):
    validate_track(data)
    source, reel = preflight(conn, env)
    now = datetime.now(timezone.utc)
    new_folder = MEDIA / NEW_ID
    new_folder.mkdir(mode=0o700)
    for number in range(1, int(reel["photos"]) + 1):
        name = f"slide-{number}.jpg"
        with (new_folder / name).open("xb") as target:
            target.write((MEDIA / SOURCE_ID / name).read_bytes())
    reel = {key: value for key, value in reel.items() if not key.startswith("render") and key not in ("file", "bytes")}
    reel.update(hold=True, render="pending")
    draft = dict(source)
    draft.update(
        draft_id=NEW_ID,
        scheduled_for=None,
        reel=reel,
        slides=json.loads(source["slides"]),
        hashtags=source["hashtags"].split(),
    )
    stamp = now.isoformat(timespec="seconds")
    with conn:
        storage.insert_draft(conn, draft, now.astimezone(cli.LONDON).date().isoformat(), stamp)
    result["draft_id"] = NEW_ID
    track_id = music.save(conn, MEDIA, TITLE, LICENCE, SOURCE, data, now)
    pick = music.choice(track_id, "replace", conn)
    reel["music"] = {"track_id": pick["track_id"], "mix": pick["mix"], "title": TITLE, "licence": LICENCE}
    result["track"] = {
        "track_id": track_id,
        "title": TITLE,
        "licence": LICENCE,
        "source": SOURCE,
        "download_url": TRACK_URL,
        "sha256": TRACK_SHA256,
        "bytes": len(data),
    }
    with conn:
        conn.execute("UPDATE drafts SET reel=? WHERE draft_id=?", (json.dumps(reel), NEW_ID))
    reel = render.render(conn, MEDIA, NEW_ID, reel)
    facts, problems = video.check(new_folder / "reel.mp4")
    if problems or facts.get("audio_codec") != "aac":
        raise ValueError("rendered video lacks valid AAC audio")
    result["render"] = {
        "state": reel["render"],
        "bytes": facts["bytes"],
        "seconds": facts["seconds"],
        "audio_codec": facts["audio_codec"],
        "mix": "replace",
    }
    with conn:
        conn.execute("UPDATE drafts SET reel=?,status='approved' WHERE draft_id=?", (json.dumps(reel), NEW_ID))
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (NEW_ID,)).fetchone()
    client = cli.graph_client(env)
    publish_new(conn, env, row, client, result)
    story_client = StoryClient(client)
    try:
        media_id = publish.publish_reel(
            story_client, row, MEDIA, Path(env["INSTAGRAM_PUBLIC_DIR"]), env["INSTAGRAM_PUBLIC_URL"]
        )
    except Exception as exc:
        result["story"] = {"outcome": "unknown" if story_client.attempted else "failed", "error": type(exc).__name__}
        raise
    result["story"] = {
        "outcome": "published",
        "media_id": media_id,
        "permalink": publish.permalink(client, media_id),
        "error": None,
    }
    result.update(outcome="published", error=None)


def main():
    result = {"phase": "music_reel_and_story", "source_draft_id": SOURCE_ID}
    try:
        data = base64.b64decode(os.environ.pop("OWNER_TRACK_BASE64"), validate=True)
        env = {**load_env(Path(".env")), **os.environ}
        with (MEDIA / ".daily.lock").open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            conn = sqlite3.connect("file:data/instagram.sqlite3?mode=rw", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                execute(conn, env, data, result)
            finally:
                conn.close()
    except Exception as exc:
        result.update(outcome="failed", error=type(exc).__name__)
    print(json.dumps(result))
    return 0 if result.get("outcome") == "published" else 1


if __name__ == "__main__":
    sys.exit(main())
