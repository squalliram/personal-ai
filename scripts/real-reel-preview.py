"""One-off preview transport: isolated branch code, read-only production inputs, encrypted output."""

import base64
import io
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

REMOTE = r"""
import io, pathlib, subprocess, sys, tarfile, tempfile
checkout = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix="reel-preview-", dir=pathlib.Path.home()) as directory:
    root = pathlib.Path(directory)
    code = root / "code"
    code.mkdir()
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:gz") as archive:
        archive.extractall(code, filter="data")
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(root / "venv")], check=True, stdout=subprocess.DEVNULL)
    result = subprocess.run([str(root / "venv/bin/python"), str(code / "scripts/real-reel-preview.py"), "render", str(checkout), str(root)], cwd=code, capture_output=True, timeout=900)
    if result.returncode:
        raise RuntimeError("Preview render failed: " + result.stderr.decode()[-3000:])
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
        archive.add(root / "output", arcname="preview")
"""


def render_preview(checkout, root):
    import hashlib
    import json
    import shutil
    import sqlite3
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from instagram_agent import music as library
    from instagram_agent import render

    with sqlite3.connect((checkout / "data/instagram.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        selected = conn.execute(
            "SELECT draft_id,topic,slides,reel,created_on,status FROM drafts WHERE draft_id=? AND kind='reel'",
            ("a7b7040197355203",),
        ).fetchone()
        assert selected and selected["status"] == "rejected"
        reel = json.loads(selected["reel"])
        assert reel["source"] == "photos"
        reel["framing"] = "cover"
        reel["music"] = {"track_id": "1e2c3695319c705c", "mix": "replace"}
        usable_tracks = [
            {"track_id": t["track_id"], "title": t["title"]}
            for t in library.for_reel(conn, checkout / "data/instagram/drafts")
        ]
        slides = json.loads(selected["slides"])
        assert len(slides) == reel["photos"] and 1 <= len(slides) <= 10
        source = checkout / "data/instagram/drafts" / selected["draft_id"]
        output = root / "output"
        output.mkdir()
        originals = {}
        for number, slide in enumerate(slides, 1):
            name = slide["file"]
            assert name in (f"slide-{number}.jpg", f"slide-{number}.png")
            path = source / name
            assert path.is_file() and not path.is_symlink()
            originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            shutil.copyfile(path, output / name)
        track = None
        choice = reel.get("music") or {}
        if choice.get("track_id"):
            music = conn.execute("SELECT ext,licence FROM music WHERE track_id=?", (choice["track_id"],)).fetchone()
            assert music and music["licence"].strip()
            track_source = checkout / "data/instagram/drafts/music" / choice["track_id"] / ("track." + music["ext"])
            assert track_source.is_file() and not track_source.is_symlink()
            track = root / ("music." + music["ext"])
            shutil.copyfile(track_source, track)
            assert (
                hashlib.sha256(track.read_bytes()).hexdigest()
                == "fed494b2c6894dad359a05130316d107fe616df28d68d68c6d159b6a03e38591"
            )
    caps = render.capabilities()
    assert render.can_render("photos", caps)
    argv, seconds = render.command(output, reel, output, caps, track)
    result = subprocess.run(argv, capture_output=True, timeout=800)
    if result.returncode:
        raise RuntimeError("ffmpeg: " + result.stderr.decode()[-2500:])
    for name, second in (("f1.jpg", "1"), ("f2.jpg", "2.9"), ("f3.jpg", "7.5")):
        subprocess.run(
            [
                caps["ffmpeg"],
                "-loglevel",
                "error",
                "-ss",
                second,
                "-i",
                str(output / "reel.mp4"),
                "-frames:v",
                "1",
                str(output / name),
            ],
            check=True,
            capture_output=True,
        )
    metadata = {
        "draft_id": selected["draft_id"],
        "topic": selected["topic"],
        "created_on": selected["created_on"],
        "seconds": seconds,
        "caps": caps,
        "source_status": selected["status"],
        "preview_framing": reel["framing"],
        "preview_music": reel["music"],
        "usable_tracks": usable_tracks,
        "original_slide_hashes_unchanged": all(
            hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items()
        ),
        "music_included": track is not None,
    }
    assert metadata["original_slide_hashes_unchanged"]
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2))


def transport():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for name in ("instagram_agent", "scripts/real-reel-preview.py"):
            bundle.add(name, filter=lambda info: None if "__pycache__" in info.name else info)
    encoded = base64.b64encode(REMOTE.encode()).decode()
    command = shlex.join(
        [
            "/opt/alt/python312/bin/python3",
            "-c",
            f"import base64;exec(base64.b64decode('{encoded}'))",
            os.environ["CPANEL_APP_PATH"],
        ]
    )
    ssh = [
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
    ]
    with subprocess.Popen(
        ["age", "-r", os.environ["PREVIEW_AGE_RECIPIENT"], "-o", os.environ["PREVIEW_OUTPUT"]], stdin=subprocess.PIPE
    ) as encrypt:
        subprocess.run(ssh, input=archive.getvalue(), stdout=encrypt.stdin, check=True, timeout=1100)
        encrypt.stdin.close()
        assert encrypt.wait() == 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "render":
        render_preview(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        transport()
