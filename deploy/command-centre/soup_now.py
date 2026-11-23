"""Owner-approved conversion of tonight's soup carousel to a reel and Story."""

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from instagram_agent import cli, music, publish, render, storage, video
from octopus_energy.cli import load_env

SOURCE_ID = "a99a85b112d82e6b"
MEDIA = Path("data/instagram/drafts")
NEW_ID = hashlib.sha256(b"owner-approved-soup-reel-story-2026-11-23").hexdigest()[:16]
TRACK_ID = "3883c68e84a5d709"


class OnceClient:
    def __init__(self, client, story=False):
        self.client, self.story = client, story
        self.user_id = client.user_id
        self.attempted = False

    def get(self, path, params):
        return self.client.get(path, params)

    def post(self, path, params, **kwargs):
        if path == f"{self.user_id}/media":
            if self.story:
                params = {"media_type": "STORIES", "video_url": params["video_url"]}
        elif path == f"{self.user_id}/media_publish":
            if self.attempted:
                raise publish.PublishError("media_publish already attempted; never retry")
            self.attempted = True
        else:
            raise publish.PublishError("unexpected Instagram write refused")
        return self.client.post(path, params, retry=False)


def stop(conn):
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (SOURCE_ID,)).fetchone()
    if row is None or publish.kind(row) != "carousel" or row["status"] == "published":
        conn.rollback()
        raise ValueError("expected unpublished carousel missing")
    conn.execute("UPDATE drafts SET status='rejected' WHERE draft_id=?", (SOURCE_ID,))
    conn.commit()
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (SOURCE_ID,)).fetchone()
    skipped = not publish.will_post(row["status"], row["review_score"], publish.held(row))
    now = datetime.now(timezone.utc)
    absent = SOURCE_ID not in {r["draft_id"] for r in publish.due(conn, now)}
    slides = json.loads(row["slides"])
    files = []
    for slide in slides:
        name = slide.get("file", "")
        path = MEDIA / SOURCE_ID / name
        files.append({"file": name, "present": bool(name) and path.is_file() and not path.is_symlink()})
    return {
        "carousel_status": row["status"],
        "publisher_skips": skipped,
        "absent_from_due": absent,
        "topic": row["topic"],
        "slides": files,
        "can_render_photos": render.can_render("photos"),
    }


def preflight(conn, env):
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (SOURCE_ID,)).fetchone()
    if row is None or publish.kind(row) != "carousel" or row["status"] != "rejected":
        raise ValueError("source carousel must already be rejected")
    if conn.execute("SELECT 1 FROM drafts WHERE draft_id=?", (NEW_ID,)).fetchone() or (MEDIA / NEW_ID).exists():
        raise ValueError("replacement already exists; refusing rerun")
    if not publish.configured(env) or not render.can_render("photos"):
        raise ValueError("publishing or rendering configuration unavailable")
    public = Path(env["INSTAGRAM_PUBLIC_DIR"])
    index = public / "index.html"
    if public.is_symlink() or not public.is_dir() or index.is_symlink() or not index.is_file():
        raise ValueError("existing public directory and index.html required")
    track = next((t for t in music.tracks(conn) if t["track_id"] == TRACK_ID), None)
    if track is None or music.usable_file(conn, MEDIA, TRACK_ID) is None:
        raise ValueError("licensed Overcast Cafe track unavailable")
    slides = json.loads(row["slides"])
    if not isinstance(slides, list) or not 1 <= len(slides) <= 10 or (MEDIA / SOURCE_ID).is_symlink():
        raise ValueError("unsafe or unavailable source slides")
    for number, slide in enumerate(slides, 1):
        if slide.get("file") != f"slide-{number}.jpg":
            raise ValueError("unexpected slide filename")
        path = MEDIA / SOURCE_ID / slide["file"]
        if path.is_symlink() or not path.is_file() or path.read_bytes()[:3] != b"\xff\xd8\xff":
            raise ValueError("source slide is not a stored JPEG")
    return row, slides, track


