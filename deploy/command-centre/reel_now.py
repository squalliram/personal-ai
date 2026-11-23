"""Owner-requested one-off photo reel; never select another upload or draft."""

import fcntl
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from instagram_agent import cli, drafts, publish, render
from octopus_energy.cli import load_env

UPLOAD_ID = "6ac173c47a5ec428"
PROTECTED_DRAFT = "a99a85b112d82e6b"
MEDIA = Path("data/instagram/drafts")


def selected(conn, draft_id):
    if not draft_id or draft_id == PROTECTED_DRAFT:
        raise ValueError("invalid selected draft")
    upload = conn.execute("SELECT * FROM uploads WHERE upload_id=?", (UPLOAD_ID,)).fetchone()
    if not upload or upload["draft_id"] != draft_id or not upload["reel"]:
        raise ValueError("draft is not from the selected reel upload")
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if not row or row["kind"] != "reel":
        raise ValueError("selected draft is not a reel")
    return row


def make(conn, env):
    upload = conn.execute("SELECT * FROM uploads WHERE upload_id=?", (UPLOAD_ID,)).fetchone()
    if not upload or upload["status"] != "waiting" or not upload["reel"]:
        raise ValueError("selected upload must be waiting and marked reel")
    as_reel = bool(upload["reel"]) and render.can_render("photos")
    if not as_reel:
        raise ValueError("photo reel rendering unavailable; refusing carousel fallback")
    ai = cli.model_client(env)
    if ai is None:
        raise ValueError("AI provider unavailable")
    skills = env.get("INSTAGRAM_SKILLS_DIR")
    rules = drafts.load_rules(Path(skills).expanduser() if skills else None)
    voice = cli._read(env.get("INSTAGRAM_VOICE_FILE", "agents/instagram-voice.md"))
    now = datetime.now(timezone.utc)
    draft = drafts.generate_from_upload(conn, ai, now, upload, MEDIA, rules=rules, voice=voice, as_reel=as_reel)
    if not draft or draft.get("kind") != "reel":
        raise ValueError("no usable reel generated")
    draft["reel"]["hold"] = True
    draft_id = drafts.store_upload_draft(conn, draft, upload, MEDIA, now)
    row = selected(conn, draft_id)
    reel = render.render(conn, MEDIA, draft_id, json.loads(row["reel"]))
    with conn:
        conn.execute("UPDATE drafts SET reel=? WHERE draft_id=?", (json.dumps(reel), draft_id))
    row = selected(conn, draft_id)
    return {
        "draft_id": draft_id, "kind": row["kind"], "status": row["status"],
        "scheduled_for": row["scheduled_for"], "topic": row["topic"], "caption": row["caption"],
        "hashtags_count": len(row["hashtags"].split()), "render": reel.get("render"),
        "file_size": (MEDIA / draft_id / "reel.mp4").stat().st_size, "seconds": reel.get("seconds"),
        "error": None,
    }


def send(conn, env, draft_id):
    row = selected(conn, draft_id)
    if row["status"] not in ("pending", "approved") or not publish.rendered(row):
        raise ValueError("selected reel is not publishable")
    if not publish.configured(env):
        raise ValueError("publishing configuration is not ready")
    if conn.execute("SELECT 1 FROM publish_runs WHERE draft_id=?", (draft_id,)).fetchone():
        raise ValueError("prior publish attempt exists; refusing retry")
    reel = json.loads(row["reel"])
    if reel.get("render") != "done" or not (MEDIA / draft_id / "reel.mp4").is_file():
        raise ValueError("rendered reel is missing")
    with conn:
        conn.execute("UPDATE drafts SET status='approved' WHERE draft_id=?", (draft_id,))
    row = selected(conn, draft_id)
    client = cli.graph_client(env)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sent = []

    def started():
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = selected(conn, draft_id)
            if not publish.will_post(latest["status"], latest["review_score"], publish.held(latest)):
                raise publish.Withdrawn("selected reel withdrawn")
            publish._record(conn, draft_id, stamp, "started")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        sent.append(True)

    try:
        media_id = publish.publish_reel(
            client, row, MEDIA, Path(env["INSTAGRAM_PUBLIC_DIR"]), env["INSTAGRAM_PUBLIC_URL"], on_publish=started
        )
    except Exception as exc:
        error = str(exc) if isinstance(exc, publish.PublishError) else type(exc).__name__
        if sent:
            error += "; outcome unknown, check Instagram; not retried"
        with conn:
            publish._record(conn, draft_id, stamp, "failed", error=error)
            if sent:
                conn.execute("UPDATE drafts SET status='failed' WHERE draft_id=?", (draft_id,))
        return {"draft_id": draft_id, "outcome": "failed", "error": error}
    with conn:
        publish._record(conn, draft_id, stamp, "published", media_id=media_id)
        conn.execute("UPDATE drafts SET status='published',media_id=? WHERE draft_id=?", (media_id, draft_id))
    return {"draft_id": draft_id, "outcome": "published", "media_id": media_id,
            "permalink": publish.permalink(client, media_id), "error": None}


def main():
    result = {"phase": sys.argv[1]}
    try:
        if sys.argv[1] not in ("draft", "publish"):
            raise ValueError("invalid phase")
        env = {**load_env(Path(".env")), **os.environ}
        with (MEDIA / ".daily.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            conn = sqlite3.connect("file:data/instagram.sqlite3?mode=rw", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                result.update(make(conn, env) if sys.argv[1] == "draft" else send(conn, env, sys.argv[2]))
            finally:
                conn.close()
    except Exception as exc:
        result["error"] = type(exc).__name__
    print(json.dumps(result))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
