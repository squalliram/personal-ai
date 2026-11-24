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
    from instagram_agent import cloudflare_client, openai_client, render

    with sqlite3.connect((checkout / "data/instagram.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT draft_id,topic,slides,reel,created_on FROM drafts WHERE kind='reel' "
            "AND NOT EXISTS (SELECT 1 FROM uploads WHERE uploads.draft_id=drafts.draft_id) "
            "ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        selected = next(
            row
            for row in rows
            if json.loads(row["reel"] or "{}").get("source") == "photos" and 3 <= len(json.loads(row["slides"])) <= 8
        )
        reel = json.loads(selected["reel"])
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
    caps = render.capabilities()
    assert render.can_render("photos", caps)
    argv, seconds = render.command(output, reel, output, caps, track)
    result = subprocess.run(argv, capture_output=True, timeout=800)
    if result.returncode:
        raise RuntimeError("ffmpeg: " + result.stderr.decode()[-2500:])
    for name, second in (("f1.jpg", "1"), ("f2.jpg", "4"), ("f3.jpg", "7.5")):
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
    allowed = {"INSTAGRAM_AI_PROVIDER", "CLOUDFLARE_IMAGE_MODEL", "OPENAI_IMAGE_MODEL"}
    settings, present = {}, set()
    for line in (checkout / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            key = key.strip()
            if value.strip().strip("\"'"):
                present.add(key)
                if key in allowed:
                    settings[key] = value.strip().strip("\"'")
    provider = settings.get("INSTAGRAM_AI_PROVIDER") or (
        "cloudflare" if {"CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"} <= present else "openai"
    )
    model = (
        settings.get("CLOUDFLARE_IMAGE_MODEL", cloudflare_client.IMAGE_MODEL)
        if provider == "cloudflare"
        else settings.get("OPENAI_IMAGE_MODEL", openai_client.IMAGE_MODEL)
    )
    metadata = {
        "draft_id": selected["draft_id"],
        "topic": selected["topic"],
        "created_on": selected["created_on"],
        "seconds": seconds,
        "caps": caps,
        "provider_current_config": provider,
        "image_model_current_config": model,
        "model_env_names_present": sorted(allowed & present),
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