def execute(conn, env, result):
    source, slides, track = preflight(conn, env)
    folder = MEDIA / NEW_ID
    folder.mkdir(mode=0o700)
    for slide in slides:
        with (folder / slide["file"]).open("xb") as target, (MEDIA / SOURCE_ID / slide["file"]).open("rb") as original:
            shutil.copyfileobj(original, target)
    seconds = render.photo_seconds(len(slides)) * len(slides)
    reel = {
        "source": "photos",
        "source_draft_id": SOURCE_ID,
        "photos": len(slides),
        "seconds": seconds,
        "hook": source["topic"],
        "on_screen": [],
        "cover_ms": 0,
        "hold": True,
        "render": "pending",
        "music": {"track_id": TRACK_ID, "mix": "replace", "title": track["title"], "licence": track["licence"]},
    }
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    draft = dict(source)
    draft.update(
        draft_id=NEW_ID, kind="reel", reel=reel, slides=slides, hashtags=source["hashtags"].split(), scheduled_for=None
    )
    with conn:
        storage.insert_draft(conn, draft, now.astimezone(cli.LONDON).date().isoformat(), stamp)
    result.update(draft_id=NEW_ID, track={k: track[k] for k in ("track_id", "title", "licence", "source")})
    reel = render.render(conn, MEDIA, NEW_ID, reel)
    facts, problems = video.check(folder / "reel.mp4")
    if problems or facts.get("audio_codec") != "aac":
        raise ValueError("rendered reel must have valid AAC audio")
    result["render"] = {
        "state": reel["render"],
        "seconds": facts["seconds"],
        "bytes": facts["bytes"],
        "audio_codec": facts["audio_codec"],
        "mix": "replace",
    }
    with conn:
        conn.execute("UPDATE drafts SET reel=?,status='approved' WHERE draft_id=?", (json.dumps(reel), NEW_ID))
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (NEW_ID,)).fetchone()
    client = cli.graph_client(env)
    client.retries = 0
    reel_client = OnceClient(client)

    def started():
        conn.execute("BEGIN IMMEDIATE")
        try:
            status = conn.execute("SELECT status FROM drafts WHERE draft_id=?", (NEW_ID,)).fetchone()[0]
            if status != "approved":
                raise publish.Withdrawn("replacement withdrawn before posting")
            publish._record(conn, NEW_ID, stamp, "started")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    try:
        media_id = publish.publish_reel(
            reel_client, row, MEDIA, Path(env["INSTAGRAM_PUBLIC_DIR"]), env["INSTAGRAM_PUBLIC_URL"], on_publish=started
        )
    except Exception as exc:
        with conn:
            publish._record(conn, NEW_ID, stamp, "failed", error=type(exc).__name__)
            conn.execute("UPDATE drafts SET status='failed' WHERE draft_id=?", (NEW_ID,))
        result["reel"] = {"outcome": "unknown" if reel_client.attempted else "failed", "error": type(exc).__name__}
        raise
    with conn:
        publish._record(conn, NEW_ID, stamp, "published", media_id=media_id)
        conn.execute("UPDATE drafts SET status='published',media_id=? WHERE draft_id=?", (media_id, NEW_ID))
    result["reel"] = {"outcome": "published", "media_id": media_id, "permalink": publish.permalink(client, media_id)}
    print(json.dumps({"checkpoint": "reel_published", **result}), flush=True)
    story_client = OnceClient(client, story=True)
    try:
        story_id = publish.publish_reel(
            story_client, row, MEDIA, Path(env["INSTAGRAM_PUBLIC_DIR"]), env["INSTAGRAM_PUBLIC_URL"]
        )
    except Exception as exc:
        result["story"] = {"outcome": "unknown" if story_client.attempted else "failed", "error": type(exc).__name__}
        raise
    result["story"] = {"outcome": "published", "media_id": story_id, "permalink": publish.permalink(client, story_id)}
    reel["story"] = result["story"]
    with conn:
        conn.execute("UPDATE drafts SET reel=? WHERE draft_id=?", (json.dumps(reel), NEW_ID))
    result["outcome"] = "published"


def main():
    phase = sys.argv[1] if len(sys.argv) == 2 else "stop"
    result = {"phase": phase, "source_draft_id": SOURCE_ID}
    try:
        conn = sqlite3.connect("file:data/instagram.sqlite3?mode=rw", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if phase == "stop":
                result.update(stop(conn))
                result["outcome"] = "stopped"
            elif phase == "publish":
                env = {**load_env(Path(".env")), **os.environ}
                with (MEDIA / ".daily.lock").open("r") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    execute(conn, env, result)
                row = conn.execute("SELECT status,review_score FROM drafts WHERE draft_id=?", (SOURCE_ID,)).fetchone()
                result.update(
                    carousel_status=row["status"],
                    publisher_skips=not publish.will_post(row["status"], row["review_score"]),
                )
            else:
                raise ValueError("unexpected phase")
        finally:
            conn.close()
    except Exception as exc:
        result.update(outcome="failed", error=type(exc).__name__)
    print(json.dumps(result))
    return 0 if result.get("outcome") in ("stopped", "published") else 1


if __name__ == "__main__":
    sys.exit(main())
