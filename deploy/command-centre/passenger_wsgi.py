"""Passenger entry point for the hosted Command Centre (cPanel "Setup Python App").

Deployed by .github/workflows/deploy-command-centre.yml into the app directory,
which lives outside the git checkout and outside the web root. Settings come
from command-centre.env beside this file (written by the deploy workflow with
mode 0600); only COMMAND_CENTRE_* keys are read, matching config.py. The
versioned release supplies the code; LiteSpeed terminates TLS, so the app
requires connection TLS metadata and redirects plain-HTTP requests to HTTPS.
"""

import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

try:
    lines = (APP_DIR / "command-centre.env").read_text(encoding="utf-8").splitlines()
except FileNotFoundError:
    raise RuntimeError("command-centre.env is missing; run the deploy-command-centre workflow") from None

values = {
    "COMMAND_CENTRE_PASSWORD_HASH": "",
    "COMMAND_CENTRE_SESSIONS_DB": "",
    "COMMAND_CENTRE_ALLOWED_HOSTS": "",
}
for line in lines:
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("COMMAND_CENTRE_"):
            values[key] = value.strip().strip("\"'")

checkout = values.get("COMMAND_CENTRE_CHECKOUT")
if not checkout:
    raise RuntimeError("COMMAND_CENTRE_CHECKOUT is not set in command-centre.env")
sys.path.insert(0, checkout)

from command_centre.app import CommandCentreApp  # noqa: E402
from command_centre.config import load_settings  # noqa: E402
from command_centre.security import Permission, valid_password_hash  # noqa: E402

settings = load_settings(Path(checkout), environ=values)
hosts = settings.allowed_hosts
if not hosts:
    raise RuntimeError("COMMAND_CENTRE_ALLOWED_HOSTS is not set in command-centre.env")
if not valid_password_hash(settings.password_hash):
    raise RuntimeError("A valid COMMAND_CENTRE_PASSWORD_HASH is required")
if settings.permission_ceiling > Permission.RECOMMEND or not settings.sessions_db:
    raise RuntimeError("Hosted settings require shared sessions and a read/recommend permission ceiling")
release_sha = values.get("COMMAND_CENTRE_RELEASE_SHA", "")
if release_sha and not re.fullmatch(r"[a-f0-9]{40}", release_sha):
    raise RuntimeError("Invalid release identity")
app = CommandCentreApp(settings, allowed_hosts=hosts, secure=True)


def application(environ, start_response):
    # This host's adapter can derive scheme AND port from forwarded headers.
    # LiteSpeed supplies SSL_PROTOCOL/SSL_CIPHER from the TLS connection itself.
    # Client headers have HTTP_ prefixes and cannot provide these reserved keys.
    tls = bool(environ.get("SSL_PROTOCOL") and environ.get("SSL_CIPHER"))
    environ = environ.copy()
    environ["wsgi.url_scheme"] = "https" if tls else "http"

    def start(status, headers, exc_info=None):
        if release_sha:
            headers.append(("X-Command-Centre-Release", release_sha))
        if environ.get("PATH_INFO") == "/api/v1/health":
            port = environ.get("SERVER_PORT")
            port = port if port in ("80", "443") else "other"
            headers.append(("X-Command-Centre-Transport", f"tls={int(tls)};port={port}"))
        return start_response(status, headers, exc_info) if exc_info else start_response(status, headers)

    return app(environ, start)
