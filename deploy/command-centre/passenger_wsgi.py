"""Passenger entry point for the hosted Command Centre (cPanel "Setup Python App").

Deployed by .github/workflows/deploy-command-centre.yml into the app directory,
which lives outside the git checkout and outside the web root. Settings come
from command-centre.env beside this file (written by the deploy workflow with
mode 0600); only COMMAND_CENTRE_* keys are read, matching config.py. The
personal-ai checkout supplies the code; Apache terminates TLS, so the app is
built with secure=True and redirects any plain-HTTP request to HTTPS.
"""

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

try:
    lines = (APP_DIR / "command-centre.env").read_text(encoding="utf-8").splitlines()
except FileNotFoundError:
    raise RuntimeError("command-centre.env is missing; run the deploy-command-centre workflow") from None

for line in lines:
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("COMMAND_CENTRE_"):
            os.environ[key] = value.strip().strip("\"'")

checkout = os.environ.get("COMMAND_CENTRE_CHECKOUT")
if not checkout:
    raise RuntimeError("COMMAND_CENTRE_CHECKOUT is not set in command-centre.env")
sys.path.insert(0, checkout)

from command_centre.app import CommandCentreApp  # noqa: E402
from command_centre.config import load_settings  # noqa: E402

settings = load_settings(Path(checkout))
hosts = [host.strip() for host in os.environ.get("COMMAND_CENTRE_ALLOWED_HOSTS", "").split(",") if host.strip()]
if not hosts:
    raise RuntimeError("COMMAND_CENTRE_ALLOWED_HOSTS is not set in command-centre.env")
application = CommandCentreApp(settings, allowed_hosts=hosts, secure=True)
