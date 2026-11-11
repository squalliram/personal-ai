"""Domain-scoped release installer, executed over SSH with Python 3.12.

Secrets arrive on stdin, never in argv or logs. The energy checkout is only
used as a Git object store and read-only data source, never checked out here.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import runpy
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

DOMAIN = "iramkhan.com"
URI = "command-centre"


class ReleaseError(Exception):
    pass


def run(args, *, cwd=None, umask=0o077):
    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=120, umask=umask)
    if result.returncode:
        raise ReleaseError("Hosting command failed: " + Path(args[0]).name)
    return result.stdout


def atomic_write(path: Path, data: bytes, *, mode=0o600):
    if path.is_symlink():
        raise ReleaseError("Refusing a symlink at a managed file")
    fd, temporary = tempfile.mkstemp(prefix=".release-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure_wsgi_mapping(app: Path, home: Path):
    """Complete only this mount's CloudLinux mapping for LiteSpeed."""
    mount = home / "public_html" / URI
    path = mount / ".htaccess"
    require(not mount.is_symlink() and not path.is_symlink() and path.is_file(), "Scoped app mapping is unavailable")
    original = path.read_bytes()
    text = original.decode("utf-8")
    directives = {}
    for line in text.splitlines():
        if re.match(r"\s*Passenger(?:AppRoot|BaseURI|AppType|StartupFile)\b", line, re.IGNORECASE):
            fields = shlex.split(line, comments=True)
            require(len(fields) == 2, "Unrecognized scoped Passenger directive")
            directives[fields[0].lower()] = fields[1]
    require(directives.get("passengerapproot") == str(app), "Scoped mapping points to another app")
    require(directives.get("passengerbaseuri") == "/" + URI, "Scoped mapping points to another URL")
    updated = text
    additions = []
    for key, value in (("PassengerAppType", "wsgi"), ("PassengerStartupFile", "passenger_wsgi.py")):
        existing = directives.get(key.lower())
        require(existing is None or existing == value, "Scoped WSGI mapping conflicts with another configuration")
        if existing is None:
            additions.append(f"{key} {value}")
    if additions:
        updated = updated.rstrip() + "\n\n# Command Centre WSGI mapping\n" + "\n".join(additions) + "\n"
    redirect = (
        "# Command Centre HTTPS BEGIN\n"
        "RewriteEngine On\n"
        "RewriteCond %{HTTPS} !=on\n"
        f"RewriteRule ^ https://{DOMAIN}%{{REQUEST_URI}} [R=308,L,NE]\n"
        "# Command Centre HTTPS END"
    )
    if "# Command Centre HTTPS BEGIN" in updated:
        require(redirect in updated, "Scoped HTTPS mapping conflicts with the managed redirect")
    else:
        updated = updated.rstrip() + "\n\n" + redirect + "\n"
    if updated != text:
        atomic_write(app / "mount.htaccess.previous", original)
        # .htaccess contains no secrets and must be readable by the web server.
        atomic_write(path, updated.encode(), mode=0o644)
    # The selector may have inherited the private installer umask on an earlier
    # release. Only this public mount and its credential-free mapping are public.
    mount.chmod(0o755)
    path.chmod(0o644)
    return path, original


def selector(action, *args):
    executable = shutil.which("cloudlinux-selector")
    if not executable:
        raise ReleaseError("CloudLinux Python Selector is unavailable")
    raw = run(
        [executable, action, "--json", "--interpreter", "python", "--domain", DOMAIN, *args],
        umask=0o022,
    )
    try:
        response = json.loads(raw)
    except ValueError:
        raise ReleaseError("CloudLinux returned an unreadable response") from None
    if response.get("result") != "success":
        # Registry errors can contain environment values. Do not log them.
        raise ReleaseError("CloudLinux app operation failed: " + action)
    return response


