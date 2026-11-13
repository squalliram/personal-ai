"""Runner-side SSH transport. Never put the password hash or API keys in argv or output."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


def main():
    env = os.environ
    checkout, sha = env["CPANEL_APP_PATH"], env["DEPLOY_SHA"]
    port = env.get("CPANEL_SSH_PORT") or "22"
    host, user = env["CPANEL_SSH_HOST"], env["CPANEL_SSH_USERNAME"]
    for value, pattern in (
        (checkout, r"/[a-zA-Z0-9_./-]+"),
        (sha, r"[a-f0-9]{40}"),
        (port, r"[0-9]+"),
        (host, r"[a-zA-Z0-9][a-zA-Z0-9.-]*"),
        (user, r"[a-zA-Z0-9_][a-zA-Z0-9_-]*"),
    ):
        if not re.fullmatch(pattern, value):
            raise ValueError("Invalid SSH deployment configuration")
    script = Path(__file__).with_name("deploy_host.py").read_text()
    command = shlex.join(["/opt/alt/python312/bin/python3", "-c", script, checkout, sha])
    result = subprocess.run(
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
            port,
            f"{user}@{host}",
            command,
        ],
        input=json.dumps(
            {
                "password_hash": env["COMMAND_CENTRE_PASSWORD_HASH"],
                "openai_api_key": env.get("COMMAND_CENTRE_OPENAI_API_KEY", ""),
            }
        ).encode(),
        timeout=600,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
