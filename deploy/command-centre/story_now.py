"""One explicitly requested Story of the owner's already-published latte reel."""

import fcntl
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from instagram_agent import cli, publish
from octopus_energy.cli import load_env

DRAFT_ID = "4025e9735f1332fc"
REEL_MEDIA_ID = "18211634860370002"
UPLOAD_ID = "6ac173c47a5ec428"
MEDIA = Path("data/instagram/drafts")


class StoryClient:
    def __init__(self, client):
        self.client = client
        self.user_id = client.user_id
        self.attempted = False

    def get(self, path, params):
        return self.client.get(path, params)

    def post(self, path, params, **kwargs):
        if path == f"{self.user_id}/media":
            params = {"media_type": "STORIES", "video_url": params["video_url"]}
            return self.client.post(path, params, retry=False)
        if path == f"{self.user_id}/media_publish":
            if self.attempted:
                raise publish.PublishError("Story publish already attempted; never retry")
            self.attempted = True
            return self.client.post(path, params, retry=False)
        raise publish.PublishError("unexpected Instagram write refused")


def story(conn, env):
    row = conn.execute(
        "SELECT d.* FROM drafts d JOIN uploads u ON u.draft_id=d.draft_id "
        "WHERE d.draft_id=? AND u.upload_id=? AND u.reel=1",
        (DRAFT_ID, UPLOAD_ID),
    ).fetchone()
    if row is None or row["kind"] != "reel" or row["status"] != "published" or row["media_id"] != REEL_MEDIA_ID:
        raise publish.PublishError("expected already-published owner reel not found")
    reel = json.loads(row["reel"] or "{}")
    if reel.get("render") != "done" or reel.get("file") != "reel.mp4":
        raise publish.PublishError("expected rendered reel not found")
    if not publish.configured(env):
        raise publish.PublishError("publishing configuration not ready")
    client = StoryClient(cli.graph_client(env))
    try:
        media_id = publish.publish_reel(
            client, row, MEDIA, Path(env["INSTAGRAM_PUBLIC_DIR"]), env["INSTAGRAM_PUBLIC_URL"]
        )
    except Exception as exc:
        return {
            "outcome": "unknown" if client.attempted else "failed",
            "media_id": None,
            "permalink": None,
            "error": type(exc).__name__,
            "media_publish_attempted": client.attempted,
        }
    return {
        "outcome": "published",
        "media_id": media_id,
        "permalink": publish.permalink(client, media_id),
        "error": None,
        "media_publish_attempted": client.attempted,
    }


def main():
    result = {"phase": "story", "draft_id": DRAFT_ID}
    try:
        if sys.argv[1:] != ["story", DRAFT_ID]:
            raise publish.PublishError("unexpected phase or draft")
        env = {**load_env(Path(".env")), **os.environ}
        with (MEDIA / ".daily.lock").open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            conn = sqlite3.connect("file:data/instagram.sqlite3?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                result.update(story(conn, env))
            finally:
                conn.close()
    except Exception as exc:
        result.update(outcome="failed", media_id=None, permalink=None, error=type(exc).__name__)
    print(json.dumps(result))
    return 0 if result.get("outcome") == "published" else 1


if __name__ == "__main__":
    sys.exit(main())