def registration(registry, app: Path, home: Path):
    """Reject a conflicting URL/root; never change an unrelated app."""
    relative = str(app.relative_to(home))
    found = None
    versions = registry.get("available_versions", {})
    if not registry.get("selector_enabled") or not any(
        version.startswith("3.12") and info.get("status") == "enabled" for version, info in versions.items()
    ):
        raise ReleaseError("Python 3.12 is not enabled in CloudLinux")
    for version, info in versions.items():
        for user in info.get("users", {}).values():
            for root, details in user.get("applications", {}).items():
                same_root = root in (relative, str(app))
                same_url = details.get("domain") == DOMAIN and str(details.get("app_uri", "")).strip("/") == URI
                if same_root != same_url:
                    raise ReleaseError("Existing app conflicts with the requested root or URL")
                if same_url:
                    if (
                        not version.startswith("3.12")
                        or details.get("startup_file") != "passenger_wsgi.py"
                        or details.get("entry_point", "application") != "application"
                    ):
                        raise ReleaseError("Existing Command Centre app needs an explicit configuration change")
                    found = details.get("app_status", "started")
    return found


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, *, headers=None):
    try:
        response = build_opener(NoRedirect()).open(Request(url, headers=headers or {}), timeout=15)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read(2 * 1024 * 1024)


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def verify(app: Path, sha: str, deployment_id: str, *, revoke_sessions=False):
    base = f"https://{DOMAIN}/{URI}"
    status, headers = None, {}
    for attempt in range(24):
        try:
            status, headers, body = fetch(base + "/api/v1/health")
            if (
                status == 200
                and headers.get("X-Command-Centre-Release") == sha
                and headers.get("X-Command-Centre-Deployment") == deployment_id
                and json.loads(body) == {"status": "ok"}
            ):
                break
        except (OSError, ValueError):
            pass
        if attempt == 23:
            served = headers.get("X-Command-Centre-Release", "")
            served = served if re.fullmatch(r"[a-f0-9]{40}", served) else "unversioned"
            transport = headers.get("X-Command-Centre-Transport", "")
            transport = transport if re.fullmatch(r"tls=[01];port=(80|443|other)", transport) else "unknown"
            raise ReleaseError(
                f"Public HTTPS release identity/health check failed "
                f"(status={status}, served_release={served}, transport={transport})"
            )
        time.sleep(3)
    require(headers.get("X-Content-Type-Options") == "nosniff", "Security headers missing")
    require(bool(headers.get("Content-Security-Policy")), "Content security policy missing")
    require(bool(headers.get("Strict-Transport-Security")), "HSTS missing")
    for path, needle in (("/", b"Command Centre"), ("/static/app.js", b"api/v1/"), ("/static/app.css", b"{")):
        status, _, body = fetch(base + path)
        require(status == 200 and needle in body, "Page or static asset check failed")
    paths = ("/api/v1/overview", "/api/v1/energy", "/api/v1/integrations", "/api/v1/automations", "/api/v1/session")
    for path in paths:
        status, _, _ = fetch(base + path)
        require(status == 401, "Protected API exposed or unavailable")
    for request_headers in ({}, {"X-Forwarded-Proto": "https"}):
        status, headers, _ = fetch(f"http://{DOMAIN}/{URI}/api/v1/health", headers=request_headers)
        transport = headers.get("X-Command-Centre-Transport", "")
        transport = transport if re.fullmatch(r"tls=[01];port=(80|443|other)", transport) else "unknown"
        require(
            status in (301, 302, 307, 308),
            f"Plain HTTP did not redirect "
            f"(forwarded_header={bool(request_headers)}, status={status}, transport={transport}, "
            f"cache_hit={headers.get('X-LiteSpeed-Cache') == 'hit'})",
        )
        require(headers.get("Location", "").startswith(base + "/"), "HTTPS redirect escaped the Command Centre path")

    # Short-lived READ session tests real HTTPS API/data paths without knowing,
    # logging, or replacing the owner's passphrase. Always revoke it.
    namespace = runpy.run_path(str(app / "passenger_wsgi.py"))
    application = namespace["app"]
    from command_centre.security import Permission

    token, _ = application.sessions.create(Permission.READ)
    try:
        cookie = {"Cookie": f"__Host-cc_session={token}"}
        results = {}
        for path in paths:
            status, _, body = fetch(base + path, headers=cookie)
            require(status == 200, "Authenticated read-only API check failed")
            results[path] = json.loads(body)
        energy = results["/api/v1/energy"]
        require(
            (energy["state"] == "available" and bool(energy.get("daily") or energy.get("totals")))
            or (energy["state"] == "refresh_required" and bool(energy.get("sync_history"))),
            "Live energy source has no readable data",
        )
        if revoke_sessions:
            application.sessions.revoke_all()
            status, _, _ = fetch(base + "/api/v1/session", headers=cookie)
            require(status == 401, "Credential reset did not invalidate existing sessions")
        return {
            "energy_state": energy["state"],
            "energy_daily_rows": len(energy["daily"]),
            "briefing_state": application._collect()["briefing"]["state"],
            "authenticated_read_smoke": True,
            "owner_passphrase_sign_in_tested": False,
            "existing_sessions_revoked": revoke_sessions,
        }
    finally:
        application.sessions.revoke(token)


