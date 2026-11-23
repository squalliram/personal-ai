"""WSGI application: authentication, CSRF, permissions, rate limits and the read-only JSON API."""

from __future__ import annotations

import csv
import email.parser
import email.policy
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import notifications
from instagram_agent import clips as instagram_clips
from instagram_agent import cloudflare_client, openai_client
from instagram_agent import music as instagram_music
from instagram_agent import storage as instagram_storage
from instagram_agent import uploads as instagram_uploads

from . import assistant, energy_refresh, insights, passkeys, sources
from .audit import AuditLog
from .automations import CronReader, list_automations, read_crontab
from .config import Settings
from .security import (
    Permission,
    RateLimiter,
    Session,
    SessionStore,
    SqliteRateLimiter,
    SqliteSessionStore,
    verify_password,
)

LONDON = ZoneInfo("Europe/London")
STATIC_DIR = Path(__file__).with_name("static")
# The standalone /energy-usage dashboard script doubles as the Energy view's pure helper library.
ENERGY_CORE = Path(__file__).resolve().parent.parent / "octopus_energy/dashboard.js"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/energy-core.js": (ENERGY_CORE, "text/javascript; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
}
MAX_BODY_BYTES = 4096
MAX_TELEGRAM_BODY_BYTES = 64 * 1024
# Overview, integrations, automations, modules and the assistant share one snapshot. It is reused until a
# source file changes or this many seconds pass, so time-based health (stale, overdue) stays current.
COLLECT_TTL_SECONDS = 30
AUDIT_PAGE_SIZES = (10, 25, 50, 100)
AUDIT_EXPORT_LIMIT = 10_000
AUDIT_ACTION = re.compile(r"[a-z_]{1,40}")
SESSION_ID = re.compile(r"[a-f0-9]{16}")
PAGE_NUMBER = re.compile(r"[0-9]{1,6}")
# Spreadsheet apps run a cell starting with any of these as a formula (OWASP "CSV injection").
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
# Callers allowed to post results to the owner inbox with the ingest bearer token.
INGEST_SOURCES = ("jarvis",)
INGEST_OUTCOMES = ("ok", "failed")
INGEST_FIELDS = {"source", "outcome", "title", "link"}
MAX_INGEST_TITLE = 200
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
    # Machine callers: the handler checks its own bearer token; no cookie session, Origin or CSRF check.
    bearer: bool = False


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


def _json_body(environ: dict, limit: int = MAX_BODY_BYTES) -> tuple[object, Result | None]:
    if not environ.get("CONTENT_TYPE", "").split(";")[0].strip() == "application/json":
        return None, _error(415, "json_required")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return None, _error(400, "invalid_request")
    if not 0 < length <= limit:
        return None, _error(413 if length > limit else 400, "invalid_request")
    try:
        return json.loads(environ["wsgi.input"].read(length)), None
    except (ValueError, UnicodeDecodeError):
        return None, _error(400, "invalid_request")


def _form_parts(environ: dict, limit: int) -> tuple[list[tuple[str, bool, bytes]] | None, Result | None]:
    """(name, is_file, data) for each part of a multipart/form-data body of at most `limit` bytes."""
    content_type = environ.get("CONTENT_TYPE", "")
    if content_type.split(";")[0].strip().lower() != "multipart/form-data" or "boundary=" not in content_type:
        return None, _error(415, "multipart_required")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return None, _error(400, "invalid_request")
    if not 0 < length <= limit:
        return None, _error(413 if length > 0 else 400, "upload_too_large" if length > 0 else "invalid_request")
    body = environ["wsgi.input"].read(length)
    if len(body) != length:
        return None, _error(400, "invalid_request")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "replace")
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(header + body)
    if not message.is_multipart() or message.defects:
        return None, _error(400, "invalid_request")
    parts = []
    for part in message.iter_parts():
        data = part.get_payload(decode=True)
        if part.get_content_disposition() != "form-data" or not isinstance(data, bytes):
            return None, _error(400, "invalid_request")
        parts.append((part.get_param("name", header="content-disposition"), part.get_filename() is not None, data))
    return parts, None


def _fields(parts, files: str, texts: set[str]) -> tuple[dict[str, str], list[bytes]] | None:
    """Each text field at most once and the `files` file parts; None if anything else is in the form."""
    found: dict[str, str] = {}
    data = []
    for name, is_file, value in parts:
        if is_file and name == files:
            data.append(value)
        elif not is_file and name in texts and name not in found:
            try:
                found[name] = value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        else:
            return None
    return found, data


def _photo_form(environ: dict) -> tuple[tuple[str, list[bytes], dict] | None, Result | None]:
    """The note, photo files and reel choices (`reel`, `music`, `mix`) from a photo upload, nothing else."""
    parts, error = _form_parts(environ, instagram_uploads.MAX_UPLOAD_BYTES)
    if error:
        return None, error
    if sum(1 for name, is_file, _ in parts if is_file and name == "photos") > instagram_uploads.MAX_PHOTOS:
        return None, _error(400, "photo_count")
    if sum(1 for name, is_file, _ in parts if not is_file and name == "note") != 1:
        return None, _error(400, "invalid_note")
    found = _fields(parts, "photos", {"note", "reel", "music", "mix"})
    if found is None:
        return None, _error(400, "invalid_note" if any(n == "note" for n, f, _ in parts) else "invalid_request")
    fields, photos = found
    return (fields.pop("note"), photos, fields), None


def csv_cell(value: object) -> object:
    """Neutralise spreadsheet formulas by prefixing an apostrophe, so a cell is always shown as text."""
    if isinstance(value, str) and value.lstrip(" ").startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


