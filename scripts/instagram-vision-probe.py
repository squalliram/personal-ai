"""One-off vision verification: isolated code, read-only inputs, verdicts only on stdout."""

import base64
import io
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

REMOTE = r"""
import io, os, pathlib, subprocess, sys, tarfile, tempfile
checkout = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix="vision-probe-", dir=pathlib.Path.home()) as directory:
    root = pathlib.Path(directory)
    code = root / "code"
    code.mkdir()
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:gz") as archive:
        archive.extractall(code, filter="data")
    result = subprocess.run([sys.executable, str(code / "scripts/instagram-vision-probe.py"), "verify", str(checkout), str(root)], cwd=code, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, timeout=1000)
    sys.stdout.buffer.write(result.stdout)
    if result.returncode:
        sys.stderr.write("Vision probe did not meet expectations; see verdict JSON.\n")
sys.exit(result.returncode)
"""


def verify(checkout, root):
    import hashlib
    import json
    import shutil
    import sqlite3
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from instagram_agent import image_review, render
    from instagram_agent.openai_client import AIError
    from octopus_energy.cli import load_env

    env = {**load_env(checkout / ".env"), **os.environ}
    ai = image_review.client(env)
    if ai is None:
        print(json.dumps({"error": "image review unavailable: no configured vision client"}), flush=True)
        return 1

    class Observed:
        text_model = ai.text_model

        def __init__(self):
            self.errors = []

        def chat_json(self, system, user, **kwargs):
            try:
                return ai.chat_json(system, user, **kwargs)
            except AIError as exc:
                self.errors.append(str(exc))
                raise

    observed = Observed()
    with sqlite3.connect((checkout / "data/instagram.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT draft_id,slides,reel FROM drafts WHERE draft_id=? AND kind='reel'", ("a7b7040197355203",)
        ).fetchone()
        assert row is not None, "the requested muffin draft is missing"
        slides, reel = json.loads(row["slides"]), json.loads(row["reel"])
        source = checkout / "data/instagram/drafts" / row["draft_id"]
        images = {}
        for index, label in ((3, "hand_slide"), (0, "clean_slide")):
            name = slides[index]["file"]
            assert name in (f"slide-{index + 1}.jpg", f"slide-{index + 1}.png")
            images[label] = source / name
        assert reel["file"] in ("reel.mp4", "reel.mov")
        original_reel = source / reel["file"]
        originals = [*images.values(), original_reel]
        assert all(path.is_file() and not path.is_symlink() for path in originals)
        hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in originals}
        result = {"draft_id": row["draft_id"], "provider": type(ai).__name__, "model": ai.text_model}
        for label, path in images.items():
            result[label] = image_review.check(observed, path.read_bytes())
        copied = root / "reel.mp4"
        shutil.copyfile(original_reel, copied)
        result["rendered_frames"] = image_review.frames(
            copied, reel["seconds"], observed, render.capabilities().get("ffmpeg"), render._run
        )
        result["source_files_unchanged"] = all(
            hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in hashes.items()
        )
        result["database_changes"] = conn.total_changes
        result["provider_errors"] = observed.errors
    print(json.dumps(result, indent=2), flush=True)
    hand = result["hand_slide"]
    success = hand["status"] == "failed" and any(
        "hand" in reason.lower() or "finger" in reason.lower() for reason in hand["reasons"]
    )
    success = (
        success and result["clean_slide"]["status"] == "passed" and result["rendered_frames"]["status"] != "unavailable"
    )
    return 0 if success and result["source_files_unchanged"] and result["database_changes"] == 0 else 1


def transport():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for name in ("instagram_agent", "octopus_energy", "notifications", "scripts/instagram-vision-probe.py"):
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
    subprocess.run(ssh, input=archive.getvalue(), check=True, timeout=1100)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        try:
            status = verify(Path(sys.argv[2]), Path(sys.argv[3]))
        except Exception as exc:
            import json

            print(json.dumps({"probe_error": type(exc).__name__}), flush=True)
            status = 1
        sys.exit(status)
    transport()