def install(checkout: Path, sha: str, password_hash: str, *, home: Path | None = None):
    home = (home or Path.home()).resolve()
    checkout = checkout.resolve()
    require(bool(re.fullmatch(r"[a-f0-9]{40}", sha)), "Invalid release SHA")
    require(
        bool(re.fullmatch(r"pbkdf2_sha256\$\d+\$[A-Za-z0-9_-]{22,}\$[A-Za-z0-9_-]{43}", password_hash)),
        "Invalid password hash format",
    )
    require(600_000 <= int(password_hash.split("$")[1]) <= 10_000_000, "Unsafe password hash iterations")
    app = checkout.parent / "command-centre-app"
    require(app.is_relative_to(home) and "public_html" not in app.relative_to(home).parts, "App root is not private")
    require((checkout / ".git").is_dir(), "Server Git checkout is unavailable")
    os.umask(0o077)
    # Same lock as the energy release, held across EVERY mutation and verification.
    fd = os.open(checkout / ".git/energy-dashboard.deploy.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run(["git", "fetch", "--no-tags", "origin", "main"], cwd=checkout)
        current = run(["git", "rev-parse", "origin/main"], cwd=checkout).decode().strip()
        if current != sha:
            return {"status": "skipped_stale", "release": sha}
        registry = selector("get")
        registered = registration(registry, app, home)
        require(not app.is_symlink(), "App root must not be a symlink")
        require(
            not app.exists() or not any(app.iterdir()) or (app / ".command-centre-managed").is_file(),
            "Existing unmanaged app directory will not be overwritten",
        )
        # LiteSpeed must stat <app>/tmp/restart.txt as its web-server user.
        # Execute-only access allows that, without directory listings or secret reads.
        app.mkdir(mode=0o711, exist_ok=True)
        app.chmod(0o711)
        atomic_write(app / ".command-centre-managed", b"1\n")
        for directory in ("releases", "data", "tmp"):
            target = app / directory
            require(not target.is_symlink(), "Managed directories must not be symlinks")
            mode = 0o711 if directory == "tmp" else 0o700
            target.mkdir(mode=mode, exist_ok=True)
            target.chmod(mode)
        release = app / "releases" / sha
        if not release.exists():
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=app / "releases"))
            try:
                archive = run(["git", "archive", "--format=tar", sha], cwd=checkout)
                with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                    source.extractall(staging, filter="data")
                atomic_write(staging / ".complete", sha.encode())
                os.replace(staging, release)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        require(not release.is_symlink() and (release / ".complete").read_text() == sha, "Incomplete code release")
        deployment_id = secrets.token_hex(16)
        values = {
            "PASSWORD_HASH": password_hash,
            "CHECKOUT": str(release),
            "RELEASE_SHA": sha,
            "DEPLOYMENT_ID": deployment_id,
            "ALLOWED_HOSTS": DOMAIN,
            "AUDIT_DB": str(app / "data/audit.sqlite3"),
            "SESSIONS_DB": str(app / "data/sessions.sqlite3"),
            "ENERGY_DB": str(checkout / "data/octopus_energy.sqlite3"),
            "ENERGY_STATUS": str(checkout / "reports/energy/dashboard/status.json"),
            "BRIEFING_DB": str(checkout / "data/daily_briefing.sqlite3"),
            "BRIEFING_REPORTS": str(checkout / "reports/daily"),
            "PERMISSION": "recommend",
            "INSPECT_CRONTAB": "1",
        }
        require(
            all("\n" not in value and "\r" not in value for value in values.values()), "Invalid configuration value"
        )
        configuration = "".join(f"COMMAND_CENTRE_{key}={value}\n" for key, value in values.items()).encode()
        env_file, entrypoint = app / "command-centre.env", app / "passenger_wsgi.py"
        verified_file = app / ".verified-release"
        require(not verified_file.is_symlink(), "Refusing a symlink at release metadata")
        repeat_verified = verified_file.exists() and verified_file.read_text() == sha
        previous = {}
        for path in (env_file, entrypoint):
            require(not path.is_symlink(), "Refusing a symlink at an app file")
            previous[path] = path.read_bytes() if path.exists() else None
            if previous[path] is not None and not repeat_verified:
                atomic_write(path.with_name(path.name + ".previous"), previous[path])
        old_hash = next(
            (
                line.partition("=")[2]
                for line in (previous[env_file] or b"").decode().splitlines()
                if line.startswith("COMMAND_CENTRE_PASSWORD_HASH=")
            ),
            None,
        )
        credential_changed = previous[env_file] is not None and old_hash != password_hash
        created = False
        started = False
        mapping_path, previous_mapping = None, None
        try:
            wrapper = (release / "deploy/command-centre/passenger_wsgi.py").read_bytes()
            atomic_write(entrypoint, wrapper)
            atomic_write(env_file, configuration)
            relative = str(app.relative_to(home))
            if not registered:
                selector(
                    "create",
                    "--app-root",
                    relative,
                    "--app-uri",
                    URI,
                    "--version",
                    "3.12",
                    "--startup-file",
                    "passenger_wsgi.py",
                    "--entry-point",
                    "application",
                )
                created = True
                # Some selector versions create an example entrypoint.
                atomic_write(entrypoint, wrapper)
            # Fail closed on settings/import errors BEFORE a successful release is reported.
            run([sys.executable, "-c", "import runpy,sys; runpy.run_path(sys.argv[1])", str(entrypoint)])
            if registered == "stopped":
                selector("start", "--app-root", relative)
                started = True
            mapping_path, previous_mapping = configure_wsgi_mapping(app, home)
            (app / "tmp/restart.txt").touch(mode=0o600)
            result = verify(app, sha, deployment_id, revoke_sessions=credential_changed)
            atomic_write(verified_file, sha.encode())
        except Exception:
            if mapping_path is not None:
                atomic_write(mapping_path, previous_mapping, mode=0o644)
            for path, content in previous.items():
                if content is not None:
                    atomic_write(path, content)
            (app / "tmp/restart.txt").touch(mode=0o600)
            if created or started:
                selector("stop", "--app-root", str(app.relative_to(home)))
            raise
        return {"status": "deployed", "release": sha, **result}


def main():
    try:
        payload = json.loads(sys.stdin.read(16 * 1024))
        result = install(Path(sys.argv[1]), sys.argv[2], payload["password_hash"])
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        # Even exception strings from external tools may include protected values.
        print("Command Centre deployment failed: " + type(error).__name__, file=sys.stderr)
        if isinstance(error, ReleaseError):
            print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