_PLATFORMS = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Macintosh", "Mac"),
    ("Windows", "Windows"),
    ("Linux", "Linux"),
)
_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("FxiOS/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)


def describe_device(user_agent: str) -> str:
    """A coarse label such as "Safari on iPhone"; the raw User-Agent is never stored."""
    platform = next((name for marker, name in _PLATFORMS if marker in user_agent), None)
    browser = next((name for marker, name in _BROWSERS if marker in user_agent), None)
    if platform and browser:
        return f"{browser} on {platform}"
    return platform or browser or "Unknown device"


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
        notify: Callable[..., int | None] = notifications.notify,
    ) -> None:
        self.settings, self.secure, self.clock, self.cron_reader = settings, secure, clock, cron_reader
        self.notify = notify
        self.monotonic = monotonic
        self._collect_lock = threading.Lock()
        self._collected: tuple[tuple, float, dict] | None = None
        self.allowed_hosts = frozenset(allowed_hosts)
        self.cookie_name = "__Host-cc_session" if secure else "cc_session"
        if settings.sessions_db:
            self.sessions: SessionStore | SqliteSessionStore = SqliteSessionStore(
                settings.sessions_db,
                idle_seconds=settings.session_idle_seconds,
                absolute_seconds=settings.session_absolute_seconds,
                remember_seconds=settings.session_remember_seconds,
                clock=clock,
            )
        else:
            self.sessions = SessionStore(
                idle_seconds=settings.session_idle_seconds,
                absolute_seconds=settings.session_absolute_seconds,
                remember_seconds=settings.session_remember_seconds,
                clock=clock,
            )
        self.audit = audit or AuditLog(settings.audit_db, clock=clock)
        self.passkeys = (
            passkeys.PasskeyStore(settings.passkeys_db, clock=clock)
            if settings.passkeys_db and settings.password_hash and passkeys.available()
            else None
        )
        self.login_per_client = RateLimiter(5, 15 * 60, clock=monotonic)
        self.login_global = RateLimiter(20, 15 * 60, clock=monotonic)
        self.api_limiter = RateLimiter(240, 60, clock=monotonic)
        self.assistant_limiter = RateLimiter(30, 60 * 60, clock=monotonic)
        self.passkey_options_limiter = RateLimiter(20, 15 * 60, clock=monotonic)
        self.ingest_limiter = RateLimiter(30, 10 * 60, clock=monotonic)
        # Bounds audit rows written for rejected cross-origin requests; the 403 itself is never limited.
        self.cross_origin_audit_limiter = RateLimiter(10, 60, clock=monotonic)
        if settings.sessions_db:
            self.login_per_client = SqliteRateLimiter(settings.sessions_db, "login_client", 5, 900, clock=clock)
            self.login_global = SqliteRateLimiter(settings.sessions_db, "login_global", 20, 900, clock=clock)
            self.api_limiter = SqliteRateLimiter(settings.sessions_db, "api", 240, 60, clock=clock)
            self.assistant_limiter = SqliteRateLimiter(settings.sessions_db, "assistant", 30, 3600, clock=clock)
            self.passkey_options_limiter = SqliteRateLimiter(
                settings.sessions_db, "passkey_options", 20, 900, clock=clock
            )
            self.ingest_limiter = SqliteRateLimiter(settings.sessions_db, "notify_ingest", 30, 600, clock=clock)
        self.static = {
            path: ((STATIC_DIR / name).read_bytes(), content_type)
            for path, (name, content_type) in STATIC_FILES.items()
        }
        self.routes: dict[tuple[str, str], Route] = {
            ("GET", "/api/v1/health"): Route(self.health, None),
            ("POST", "/api/v1/session"): Route(self.login, None),
            ("GET", "/api/v1/session"): Route(self.session_info, Permission.READ),
            ("DELETE", "/api/v1/session"): Route(self.logout, Permission.READ, csrf=True),
            ("GET", "/api/v1/passkeys/status"): Route(self.passkey_status, None),
            ("POST", "/api/v1/passkeys/login/options"): Route(self.passkey_login_options, None),
            ("POST", "/api/v1/passkeys/login"): Route(self.passkey_login, None),
            ("GET", "/api/v1/passkeys"): Route(self.passkey_list, Permission.READ),
            ("POST", "/api/v1/passkeys/register/options"): Route(
                self.passkey_register_options, Permission.READ, csrf=True
            ),
            ("POST", "/api/v1/passkeys/register"): Route(self.passkey_register, Permission.READ, csrf=True),
            ("POST", "/api/v1/passkeys/remove"): Route(self.passkey_remove, Permission.READ, csrf=True),
            ("GET", "/api/v1/overview"): Route(self.overview, Permission.READ),
            ("GET", "/api/v1/assistant"): Route(self.assistant_status, Permission.READ),
            ("POST", "/api/v1/assistant"): Route(self.ask_assistant, Permission.READ, csrf=True),
            ("GET", "/api/v1/modules"): Route(self.modules, Permission.READ),
            ("GET", "/api/v1/agents"): Route(self.agents, Permission.READ),
            ("GET", "/api/v1/integrations"): Route(self.integrations, Permission.READ),
            ("GET", "/api/v1/automations"): Route(self.automations, Permission.READ),
            ("GET", "/api/v1/energy"): Route(self.energy, Permission.READ),
            ("GET", "/api/v1/energy/dashboard"): Route(self.energy_dashboard, Permission.READ),
            ("GET", "/api/v1/energy/refresh"): Route(self.energy_refresh_status, Permission.READ),
            ("POST", "/api/v1/energy/refresh"): Route(self.energy_refresh, Permission.RECOMMEND, csrf=True),
            ("GET", "/api/v1/briefing/runs"): Route(self.briefing_runs, Permission.READ),
            ("GET", "/api/v1/audit"): Route(self.audit_log, Permission.READ),
            ("GET", "/api/v1/audit/export"): Route(self.audit_export, Permission.READ),
            ("GET", "/api/v1/sessions"): Route(self.session_list, Permission.READ),
            ("POST", "/api/v1/sessions/revoke"): Route(self.session_revoke, Permission.READ, csrf=True),
            ("GET", "/api/v1/instagram/drafts"): Route(self.instagram_drafts, Permission.READ),
            ("GET", "/api/v1/instagram/slide"): Route(self.instagram_slide, Permission.READ),
            ("POST", "/api/v1/instagram/decision"): Route(self.instagram_decision, Permission.RECOMMEND, csrf=True),
            ("POST", "/api/v1/instagram/hold"): Route(self.instagram_hold, Permission.RECOMMEND, csrf=True),
            ("POST", "/api/v1/instagram/uploads"): Route(self.instagram_upload, Permission.RECOMMEND, csrf=True),
            ("POST", "/api/v1/instagram/uploads/delete"): Route(
                self.instagram_upload_delete, Permission.RECOMMEND, csrf=True
            ),
            ("GET", "/api/v1/instagram/upload-photo"): Route(self.instagram_upload_photo, Permission.READ),
            ("POST", "/api/v1/instagram/music"): Route(self.instagram_music_add, Permission.RECOMMEND, csrf=True),
            ("POST", "/api/v1/instagram/music/delete"): Route(
                self.instagram_music_delete, Permission.RECOMMEND, csrf=True
            ),
            ("POST", "/api/v1/instagram/clips"): Route(self.instagram_clip_start, Permission.RECOMMEND, csrf=True),
            ("POST", "/api/v1/instagram/clips/chunk"): Route(
                self.instagram_clip_chunk, Permission.RECOMMEND, csrf=True
            ),
            ("POST", "/api/v1/instagram/clips/finish"): Route(
                self.instagram_clip_finish, Permission.RECOMMEND, csrf=True
            ),
            ("POST", "/api/v1/instagram/clips/delete"): Route(
                self.instagram_clip_delete, Permission.RECOMMEND, csrf=True
            ),
            ("GET", "/api/v1/notifications"): Route(self.notifications, Permission.READ),
            ("POST", "/api/v1/notifications"): Route(self.notification_ingest, None, bearer=True),
            ("POST", "/api/v1/telegram/webhook"): Route(self.telegram_webhook, None, bearer=True),
        }

    # WSGI -------------------------------------------------------------------------------------

    def __call__(self, environ: dict, start_response: Callable) -> list[bytes]:
        started = time.perf_counter()
        try:
            status, payload, headers = self._dispatch(environ)
        except Exception as error:
            print(f"command-centre internal_error {type(error).__name__}", file=sys.stderr)
            status, payload, headers = _error(500, "internal_error")
        elapsed_ms = (time.perf_counter() - started) * 1000
        if isinstance(payload, tuple):
            body, content_type = payload
        else:
            body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            content_type = "application/json"
        start_response(
            f"{status} {HTTPStatus(status).phrase}",
            [
                *self._security_headers(),
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                ("Server-Timing", f"app;dur={elapsed_ms:.1f}"),
                *headers,
            ],
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
            # The microphone is for the browser's own speech recognition ("Hello Jarvis"); no audio reaches this server.
            ("Permissions-Policy", "camera=(), microphone=(self), geolocation=()"),
        ]
        if self.secure:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        return headers

    def _dispatch(self, environ: dict) -> Result:
        host = environ.get("HTTP_HOST", "")
        if host not in self.allowed_hosts:
            return _error(421, "misdirected_request")
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "")
        if self.secure and environ.get("wsgi.url_scheme") != "https":
            # Passenger derives the WSGI scheme from Apache, not client-supplied headers.
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
        if route.bearer:
            return route.handler(Request(environ, method, path, None, None, client))
        token = self._cookie_token(environ)
        session = self.sessions.get(token)
        if session is not None and session.permission > self.settings.permission_ceiling:
            # A lowered ceiling must apply to sessions stored before the change.
            session.permission = self.settings.permission_ceiling
        actor = "owner" if session else "anonymous"
        if method not in ("GET", "HEAD") and not self._same_origin(environ, host):
            if self.cross_origin_audit_limiter.hit(client):
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
        body, error = _json_body(environ)
        if error:
            return error
        password = body.get("password") if isinstance(body, dict) else None
        remember = body.get("remember", False) if isinstance(body, dict) else None
        if not isinstance(password, str) or not isinstance(remember, bool) or not set(body) <= {"password", "remember"}:
            return _error(400, "invalid_request")
        # Reserve attempts before expensive verification, including across Passenger workers.
        reservation = self.login_global.reserve("all")
        if reservation is None or not self.login_per_client.hit(client):
            if reservation is not None:
                self.login_global.release("all", reservation)
            self.audit.record("login", "rate_limited", client=client)
            return _error(429, "rate_limited", [("Retry-After", "900")])
        if not verify_password(password, self.settings.password_hash):
            self.audit.record("login", "failure", client=client)
            return _error(401, "invalid_credentials")
        self.login_global.release("all", reservation)
        self.login_per_client.reset(client)
        return self._start_session(request, remember, "passphrase")

    def _start_session(self, request: Request, remember: bool, method: str) -> Result:
        self.sessions.revoke(request.token)
        device = describe_device(request.environ.get("HTTP_USER_AGENT", ""))
        token, session = self.sessions.create(
            self.settings.permission_ceiling, remember=remember, device=device, client=request.client
        )
        self.audit.record(
            "login",
            "success",
            actor="owner",
            client=request.client,
            permission=session.permission.label,
            remembered=session.remember,
            method=method,
            device=device,
        )
        lifetime = (
            self.settings.session_remember_seconds if session.remember else self.settings.session_absolute_seconds
        )
        return 200, self._session_payload(session), [self._cookie(token, lifetime)]

    # Passkeys ---------------------------------------------------------------------------------

    def _webauthn_party(self, environ: dict) -> tuple[str, str]:
        host = environ.get("HTTP_HOST", "")
        return f"{'https' if self.secure else 'http'}://{host}", host.rpartition(":")[0] if ":" in host else host

    def _reserve_attempt(self, client: str, action: str) -> tuple[int | None, Result | None]:
        if self.login_per_client.blocked(client) or self.login_global.blocked("all"):
            self.audit.record(action, "rate_limited", client=client)
            return None, _error(429, "rate_limited", [("Retry-After", "900")])
        reservation = self.login_global.reserve("all")
        if reservation is None or not self.login_per_client.hit(client):
            if reservation is not None:
                self.login_global.release("all", reservation)
            self.audit.record(action, "rate_limited", client=client)
            return None, _error(429, "rate_limited", [("Retry-After", "900")])
        return reservation, None

    def _passkey_body(self, environ: dict, required: set[str], optional: set[str], limit: int = MAX_BODY_BYTES):
        body, error = _json_body(environ, limit)
        if error:
            return None, error
        if not isinstance(body, dict) or not required <= set(body) <= required | optional:
            return None, _error(400, "invalid_request")
        return body, None

    def _passkeys_usable(self, request: Request) -> bool:
        """WebAuthn needs a domain RP ID, so passkeys are off on IP-literal hosts such as 127.0.0.1."""
        if self.passkeys is None:
            return False
        _, rp_id = self._webauthn_party(request.environ)
        try:
            ipaddress.ip_address(rp_id.strip("[]"))
        except ValueError:
            return True
        return False

    def passkey_status(self, request: Request) -> Result:
        available = self._passkeys_usable(request)
        return 200, {"available": available, "registered": available and self.passkeys.count() > 0}, []

    def passkey_login_options(self, request: Request) -> Result:
        if not self._passkeys_usable(request):
            return _error(503, "passkeys_unavailable")
        if (
            self.login_per_client.blocked(request.client)
            or self.login_global.blocked("all")
            or not self.passkey_options_limiter.hit(request.client)
        ):
            return _error(429, "rate_limited", [("Retry-After", "900")])
        if not self.passkeys.count():
            return _error(404, "no_passkeys")
        _, rp_id = self._webauthn_party(request.environ)
        challenge = self.passkeys.issue_challenge("login")
        return (
            200,
            {
                "challenge": passkeys.b64url_encode(challenge),
                "rp_id": rp_id,
                "timeout": passkeys.CHALLENGE_SECONDS * 1000,
                "user_verification": "required",
            },
            [],
        )

    def passkey_login(self, request: Request) -> Result:
        if not self._passkeys_usable(request):
            return _error(503, "passkeys_unavailable")
        fields = {"credential_id", "client_data_json", "authenticator_data", "signature"}
        body, error = self._passkey_body(request.environ, fields, {"remember"})
        if error:
            return error
        remember = body.get("remember", False)
        if not isinstance(remember, bool) or not all(isinstance(body[name], str) for name in fields):
            return _error(400, "invalid_request")
        reservation, error = self._reserve_attempt(request.client, "passkey_login")
        if error:
            return error
        origin, rp_id = self._webauthn_party(request.environ)
        try:
            credential_id = passkeys.b64url_encode(passkeys.b64url_decode(body["credential_id"]))
            client_data = passkeys.b64url_decode(body["client_data_json"])
            challenge = passkeys.client_data_challenge(client_data, kind="webauthn.get", origin=origin)
            if not self.passkeys.consume_challenge(challenge, "login"):
                raise passkeys.PasskeyError("challenge_expired")
            stored = self.passkeys.get(credential_id)
            if stored is None:
                raise passkeys.PasskeyError("unknown_credential")
            count = passkeys.verify_assertion(
                client_data_json=client_data,
                authenticator_data=passkeys.b64url_decode(body["authenticator_data"]),
                signature=passkeys.b64url_decode(body["signature"]),
                rp_id=rp_id,
                cose_key=stored[0],
                stored_count=stored[1],
            )
            if not self.passkeys.mark_used(credential_id, stored[1], count):
                raise passkeys.PasskeyError("counter_regressed")
        except passkeys.PasskeyError as failure:
            self.audit.record("passkey_login", "failure", client=request.client, reason=str(failure))
            return _error(401, "invalid_credentials")
        self.login_global.release("all", reservation)
        self.login_per_client.reset(request.client)
        return self._start_session(request, remember, "passkey")

    def _iso_or_none(self, instant: float | None) -> str | None:
        return None if instant is None else self._iso(instant)

    def passkey_list(self, request: Request) -> Result:
        if not self._passkeys_usable(request):
            return 200, {"available": False, "passkeys": []}, []
        items = [
            {
                **item,
                "created_at": self._iso(item["created_at"]),
                "last_used_at": self._iso_or_none(item["last_used_at"]),
            }
            for item in self.passkeys.list()
        ]
        return 200, {"available": True, "passkeys": items, "limit": passkeys.MAX_PASSKEYS}, []

    def passkey_register_options(self, request: Request) -> Result:
        if not self._passkeys_usable(request):
            return _error(503, "passkeys_unavailable")
        body, error = self._passkey_body(request.environ, {"password"}, set())
        if error:
            return error
        if not isinstance(body["password"], str):
            return _error(400, "invalid_request")
        generation = self.passkeys.generation()
        if self.sessions.get(request.token) is None:
            return _error(401, "authentication_required")
        # Re-entering the passphrase stops a stolen session cookie from adding a lasting credential.
        reservation, error = self._reserve_attempt(request.client, "passkey_register")
        if error:
            return error
        if not verify_password(body["password"], self.settings.password_hash):
            self.audit.record("passkey_register", "failure", actor="owner", client=request.client)
            return _error(401, "invalid_credentials")
        self.login_global.release("all", reservation)
        self.login_per_client.reset(request.client)
        _, rp_id = self._webauthn_party(request.environ)
        try:
            challenge = self.passkeys.issue_challenge(f"register:{request.session.token_hash}", generation=generation)
        except passkeys.PasskeyError:
            return _error(409, "passkey_not_added")
        return (
            200,
            {
                "challenge": passkeys.b64url_encode(challenge),
                "rp_id": rp_id,
                "rp_name": "Jarvis",
                "user_id": passkeys.b64url_encode(b"jarvis-owner"),
                "user_name": "jarvis",
                "user_display_name": "Jarvis",
                "exclude_credentials": self.passkeys.credential_ids(),
                "timeout": passkeys.CHALLENGE_SECONDS * 1000,
            },
            [],
        )

    def passkey_register(self, request: Request) -> Result:
        if not self._passkeys_usable(request):
            return _error(503, "passkeys_unavailable")
        body, error = self._passkey_body(
            request.environ, {"client_data_json", "attestation_object"}, {"label"}, limit=16 * 1024
        )
        if error:
            return error
        label = body.get("label", "Passkey")
        if not isinstance(label, str) or not all(
            isinstance(body[k], str) for k in ("client_data_json", "attestation_object")
        ):
            return _error(400, "invalid_request")
        label = " ".join(label.split())[: passkeys.MAX_LABEL] or "Passkey"
        origin, rp_id = self._webauthn_party(request.environ)
        # Capture before consuming the challenge: recovery deletes challenges and advances this fence.
        generation = self.passkeys.generation()
        try:
            client_data = passkeys.b64url_decode(body["client_data_json"])
            challenge = passkeys.client_data_challenge(client_data, kind="webauthn.create", origin=origin)
            if not self.passkeys.consume_challenge(challenge, f"register:{request.session.token_hash}"):
                raise passkeys.PasskeyError("challenge_expired")
            attestation = passkeys.b64url_decode(body["attestation_object"], limit=12 * 1024)
            credential_id, public_key, count = passkeys.verify_registration(attestation, rp_id)
        except passkeys.PasskeyError as failure:
            self.audit.record("passkey_register", "failure", actor="owner", client=request.client, reason=str(failure))
            return _error(400, "invalid_passkey")
        if not self.passkeys.add(credential_id, public_key, count, label, generation=generation):
            return _error(409, "passkey_not_added")
        self.audit.record("passkey_register", "success", actor="owner", client=request.client)
        return self.passkey_list(request)

    def passkey_remove(self, request: Request) -> Result:
        if not self._passkeys_usable(request):
            return _error(503, "passkeys_unavailable")
        body, error = self._passkey_body(request.environ, {"credential_id"}, set())
        if error:
            return error
        if not isinstance(body["credential_id"], str) or not self.passkeys.remove(body["credential_id"]):
            return _error(404, "not_found")
        self.audit.record("passkey_remove", "success", actor="owner", client=request.client)
        return self.passkey_list(request)

    def _session_payload(self, session: Session) -> dict:
        return {
            "authenticated": True,
            "permission": session.permission.label,
            "permission_levels": [p.label for p in Permission],
            "csrf_token": session.csrf_token,
            "expires_at": self._iso(self.sessions.expires_at(session)),
            "idle_timeout_seconds": None if session.remember else self.settings.session_idle_seconds,
            "remembered": session.remember,
        }

    def session_info(self, request: Request) -> Result:
        return 200, self._session_payload(request.session), []

    def logout(self, request: Request) -> Result:
        self.sessions.revoke(request.token)
        self.audit.record("logout", "success", actor="owner", client=request.client)
        return 200, {"authenticated": False}, [self._cookie("", 0)]

    def _source_signature(self) -> tuple:
        """Identity of every file the shared snapshot reads, including SQLite WAL files."""
        settings = self.settings
        signature: list[tuple[int, int, int] | None] = []
        for path in (
            settings.energy_db,
            settings.energy_status,
            settings.briefing_db,
            settings.briefing_reports,
            settings.energy_status.with_name("data.json"),
            settings.instagram_db,
        ):
            for candidate in (path, path.with_name(path.name + "-wal")) if path else ():
                try:
                    info = os.stat(candidate)
                except OSError:
                    signature.append(None)
                else:
                    signature.append((info.st_ino, info.st_mtime_ns, info.st_size))
        return tuple(signature)

    def _collect(self) -> dict:
        signature = self._source_signature()
        with self._collect_lock:
            cached = self._collected
            if cached and cached[0] == signature and 0 <= self.monotonic() - cached[1] < COLLECT_TTL_SECONDS:
                return cached[2]
            data = self._collect_fresh()
            self._collected = (signature, self.monotonic(), data)
            return data

    def _collect_fresh(self) -> dict:
        now = self._now()
        energy = sources.energy_snapshot(self.settings, now)
        briefing = sources.briefing_snapshot(self.settings)
        instagram = sources.instagram_drafts(self.settings)
        found = [*sources.integrations(self.settings, now, energy, briefing), sources.instagram_health(instagram, now)]
        health = {item["id"]: item for item in found}
        jobs = list_automations(self.settings, now, self.cron_reader, energy, briefing, health, instagram)
        history = sources.energy_history(self.settings, now, energy, insights.HISTORY_DAYS)
        published = sources.energy_dashboard(self.settings)["dashboard"] if history else None
        return {
            "now": now,
            "energy": energy,
            "energy_fuels": insights.energy_fuels(energy, history, published, now),
            "briefing": briefing,
            "instagram": instagram,
            "integrations": found,
            "automations": jobs,
        }

    def overview(self, request: Request) -> Result:
        data = self._collect()
        alerts = insights.attention(data, data["now"])
        instagram = next(item for item in data["integrations"] if item["id"] == "instagram")
        energy, fuels = data["energy"], data["energy_fuels"]
        return (
            200,
            {
                "generated_at": data["now"].isoformat(timespec="seconds"),
                "date": data["now"].date().isoformat(),
                "timezone": "Europe/London",
                "status": insights.system_status(alerts, data["automations"]),
                "attention": alerts,
                "insights": insights.energy_insights(energy, fuels),
                "briefing": {
                    "latest_run": data["briefing"]["runs"][0] if data["briefing"]["runs"] else None,
                    "latest_report_date": data["briefing"]["latest_report_date"],
                },
                "energy": {
                    "state": energy["state"],
                    "basis": energy["basis"],
                    "fuels": insights.public_fuels(fuels),
                    "last_successful_sync_at": energy["last_successful_sync_at"],
                    "actual_bills": energy["actual_bills"],
                },
                "automations": [{"id": j["id"], "name": j["name"], "status": j["status"]} for j in data["automations"]],
                "instagram": {
                    "status": instagram["status"],
                    "summary": instagram["summary"],
                    "pending_drafts": instagram["evidence"]["pending_drafts"],
                    "latest_attempt": instagram["evidence"]["latest_attempt"],
                    **insights.instagram_schedule(data["instagram"], data["now"]),
                },
            },
            [],
        )

    def _assistant_providers(self) -> list[tuple[str, cloudflare_client.CloudflareAI | openai_client.OpenAIClient]]:
        """Configured answer providers, free Workers AI first, OpenAI as the fallback."""
        settings, providers = self.settings, []
        if settings.cloudflare_account_id and settings.cloudflare_api_token:
            try:
                providers.append(
                    (
                        "cloudflare",
                        cloudflare_client.CloudflareAI(
                            settings.cloudflare_account_id, settings.cloudflare_api_token, timeout=30, retries=0
                        ),
                    )
                )
            except ValueError:
                pass
        if settings.openai_api_key:
            providers.append(
                (
                    "openai",
                    openai_client.OpenAIClient(
                        settings.openai_api_key,
                        text_model=settings.openai_model or openai_client.TEXT_MODEL,
                        timeout=30,
                        retries=0,
                    ),
                )
            )
        return providers

    def _notify_env(self) -> dict[str, str]:
        settings = self.settings
        env = {"COMMAND_CENTRE_NOTIFICATIONS_DB": str(settings.notifications_db)}
        if settings.telegram_bot_token and settings.telegram_chat_id:
            env |= {"TELEGRAM_BOT_TOKEN": settings.telegram_bot_token, "TELEGRAM_CHAT_ID": settings.telegram_chat_id}
        return env

    def _assistant_failed(self, reason: str, title: str) -> None:
        """Tell the owner the website Jarvis could not answer, at most once an hour per reason."""
        if self.settings.notifications_db is None:
            return
        hour = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%dT%H")
        env = self._notify_env()
        link = notifications.command_centre_link(env, "settings")
        self.notify("jarvis", "failed", title, link, key=f"assistant:{reason}:{hour}", env=env)

    def assistant_status(self, request: Request) -> Result:
        return 200, {"available": bool(self._assistant_providers())}, []

    def ask_assistant(self, request: Request) -> Result:
        providers = self._assistant_providers()
        if not providers:
            self._assistant_failed("not_configured", "Website Jarvis can't answer: no AI provider is configured")
            return _error(503, "assistant_not_configured")
        body, error = _json_body(request.environ)
        if error:
            return error
        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, str) or set(body) != {"question"}:
            return _error(400, "invalid_request")
        return self._answer_question(request, question, providers=providers)

    def _answer_question(self, request: Request, question: str, *, actor: str = "owner", providers=None) -> Result:
        providers = self._assistant_providers() if providers is None else providers
        if not providers:
            self.audit.record("assistant", "not_configured", actor=actor, client=request.client)
            return _error(503, "assistant_not_configured")
        question = " ".join(question.split())
        if not 0 < len(question) <= assistant.MAX_QUESTION:
            return _error(400, "invalid_request")
        if not self.assistant_limiter.hit("all"):
            self.audit.record("assistant", "rate_limited", actor=actor, client=request.client)
            if actor == "owner":
                self._assistant_failed("rate_limited", "Website Jarvis hit its limit of 30 questions an hour")
            return _error(429, "rate_limited", [("Retry-After", "600")])
        data = self._collect()
        context = assistant.build_context(
            now=data["now"], data=data, instagram=sources.instagram_drafts(self.settings), agents=sources.agents()
        )
        message = assistant.user_message(question, context)
        for name, client in providers:
            try:
                answer = client.chat_text(assistant.SYSTEM_PROMPT, message, max_tokens=assistant.MAX_ANSWER_TOKENS)
            except openai_client.AIError:
                self.audit.record("assistant", "failure", actor=actor, client=request.client, provider=name)
                continue
            # The question and answer are owner content, so only the outcome is audited.
            self.audit.record("assistant", "success", actor=actor, client=request.client, provider=name)
            return (
                200,
                {"answer": answer[:2000], "model": client.text_model, "generated_at": self._iso(self.clock())},
                [],
            )
        if actor == "owner":
            self._assistant_failed("unavailable", "Website Jarvis couldn't answer: every AI provider failed")
        return _error(502, "assistant_unavailable")

    def telegram_webhook(self, request: Request) -> Result:
        def audit(outcome: str) -> None:
            self.audit.record("telegram_webhook", outcome, actor="telegram", client=request.client)

        settings = self.settings
        if not self.ingest_limiter.hit(request.client):
            audit("rate_limited")
            return _error(429, "rate_limited")
        if not all((settings.telegram_webhook_secret, settings.telegram_bot_token, settings.telegram_chat_id)):
            audit("not_configured")
            return _error(503, "not_configured")
        supplied = request.environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
        if not hmac.compare_digest(supplied.encode(), settings.telegram_webhook_secret.encode()):
            audit("unauthorized")
            return _error(401, "authentication_required")
        body, error = _json_body(request.environ, limit=MAX_TELEGRAM_BODY_BYTES)
        message = body.get("message") if isinstance(body, dict) else None
        message = message if isinstance(message, dict) else {}
        chat, sender = message.get("chat"), message.get("from")
        update_id = body.get("update_id") if isinstance(body, dict) else None
        if (
            error
            or type(update_id) is not int
            or not isinstance(chat, dict)
            or not isinstance(sender, dict)
            or chat.get("type") != "private"
            or str(chat.get("id")) != settings.telegram_chat_id
            or str(sender.get("id")) != settings.telegram_chat_id
            or sender.get("is_bot") is not False
            or not isinstance(message.get("text"), str)
        ):
            audit("ignored")
            return 200, {}, []
        try:
            with closing(sqlite3.connect(settings.audit_db)) as conn, conn:
                conn.execute("CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY)")
                inserted = conn.execute("INSERT OR IGNORE INTO telegram_updates VALUES (?)", (update_id,)).rowcount
        except sqlite3.Error:
            audit("storage_failure")
            return _error(503, "unavailable")
        if not inserted:
            audit("duplicate")
            return 200, {}, []
        apology = "Sorry, I can't answer right now. Please try again later."
        try:
            status, payload, _ = self._answer_question(request, message["text"], actor="telegram")
            answer = payload["answer"] if status == 200 else apology
        except Exception:
            audit("answer_failure")
            answer = apology
        try:
            notifications.send_telegram(settings.telegram_bot_token, settings.telegram_chat_id, answer)
        except Exception:
            audit("delivery_failure")
        else:
            audit("success")
        return 200, {}, []

    def modules(self, request: Request) -> Result:
        data = self._collect()
        return 200, {"modules": sources.modules(data["integrations"])}, []

    def agents(self, request: Request) -> Result:
        now = self._now()
        instagram = sources.instagram_health(sources.instagram_drafts(self.settings), now)
        activity = {
            "instagram": {
                "status": instagram["status"],
                "summary": instagram["summary"],
                "pending_drafts": instagram["evidence"]["pending_drafts"],
                "latest_run": instagram["evidence"]["latest_attempt"],
                "href": "#instagram",
            }
        }
        return 200, {"agents": [{**agent, "activity": activity.get(agent["id"])} for agent in sources.agents()]}, []

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

    def energy_dashboard(self, request: Request) -> Result:
        return 200, sources.energy_dashboard(self.settings), []

    def energy_refresh_status(self, request: Request) -> Result:
        state, status = sources.energy_refresh_status(self.settings)
        return 200, {"state": state, "status": status}, []

    def energy_refresh(self, request: Request) -> Result:
        """Queue a background energy sync: the dashboard's request flag, never the sync itself."""
        code, body, headers = energy_refresh.request_refresh(self.settings, self._now())
        self.audit.record("energy_refresh", body["state"], actor="owner", client=request.client)
        return code, body, headers

    def briefing_runs(self, request: Request) -> Result:
        raw = request.query("limit") or "14"
        if not raw.isdigit() or not 1 <= int(raw) <= 100:
            return _error(400, "invalid_parameters")
        snapshot = sources.briefing_snapshot(self.settings, int(raw))
        return 200, snapshot, []

    def _audit_action(self, request: Request) -> tuple[str | None, bool]:
        action = request.query("action") or None
        return action, action is None or bool(AUDIT_ACTION.fullmatch(action))

    def audit_log(self, request: Request) -> Result:
        page, per_page = request.query("page") or "1", request.query("per_page") or "50"
        action, valid = self._audit_action(request)
        if not valid or not PAGE_NUMBER.fullmatch(page) or not PAGE_NUMBER.fullmatch(per_page):
            return _error(400, "invalid_parameters")
        if int(per_page) not in AUDIT_PAGE_SIZES:
            return _error(400, "invalid_parameters")
        page, per_page = int(page), int(per_page)
        if not 1 <= page <= 100_000:
            return _error(400, "invalid_parameters")
        entries, total = self.audit.page(limit=per_page, offset=(page - 1) * per_page, action=action)
        return (
            200,
            {
                "entries": entries,
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": max(1, -(-total // per_page)),
                "action": action,
                "actions": self.audit.actions(),
                "page_sizes": list(AUDIT_PAGE_SIZES),
            },
            [],
        )

    def audit_export(self, request: Request) -> Result:
        """Download the audit log (newest first) as CSV or JSON. It holds operational metadata only."""
        kind = request.query("format")
        action, valid = self._audit_action(request)
        if kind not in ("csv", "json") or not valid:
            return _error(400, "invalid_parameters")
        entries, total = self.audit.page(limit=AUDIT_EXPORT_LIMIT, action=action)
        now = self.clock()
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%MZ")
        if kind == "json":
            body = json.dumps(
                {
                    "exported_at": self._iso(now),
                    "action": action,
                    "total": total,
                    "count": len(entries),
                    "truncated": total > len(entries),
                    "entries": entries,
                },
                indent=2,
            ).encode()
            content_type = "application/json"
        else:
            stream = io.StringIO()
            writer = csv.writer(stream, lineterminator="\r\n")
            writer.writerow(["id", "at", "actor", "action", "outcome", "detail"])
            for entry in entries:
                detail = json.dumps(entry["detail"], sort_keys=True, separators=(",", ":"))
                writer.writerow(
                    [csv_cell(entry[k]) for k in ("id", "at", "actor", "action", "outcome")] + [csv_cell(detail)]
                )
            body, content_type = stream.getvalue().encode(), "text/csv; charset=utf-8"
        self.audit.record(
            "audit_export", "success", actor="owner", client=request.client, format=kind, rows=len(entries)
        )
        name = f"jarvis-audit-{stamp}.{kind}"
        return 200, (body, content_type), [("Content-Disposition", f'attachment; filename="{name}"')]

    def _session_entry(self, session: Session, current: Session) -> dict:
        return {
            "id": session.public_id,
            "current": session.token_hash == current.token_hash,
            "device": session.device or "Unknown device",
            "client": session.client or None,
            "signed_in_at": self._iso(session.created_at),
            "last_active_at": self._iso(session.last_seen_at),
            "expires_at": self._iso(self.sessions.expires_at(session)),
            "remembered": session.remember,
        }

    def session_list(self, request: Request) -> Result:
        found = sorted(self.sessions.active(), key=lambda s: s.last_seen_at, reverse=True)
        return (
            200,
            {
                "sessions": [self._session_entry(s, request.session) for s in found],
                "sign_in_activity": {
                    **self.audit.sign_in_activity(self.clock() - 24 * 60 * 60),
                    "failed_window_hours": 24,
                },
            },
            [],
        )

    def session_revoke(self, request: Request) -> Result:
        """Sign out one other session (`{"id"}`) or every other one (`{"others": true}`). Use logout for this one."""
        body, error = _json_body(request.environ)
        if error:
            return error
        if not isinstance(body, dict) or len(body) != 1:
            return _error(400, "invalid_request")
        current = request.session.token_hash
        if body.get("others") is True:
            targets = [s for s in self.sessions.active() if s.token_hash != current]
        elif isinstance(body.get("id"), str) and SESSION_ID.fullmatch(body["id"]):
            targets = [s for s in self.sessions.active() if s.public_id == body["id"]]
            if not targets:
                return _error(404, "not_found")
            if targets[0].token_hash == current:
                return _error(400, "current_session")
        else:
            return _error(400, "invalid_request")
        revoked = sum(self.sessions.revoke_hash(s.token_hash) for s in targets)
        self.audit.record(
            "session_revoke",
            "success",
            actor="owner",
            client=request.client,
            scope="others" if "others" in body else "one",
            revoked=revoked,
        )
        return self.session_list(request)

    def notifications(self, request: Request) -> Result:
        """The owner inbox from `notifications.notify`, newest first."""
        page, per_page = request.query("page") or "1", request.query("per_page") or "25"
        if not PAGE_NUMBER.fullmatch(page) or not PAGE_NUMBER.fullmatch(per_page):
            return _error(400, "invalid_parameters")
        page, per_page = int(page), int(per_page)
        if per_page not in AUDIT_PAGE_SIZES or not 1 <= page <= 100_000:
            return _error(400, "invalid_parameters")
        result = {"state": "not_configured", "entries": [], "page": page, "per_page": per_page, "total": 0, "pages": 1}
        path = self.settings.notifications_db
        if path is None:
            return 200, result, []
        try:
            with closing(sources.connect_read_only(path)) as conn:
                entries, total = notifications.recent(conn, per_page, (page - 1) * per_page)
        except FileNotFoundError:
            return 200, result, []
        except sqlite3.Error:
            return 200, {**result, "state": "unavailable"}, []
        pages = max(1, -(-total // per_page))
        return 200, {**result, "state": "available", "entries": entries, "total": total, "pages": pages}, []

    def _ingest_authorized(self, environ: dict) -> bool:
        scheme, _, supplied = environ.get("HTTP_AUTHORIZATION", "").partition(" ")
        expected = self.settings.notify_ingest_token or ""
        # Digests have a fixed length, so the comparison time says nothing about the token.
        return (
            scheme.lower() == "bearer"
            and hmac.compare_digest(
                hashlib.sha256(supplied.strip().encode()).digest(), hashlib.sha256(expected.encode()).digest()
            )
            and bool(expected)
        )

    def notification_ingest(self, request: Request) -> Result:
        """Store an agent result (Jarvis) in the owner inbox and push it to Telegram. Bearer token only."""

        def audit(outcome: str, actor: str = "anonymous", **detail: object) -> None:
            # Never the title, link or token: only what happened.
            self.audit.record("notification_ingest", outcome, actor=actor, client=request.client, **detail)

        settings = self.settings
        # Throttle first so no branch, including not_configured, does unbounded work per request.
        if not self.ingest_limiter.hit(request.client):
            audit("rate_limited")
            return _error(429, "rate_limited", [("Retry-After", "600")])
        if not settings.notify_ingest_token or settings.notifications_db is None:
            audit("not_configured")
            return _error(503, "not_configured")
        if not self._ingest_authorized(request.environ):
            audit("unauthorized")
            return _error(401, "authentication_required", [("WWW-Authenticate", "Bearer")])
        body, error = _json_body(request.environ)
        if error and error[0] == 413:
            audit("too_large", actor="ingest")
            return error
        source = body.get("source") if isinstance(body, dict) else None
        link = body.get("link") if isinstance(body, dict) else None
        title = body.get("title") if isinstance(body, dict) else None
        if (
            error
            or not isinstance(body, dict)
            or not {"source", "outcome", "title"} <= set(body) <= INGEST_FIELDS
            or source not in INGEST_SOURCES
            or body["outcome"] not in INGEST_OUTCOMES
            or not isinstance(title, str)
            or not 0 < len(title) <= MAX_INGEST_TITLE
            or not title.strip()
            or (link is not None and notifications.safe_link(link) is None)
        ):
            audit("invalid", actor="ingest")
            return _error(400, "invalid_parameters")
        stored = self.notify(source, body["outcome"], title, link, env=self._notify_env())
        if not stored:
            audit("failure", actor=source, result=body["outcome"])
            return _error(500, "internal_error")
        audit("success", actor=source, result=body["outcome"], notification=stored)
        return 201, {"id": stored}, []

    def instagram_drafts(self, request: Request) -> Result:
        return 200, {**sources.instagram_drafts(self.settings), "uploads_on": instagram_uploads.available()}, []

    def _instagram_ready(self) -> bool:
        path, media = self.settings.instagram_db, self.settings.instagram_media
        return bool(path and media and path.is_file() and not path.is_symlink() and not media.is_symlink())

    def instagram_upload(self, request: Request) -> Result:
        """Add the owner's photos and note to the queue; the daily run turns the oldest into a draft."""
        if not self._instagram_ready():
            return _error(404, "not_found")
        if not instagram_uploads.available():
            return _error(503, "uploads_unavailable")
        form, error = _photo_form(request.environ)
        if error:
            return error
        options = form[2]
        if options.get("reel", "0") not in ("0", "1"):
            return _error(400, "invalid_request")
        as_reel = options.get("reel") == "1"
        conn = sqlite3.connect(self.settings.instagram_db, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            try:
                note = instagram_uploads.clean_note(form[0])
                picked = instagram_music.choice(options.get("music"), options.get("mix", "replace"), conn)
                prepared = instagram_uploads.prepare(form[1])
            except (instagram_uploads.PhotoError, instagram_music.MusicError) as exc:
                self.audit.record("instagram_upload", "refused", actor="owner", reason=exc.code, client=request.client)
                return _error(413 if exc.code == "photo_too_large" else 400, exc.code)
            upload_id = instagram_uploads.save(
                conn, self.settings.instagram_media, note, prepared, self._now(), reel=as_reel, music=picked
            )
        finally:
            conn.close()
        with self._collect_lock:
            self._collected = None
        self.audit.record(
            "instagram_upload",
            "saved",
            actor="owner",
            upload=upload_id,
            photos=len(prepared),
            reel=as_reel,
            music=(picked or {}).get("pick", "off") if as_reel else "off",
            client=request.client,
        )
        return 200, {"upload_id": upload_id, "photos": len(prepared), "reel": as_reel}, []

    def instagram_music_add(self, request: Request) -> Result:
        """Add a track the owner has the rights to, with its licence and source, to the reel music library."""
        if not self._instagram_ready():
            return _error(404, "not_found")
        parts, error = _form_parts(request.environ, instagram_music.MAX_TRACK_BYTES + 64 * 1024)
        if error:
            return error
        found = _fields(parts, "track", {"title", "licence", "source"})
        if found is None or len(found[1]) != 1:
            return _error(400, "invalid_request")
        fields, [data] = found
        conn = sqlite3.connect(self.settings.instagram_db, timeout=5)
        try:
            track_id = instagram_music.save(
                conn,
                self.settings.instagram_media,
                fields.get("title"),
                fields.get("licence"),
                fields.get("source", ""),
                data,
                self._now(),
            )
        except instagram_music.MusicError as exc:
            self.audit.record("instagram_music", "refused", actor="owner", reason=exc.code, client=request.client)
            return _error(413 if exc.code == "track_too_large" else 400, exc.code)
        except OSError:
            return _error(507, "storage_error")
        finally:
            conn.close()
        with self._collect_lock:
            self._collected = None
        self.audit.record("instagram_music", "saved", actor="owner", track=track_id, client=request.client)
        return 200, {"track_id": track_id}, []

    def instagram_music_delete(self, request: Request) -> Result:
        body, error = _json_body(request.environ)
        if error:
            return error
        if not isinstance(body, dict) or set(body) != {"track_id"} or not isinstance(body["track_id"], str):
            return _error(400, "invalid_request")
        if not self._instagram_ready():
            return _error(404, "not_found")
        conn = sqlite3.connect(self.settings.instagram_db, timeout=5)
        try:
            deleted = instagram_music.delete(conn, self.settings.instagram_media, body["track_id"])
        finally:
            conn.close()
        if not deleted:
            return _error(404, "not_found")
        with self._collect_lock:
            self._collected = None
        self.audit.record("instagram_music", "deleted", actor="owner", track=body["track_id"], client=request.client)
        return 200, {"track_id": body["track_id"], "deleted": True}, []

    def instagram_upload_delete(self, request: Request) -> Result:
        body, error = _json_body(request.environ)
        if error:
            return error
        if (
            not isinstance(body, dict)
            or set(body) != {"upload_id"}
            or not isinstance(body["upload_id"], str)
            or not instagram_uploads.UPLOAD_ID.fullmatch(body["upload_id"])
        ):
            return _error(400, "invalid_request")
        if not self._instagram_ready():
            return _error(404, "not_found")
        conn = sqlite3.connect(self.settings.instagram_db, timeout=5)
        try:
            deleted = instagram_uploads.delete(conn, self.settings.instagram_media, body["upload_id"])
        except sqlite3.OperationalError:
            deleted = False  # no uploads table yet
        finally:
            conn.close()
        if not deleted:
            return _error(404, "not_found")
        with self._collect_lock:
            self._collected = None
        self.audit.record("instagram_upload", "deleted", actor="owner", upload=body["upload_id"], client=request.client)
        return 200, {"upload_id": body["upload_id"], "deleted": True}, []

    def _clip_body(
        self, request: Request, keys: set[str], optional: frozenset[str] = frozenset()
    ) -> tuple[dict | None, Result | None]:
        body, error = _json_body(request.environ)
        if error:
            return None, error
        if not isinstance(body, dict) or not keys <= set(body) <= keys | optional:
            return None, _error(400, "invalid_request")
        if "clip_id" in keys and not (
            isinstance(body["clip_id"], str) and instagram_uploads.UPLOAD_ID.fullmatch(body["clip_id"])
        ):
            return None, _error(400, "invalid_request")
        if not self._instagram_ready():
            return None, _error(404, "not_found")
        return body, None

    def _clip_call(self, call: Callable[[sqlite3.Connection], object]):
        conn = sqlite3.connect(self.settings.instagram_db, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return call(conn)
        finally:
            conn.close()

    def instagram_clip_start(self, request: Request) -> Result:
        """Start a clip upload: the note and the size; the browser then sends the bytes in chunks."""
        body, error = self._clip_body(request, {"note", "size"}, frozenset({"music", "mix"}))
        if error:
            return error
        try:
            note = instagram_uploads.clean_note(body["note"])
            media, now = self.settings.instagram_media, self._now()

            def start(conn):
                picked = instagram_music.choice(body.get("music"), body.get("mix", "under"), conn)
                return instagram_clips.start(conn, media, note, body["size"], now, picked)

            clip_id = self._clip_call(start)
        except (instagram_uploads.PhotoError, instagram_clips.ClipError, instagram_music.MusicError) as exc:
            return _error(413 if exc.code == "clip_too_large" else 400, exc.code)
        except OSError:
            return _error(507, "storage_error")
        return 200, {"clip_id": clip_id, "chunk_bytes": instagram_clips.CHUNK_BYTES}, []

    def instagram_clip_chunk(self, request: Request) -> Result:
        clip_id, offset = request.query("clip") or "", request.query("offset") or ""
        if not instagram_uploads.UPLOAD_ID.fullmatch(clip_id) or not offset.isdigit() or len(offset) > 12:
            return _error(400, "invalid_parameters")
        if request.environ.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/octet-stream":
            return _error(415, "octet_stream_required")
        try:
            length = int(request.environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return _error(400, "invalid_request")
        if not self._instagram_ready():
            return _error(404, "not_found")
        stream, media = request.environ["wsgi.input"], self.settings.instagram_media
        try:
            received = self._clip_call(lambda c: instagram_clips.append(c, media, clip_id, int(offset), stream, length))
        except instagram_clips.ClipError as exc:
            return _error(404 if exc.code == "not_found" else 409 if exc.code == "wrong_offset" else 400, exc.code)
        except FileNotFoundError:
            return _error(404, "not_found")
        except OSError:
            return _error(507, "storage_error")
        return 200, {"clip_id": clip_id, "received": received}, []

    def instagram_clip_finish(self, request: Request) -> Result:
        """Check the whole clip against Meta's spec; a clip that fails is dropped with the reasons."""
        body, error = self._clip_body(request, {"clip_id"})
        if error:
            return error
        media = self.settings.instagram_media
        try:
            facts = self._clip_call(lambda c: instagram_clips.finish(c, media, body["clip_id"]))
        except instagram_clips.ClipError as exc:
            if exc.code == "clip_rejected":
                self.audit.record(
                    "instagram_clip", "refused", actor="owner", clip=body["clip_id"], client=request.client
                )
                return 422, {"error": exc.code, "problems": exc.problems}, []
            return _error(404 if exc.code == "not_found" else 409, exc.code)
        except OSError:
            return _error(507, "storage_error")
        with self._collect_lock:
            self._collected = None
        self.audit.record(
            "instagram_clip",
            "saved",
            actor="owner",
            clip=body["clip_id"],
            seconds=facts.get("seconds"),
            client=request.client,
        )
        return 200, {"clip_id": body["clip_id"], "seconds": facts.get("seconds")}, []

    def instagram_clip_delete(self, request: Request) -> Result:
        body, error = self._clip_body(request, {"clip_id"})
        if error:
            return error
        media = self.settings.instagram_media
        if not self._clip_call(lambda c: instagram_clips.delete(c, media, body["clip_id"])):
            return _error(404, "not_found")
        with self._collect_lock:
            self._collected = None
        self.audit.record("instagram_clip", "deleted", actor="owner", clip=body["clip_id"], client=request.client)
        return 200, {"clip_id": body["clip_id"], "deleted": True}, []

    def instagram_upload_photo(self, request: Request) -> Result:
        upload, number = request.query("upload") or "", request.query("n") or ""
        if not instagram_uploads.UPLOAD_ID.fullmatch(upload) or not number.isdigit():
            return _error(400, "invalid_parameters")
        if not 1 <= int(number) <= instagram_uploads.MAX_PHOTOS:
            return _error(400, "invalid_parameters")
        media = self.settings.instagram_media
        if media is None or media.is_symlink() or (media / instagram_uploads.FOLDER).is_symlink():
            return _error(404, "not_found")
        try:
            return 200, (instagram_uploads.read_photo(media, upload, f"thumb-{int(number)}.jpg"), "image/jpeg"), []
        except OSError:
            return _error(404, "not_found")

    def instagram_slide(self, request: Request) -> Result:
        draft, number = request.query("draft") or "", request.query("n") or ""
        if not sources.DRAFT_ID.fullmatch(draft) or not number.isdigit() or not 1 <= int(number) <= 10:
            return _error(400, "invalid_parameters")
        media = self.settings.instagram_media
        if media is None or media.is_symlink() or (media / draft).is_symlink():
            return _error(404, "not_found")
        for extension, content_type in (("png", "image/png"), ("jpg", "image/jpeg")):
            path = media / draft / f"slide-{int(number)}.{extension}"
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                continue
            with os.fdopen(fd, "rb") as stream:
                if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return 200, (stream.read(), content_type), []
        return _error(404, "not_found")

    def instagram_hold(self, request: Request) -> Result:
        """Hold a pending reel for the owner's approval, or let it post automatically if it passes review."""
        body, error = _json_body(request.environ)
        if error:
            return error
        if (
            not isinstance(body, dict)
            or set(body) != {"draft_id", "hold", "version"}
            or type(body["version"]) is not int
            or not 0 <= body["version"] < 2**31
            or not isinstance(body["draft_id"], str)
            or not sources.DRAFT_ID.fullmatch(body["draft_id"])
            or not isinstance(body["hold"], bool)
        ):
            return _error(400, "invalid_request")
        path = self.settings.instagram_db
        if path is None or path.is_symlink() or not path.is_file():
            return _error(404, "not_found")
        draft_id = body["draft_id"]
        conn = sqlite3.connect(path, timeout=5)
        try:
            outcome = instagram_storage.hold_reel(conn, draft_id, body["hold"], body["version"])
        finally:
            conn.close()
        if outcome == "not_found":
            return _error(404, "not_found")
        if outcome == "hold_changed":
            return _error(409, "hold_changed")
        if outcome == "not_pending":
            self.audit.record("instagram_hold", "not_pending", actor="owner", draft=draft_id)
            return _error(409, "already_decided")
        if outcome == "unchanged":
            return 200, {"draft_id": draft_id, "hold": body["hold"]}, []
        with self._collect_lock:
            self._collected = None
        state = "held" if body["hold"] else "released"
        self.audit.record("instagram_hold", state, actor="owner", draft=draft_id, client=request.client)
        return 200, {"draft_id": draft_id, "hold": body["hold"]}, []

    def instagram_decision(self, request: Request) -> Result:
        """Approve or reject a pending draft. Reject stops it posting; Approve lets a weaker draft post at its slot."""
        body, error = _json_body(request.environ)
        if error:
            return error
        if (
            not isinstance(body, dict)
            or set(body) != {"draft_id", "decision"}
            or not isinstance(body["draft_id"], str)
            or not sources.DRAFT_ID.fullmatch(body["draft_id"])
            or body["decision"] not in ("approve", "reject")
        ):
            return _error(400, "invalid_request")
        path = self.settings.instagram_db
        if path is None or path.is_symlink() or not path.is_file():
            return _error(404, "not_found")
        draft_id, decision = body["draft_id"], "approved" if body["decision"] == "approve" else "rejected"
        conn = sqlite3.connect(path, timeout=5)
        try:
            changed = instagram_storage.decide_draft(
                conn, draft_id, decision, self._now().isoformat(timespec="seconds")
            )
            exists = changed or conn.execute("SELECT 1 FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
            publishing = not changed and exists and instagram_storage.publishing_started(conn, draft_id)
        finally:
            conn.close()
        if not exists:
            return _error(404, "not_found")
        if publishing:
            self.audit.record("instagram_decision", "publishing", actor="owner", draft=draft_id)
            return _error(409, "publishing")
        if not changed:
            self.audit.record("instagram_decision", "already_decided", actor="owner", draft=draft_id)
            return _error(409, "already_decided")
        with self._collect_lock:
            self._collected = None
        self.audit.record("instagram_decision", decision, actor="owner", draft=draft_id, client=request.client)
        return 200, {"draft_id": draft_id, "status": decision}, []
