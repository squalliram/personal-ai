"""WSGI application: authentication, CSRF, permissions, rate limits and the read-only JSON API."""

from __future__ import annotations

import hmac
import json
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from . import sources
from .audit import AuditLog
from .automations import CronReader, list_automations, read_crontab
from .config import Settings
from .security import Permission, RateLimiter, Session, SessionStore, SqliteSessionStore, verify_password

LONDON = ZoneInfo("Europe/London")
STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
}
MAX_BODY_BYTES = 4096
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)

Result = tuple[int, object, list[tuple[str, str]]]


@dataclass(frozen=True)
class Route:
    handler: Callable[[Request], Result]
    permission: Permission | None
    csrf: bool = False


@dataclass
class Request:
    environ: dict
    method: str
    path: str
    session: Session | None
    token: str | None
    client: str

    def query(self, name: str) -> str | None:
        values = parse_qs(self.environ.get("QUERY_STRING", ""), max_num_fields=10).get(name)
        return values[0] if values else None


def _error(status: int, code: str, headers: list[tuple[str, str]] | None = None) -> Result:
    return status, {"error": code}, headers or []


class CommandCentreApp:
    def __init__(
        self,
        settings: Settings,
        *,
        allowed_hosts: Iterable[str],
        secure: bool = False,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        cron_reader: CronReader = read_crontab,
        audit: AuditLog | None = None,
    ) -> None:
        self.settings, self.secure, self.clock, self.cron_reader = settings, secure, clock, cron_reader
        self.allowed_hosts = frozenset(allowed_hosts)
        self.cookie_name = "__Host-cc_session" if secure else "cc_session"
        if settings.sessions_db:
            self.sessions: SessionStore | SqliteSessionStore = SqliteSessionStore(
                settings.sessions_db,
                idle_seconds=settings.session_idle_seconds,
                absolute_seconds=settings.session_absolute_seconds,
                clock=clock,
            )
        else:
            self.sessions = SessionStore(
                idle_seconds=settings.session_idle_seconds,
                absolute_seconds=settings.session_absolute_seconds,
                clock=clock,
            )
        self.audit = audit or AuditLog(settings.audit_db, clock=clock)
        self.login_per_client = RateLimiter(5, 15 * 60, clock=monotonic)
        self.login_global = RateLimiter(20, 15 * 60, clock=monotonic)
        self.api_limiter = RateLimiter(240, 60, clock=monotonic)
        self.static = {
            path: ((STATIC_DIR / name).read_bytes(), content_type)
            for path, (name, content_type) in STATIC_FILES.items()
        }
        self.routes: dict[tuple[str, str], Route] = {
            ("GET", "/api/v1/health"): Route(self.health, None),
            ("POST", "/api/v1/session"): Route(self.login, None),
            ("GET", "/api/v1/session"): Route(self.session_info, Permission.READ),
            ("DELETE", "/api/v1/session"): Route(self.logout, Permission.READ, csrf=True),
            ("GET", "/api/v1/overview"): Route(self.overview, Permission.READ),
            ("GET", "/api/v1/modules"): Route(self.modules, Permission.READ),
            ("GET", "/api/v1/integrations"): Route(self.integrations, Permission.READ),
            ("GET", "/api/v1/automations"): Route(self.automations, Permission.READ),
            ("GET", "/api/v1/energy"): Route(self.energy, Permission.READ),
            ("GET", "/api/v1/briefing/runs"): Route(self.briefing_runs, Permission.READ),
            ("GET", "/api/v1/audit"): Route(self.audit_log, Permission.READ),
        }

    # WSGI -------------------------------------------------------------------------------------

    def __call__(self, environ: dict, start_response: Callable) -> list[bytes]:
        try:
            status, payload, headers = self._dispatch(environ)
        except Exception as error:
            print(f"command-centre internal_error {type(error).__name__}", file=sys.stderr)
            status, payload, headers = _error(500, "internal_error")
        if isinstance(payload, tuple):
            body, content_type = payload
        else:
            body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            content_type = "application/json"
        start_response(
            f"{status} {HTTPStatus(status).phrase}",
            [*self._security_headers(), ("Content-Type", content_type), ("Content-Length", str(len(body))), *headers],
        )
        return [body]

    def _security_headers(self) -> list[tuple[str, str]]:
        headers = [
            ("Content-Security-Policy", CSP),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Cache-Control", "no-store"),
            ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Cross-Origin-Resource-Policy", "same-origin"),
            ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
        ]
        if self.secure:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        return headers

    def _dispatch(self, environ: dict) -> Result:
        host = environ.get("HTTP_HOST", "")
        if host not in self.allowed_hosts:
            return _error(421, "misdirected_request")
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "")
        if (
            self.secure
            and environ.get("wsgi.url_scheme") != "https"
            and environ.get("HTTP_X_FORWARDED_PROTO") != "https"
        ):
            # TLS terminates at Apache in hosted deployments; never serve over HTTP there.
            location = f"https://{host}{environ.get('SCRIPT_NAME', '')}{path}"
            query = environ.get("QUERY_STRING", "")
            return 308, {"error": "https_required"}, [("Location", location + (f"?{query}" if query else ""))]
        if not path:
            return 308, {"error": "redirect"}, [("Location", environ.get("SCRIPT_NAME", "") + "/")]
        if path in self.static:
            if method != "GET":
                return _error(405, "method_not_allowed", [("Allow", "GET")])
            return 200, self.static[path], []
        route = self.routes.get((method, path))
        if route is None:
            allowed = sorted(m for m, p in self.routes if p == path)
            if allowed:
                return _error(405, "method_not_allowed", [("Allow", ", ".join(allowed))])
            return _error(404, "not_found")
        client = environ.get("REMOTE_ADDR", "unknown")
        token = self._cookie_token(environ)
        session = self.sessions.get(token)
        actor = "owner" if session else "anonymous"
        if method not in ("GET", "HEAD") and not self._same_origin(environ, host):
            self.audit.record("request", "cross_origin_rejected", actor=actor, path=path, client=client)
            return _error(403, "cross_origin_rejected")
        if route.permission is not None:
            if session is None:
                return _error(401, "authentication_required")
            if not self.api_limiter.hit(session.token_hash):
                self.audit.record("request", "rate_limited", actor=actor, path=path, client=client)
                return _error(429, "rate_limited", [("Retry-After", "60")])
            if session.permission < route.permission:
                self.audit.record(
                    "request", "permission_denied", actor=actor, path=path, required=route.permission.label
                )
                return _error(403, "insufficient_permission")
            supplied = environ.get("HTTP_X_CSRF_TOKEN", "")
            if route.csrf and not hmac.compare_digest(supplied.encode(), session.csrf_token.encode()):
                self.audit.record("request", "csrf_rejected", actor=actor, path=path, client=client)
                return _error(403, "csrf_rejected")
        return route.handler(Request(environ, method, path, session, token, client))

    def _cookie_token(self, environ: dict) -> str | None:
        try:
            cookie = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        except CookieError:
            return None
        morsel = cookie.get(self.cookie_name)
        return morsel.value if morsel and TOKEN_PATTERN.fullmatch(morsel.value) else None

    def _same_origin(self, environ: dict, host: str) -> bool:
        expected = f"{'https' if self.secure else 'http'}://{host}"
        if environ.get("HTTP_ORIGIN") != expected:
            return False
        return environ.get("HTTP_SEC_FETCH_SITE", "same-origin") == "same-origin"

    def _cookie(self, token: str, max_age: int) -> tuple[str, str]:
        secure = "; Secure" if self.secure else ""
        return "Set-Cookie", f"{self.cookie_name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), LONDON)

    def _iso(self, instant: float) -> str:
        return datetime.fromtimestamp(instant, LONDON).isoformat(timespec="seconds")

    # Handlers ---------------------------------------------------------------------------------

    def health(self, request: Request) -> Result:
        return 200, {"status": "ok"}, []

    def login(self, request: Request) -> Result:
        environ, client = request.environ, request.client
        if not self.settings.password_hash:
            return _error(503, "authentication_not_configured")
        if self.login_per_client.blocked(client) or self.login_global.blocked("all"):
            self.audit.record("login", "rate_limited", client=client)
            return _error(429, "rate_limited", [("Retry-After", "900")])
        if not environ.get("CONTENT_TYPE", "").split(";")[0].strip() == "application/json":
            return _error(415, "json_required")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return _error(400, "invalid_request")
        if not 0 < length <= MAX_BODY_BYTES:
            return _error(413 if length > MAX_BODY_BYTES else 400, "invalid_request")
        try:
            body = json.loads(environ["wsgi.input"].read(length))
        except (ValueError, UnicodeDecodeError):
            return _error(400, "invalid_request")
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(password, str) or set(body) != {"password"}:
            return _error(400, "invalid_request")
        if not verify_password(password, self.settings.password_hash):
            self.login_per_client.record(client)
            self.login_global.record("all")
            self.audit.record("login", "failure", client=client)
            return _error(401, "invalid_credentials")
        self.login_per_client.reset(client)
        self.sessions.revoke(request.token)
        token, session = self.sessions.create(self.settings.permission_ceiling)
        self.audit.record("login", "success", actor="owner", client=client, permission=session.permission.label)
        return 200, self._session_payload(session), [self._cookie(token, self.settings.session_absolute_seconds)]

    def _session_payload(self, session: Session) -> dict:
        return {
            "authenticated": True,
            "permission": session.permission.label,
            "permission_levels": [p.label for p in Permission],
            "csrf_token": session.csrf_token,
            "expires_at": self._iso(self.sessions.expires_at(session)),
            "idle_timeout_seconds": self.settings.session_idle_seconds,
        }

    def session_info(self, request: Request) -> Result:
        return 200, self._session_payload(request.session), []

    def logout(self, request: Request) -> Result:
        self.sessions.revoke(request.token)
        self.audit.record("logout", "success", actor="owner", client=request.client)
        return 200, {"authenticated": False}, [self._cookie("", 0)]

    def _collect(self) -> dict:
        now = self._now()
        energy = sources.energy_snapshot(self.settings, now)
        briefing = sources.briefing_snapshot(self.settings)
        found = sources.integrations(self.settings, now, energy, briefing)
        health = {item["id"]: item for item in found}
        jobs = list_automations(self.settings, now, self.cron_reader, energy, briefing, health)
        return {"now": now, "energy": energy, "briefing": briefing, "integrations": found, "automations": jobs}

    def overview(self, request: Request) -> Result:
        data = self._collect()
        severity = {"failed": "critical", "degraded": "warning", "stale": "warning", "refresh_required": "warning"}
        alerts = [
            {
                "severity": severity.get(item["status"], "info"),
                "kind": "fact",
                "source": item["id"],
                "fact": f"{item['name']}: {item['summary']}",
                "evidence": item["evidence"],
            }
            for item in data["integrations"]
            if item["status"] not in ("operational", "not_implemented")
        ]
        alerts += [
            {
                "severity": severity.get(job["status"], "info"),
                "kind": "fact",
                "source": job["id"],
                "fact": f"{job['name']}: {job['summary']}",
                "evidence": {"alerts": job["alerts"], "last_execution": job["last_execution"]},
            }
            for job in data["automations"]
            if job["status"] not in ("operational",) and job["id"] != "daily-briefing"
        ]
        order = {"critical": 0, "warning": 1, "info": 2}
        alerts.sort(key=lambda alert: order[alert["severity"]])
        counts: dict[str, int] = {}
        for item in data["integrations"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        energy = data["energy"]
        return (
            200,
            {
                "generated_at": data["now"].isoformat(timespec="seconds"),
                "date": data["now"].date().isoformat(),
                "timezone": "Europe/London",
                "attention": alerts,
                "integration_counts": counts,
                "missing_integrations": [
                    {"id": item["id"], "name": item["name"], "summary": item["summary"]}
                    for item in data["integrations"]
                    if item["status"] == "not_implemented"
                ],
                "briefing": {
                    "latest_run": data["briefing"]["runs"][0] if data["briefing"]["runs"] else None,
                    "latest_report_date": data["briefing"]["latest_report_date"],
                },
                "energy": {
                    "state": energy["state"],
                    "basis": energy["basis"],
                    "recent_days": energy["daily"][-14:],
                    "last_successful_sync_at": energy["last_successful_sync_at"],
                    "actual_bills": energy["actual_bills"],
                },
                "automations": [{"id": j["id"], "name": j["name"], "status": j["status"]} for j in data["automations"]],
                "not_yet_available": [
                    {"item": "Today's calendar, tasks and priorities", "phase": 2},
                    {"item": "AI recommendations with evidence", "phase": 4},
                ],
            },
            [],
        )

    def modules(self, request: Request) -> Result:
        data = self._collect()
        return 200, {"modules": sources.modules(data["integrations"])}, []

    def integrations(self, request: Request) -> Result:
        data = self._collect()
        return 200, {"integrations": data["integrations"], "states": list(sources.HEALTH_STATES)}, []

    def automations(self, request: Request) -> Result:
        data = self._collect()
        return 200, {"automations": data["automations"]}, []

    def energy(self, request: Request) -> Result:
        now = self._now()
        snapshot = sources.energy_snapshot(self.settings, now)
        return 200, {**snapshot, "health": sources.energy_health(snapshot, self.settings, now)}, []

    def briefing_runs(self, request: Request) -> Result:
        raw = request.query("limit") or "14"
        if not raw.isdigit() or not 1 <= int(raw) <= 100:
            return _error(400, "invalid_parameters")
        snapshot = sources.briefing_snapshot(self.settings, int(raw))
        return 200, snapshot, []

    def audit_log(self, request: Request) -> Result:
        return 200, {"entries": self.audit.recent(50)}, []
