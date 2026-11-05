#!/bin/sh
# Publish only the generated, identifier-free dashboard files. Run on cPanel
# after its directory protection and server-side .env have been configured.
set -eu

dashboard_dir="${DASHBOARD_DIR:-reports/energy/dashboard}"
target_dir="${DASHBOARD_TARGET:?Set DASHBOARD_TARGET to the protected web directory}"
. "$(dirname "$0")/find-python.sh"

"$PYTHON" - "$dashboard_dir" "$target_dir" <<'PY'
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

source, target = map(Path, sys.argv[1:])
for name in ("index.html", "data.json"):
    if not (source / name).is_file():
        raise SystemExit("Dashboard export is missing; nothing published.")

# Fail closed for the standard cPanel per-directory Basic Auth configuration.
# This is a local safeguard, not a substitute for checking HTTP 401 responses.
try:
    lines = (target / ".htaccess").read_text(encoding="utf-8").splitlines()
    directives = {}
    depth = 0
    for line in lines:
        parts = shlex.split(line, comments=True)
        if not parts:
            continue
        key = parts[0].lower()
        if key.startswith("</"):
            depth -= 1
        elif key.startswith("<"):
            depth += 1
        elif key in ("authtype", "authuserfile", "require", "satisfy", "authmerging", "allow", "deny", "order"):
            if depth or key in directives:
                raise ValueError("Ambiguous protection")
            directives[key] = parts[1:]
    if depth or directives.get("authtype") != ["Basic"] or directives.get("require") != ["valid-user"]:
        raise ValueError("Basic authentication is required")
    if any(key in directives for key in ("satisfy", "authmerging", "allow", "deny", "order")):
        raise ValueError("Unsupported authorization overrides")
    password_file, = directives.get("authuserfile", [])
    if not Path(password_file).is_absolute() or not Path(password_file).is_file():
        raise ValueError("Password file unavailable")
except (OSError, ValueError):
    raise SystemExit("Protected target not verified: configure direct cPanel directory Basic Auth before publishing.")

for name in ("data.json", "index.html"):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=".energy-", delete=False) as output:
            temporary = Path(output.name)
            with (source / name).open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
        temporary.chmod(0o644)
        os.replace(temporary, target / name)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
print("Dashboard files published; verify unauthenticated HTTP access remains denied.")
PY
