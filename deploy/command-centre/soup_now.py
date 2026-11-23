"""Owner-approved conversion of tonight's soup carousel to a reel and Story."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from instagram_agent import publish, render

SOURCE_ID = "a99a85b112d82e6b"
MEDIA = Path("data/instagram/drafts")


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


def main():
    result = {"phase": "stop", "source_draft_id": SOURCE_ID}
    try:
        conn = sqlite3.connect("file:data/instagram.sqlite3?mode=rw", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            result.update(stop(conn))
        finally:
            conn.close()
        result["outcome"] = "stopped"
    except Exception as exc:
        result.update(outcome="failed", error=type(exc).__name__)
    print(json.dumps(result))
    return 0 if result.get("outcome") == "stopped" else 1


if __name__ == "__main__":
    sys.exit(main())
