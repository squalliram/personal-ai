"""WSGI application: authentication, CSRF, permissions, rate limits and the read-only JSON API."""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import sqlite3
import stat
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

from instagram_agent import storage as instagram_storage

from . import passkeys, sources
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
        self.passkey_options_limiter = RateLimiter(20, 15 * 60, clock=monotonic)
        if settings.sessions_db:
            self.login_per_client = SqliteRateLimiter(settings.sessions_db, "login_client", 5, 900, clock=clock)
            self.login_global = SqliteRateLimiter(settings.sessions_db, "login_global", 20, 900, clock=clock)
            self.api_limiter = SqliteRateLimiter(settings.sessions_db, "api", 240, 60, clock=clock)
            self.passkey_options_limiter = SqliteRateLimiter(
                settings.sessions_db, "passkey_options", 20, 900, clock=clock
            )
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
            ("GET", "/api/v1/modules"): Route(self.modules, Permission.READ),
            ("GET", "/api/v1/agents"): Route(self.agents, Permission.READ),
            ("GET", "/api/v1/integrations"): Route(self.integrations, Permission.READ),
            ("GET", "/api/v1/automations"): Route(self.automations, Permission.READ),
            ("GET", "/api/v1/energy"): Route(self.energy, Permission.READ),
            ("GET", "/api/v1/briefing/runs"): Route(self.briefing_runs, Permission.READ),
            ("GET", "/api/v1/audit"): Route(self.audit_log, Permission.READ),
            ("GET", "/api/v1/instagram/drafts"): Route(self.instagram_drafts, Permission.READ),
            ("GET", "/api/v1/instagram/slide"): Route(self.instagram_slide, Permission.READ),
            ("POST", "/api/v1/instagram/decision"): Route(self.instagram_decision, Permission.RECOMMEND, csrf=True),
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
        token, session = self.sessions.create(self.settings.permission_ceiling, remember=remember)
        self.audit.record(
            "login",
            "success",
            actor="owner",
            client=request.client,
            permission=session.permission.label,
            remembered=session.remember,
            method=method,
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
        challenge = self.passkeys.issue_challenge(f"register:{request.session.token_hash}")
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
        if not self.passkeys.add(credential_id, public_key, count, label):
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

    def _collect(self) -> dict:
        now = self._now()
        energy = sources.energy_snapshot(self.settings, now)
        briefing = sources.briefing_snapshot(self.settings)
        instagram = sources.instagram_drafts(self.settings)
        found = [*sources.integrations(self.settings, now, energy, briefing), sources.instagram_health(instagram, now)]
        health = {item["id"]: item for item in found}
        jobs = list_automations(self.settings, now, self.cron_reader, energy, briefing, health, instagram)
        return {
            "now": now,
            "energy": energy,
            "briefing": briefing,
            "instagram": instagram,
            "integrations": found,
            "automations": jobs,
        }

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
            if job["status"] not in ("operational",) and job["id"] not in ("daily-briefing", "instagram-daily")
        ]
        instagram = next(item for item in data["integrations"] if item["id"] == "instagram")
        pending = instagram["evidence"]["pending_drafts"]
        if pending:
            alerts.append(
                {
                    "severity": "action",
                    "kind": "action",
                    "source": "instagram",
                    "fact": f"Review {pending} Instagram draft{'s' if pending != 1 else ''} awaiting your approval.",
                    "href": "#instagram",
                    "evidence": {"pending_drafts": pending},
                }
            )
        order = {"critical": 0, "warning": 1, "action": 2, "info": 3}
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
                "instagram": {
                    "status": instagram["status"],
                    "summary": instagram["summary"],
                    "pending_drafts": pending,
                    "latest_attempt": instagram["evidence"]["latest_attempt"],
                },
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

    def briefing_runs(self, request: Request) -> Result:
        raw = request.query("limit") or "14"
        if not raw.isdigit() or not 1 <= int(raw) <= 100:
            return _error(400, "invalid_parameters")
        snapshot = sources.briefing_snapshot(self.settings, int(raw))
        return 200, snapshot, []

    def audit_log(self, request: Request) -> Result:
        return 200, {"entries": self.audit.recent(50)}, []

    def instagram_drafts(self, request: Request) -> Result:
        return 200, sources.instagram_drafts(self.settings), []

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

    def instagram_decision(self, request: Request) -> Result:
        """Approve or reject a pending draft. This only records the decision; it never posts to Instagram."""
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
        finally:
            conn.close()
        if not exists:
            return _error(404, "not_found")
        if not changed:
            self.audit.record("instagram_decision", "already_decided", actor="owner", draft=draft_id)
            return _error(409, "already_decided")
        self.audit.record("instagram_decision", decision, actor="owner", draft=draft_id, client=request.client)
        return 200, {"draft_id": draft_id, "status": decision}, []
