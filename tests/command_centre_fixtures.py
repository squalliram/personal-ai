"""Shared helpers for command centre tests (not a test module)."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path

from command_centre.app import CommandCentreApp
from command_centre.config import Settings
from command_centre.security import Permission, hash_password
from octopus_energy.analytics import recompute_daily_costs
from octopus_energy.storage import connect, upsert_consumption, upsert_meter, upsert_rate

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD, iterations=1000)
HOST = "127.0.0.1:8770"
ORIGIN = f"http://{HOST}"
ROOT = Path(__file__).resolve().parents[1]


def make_settings(root: Path, **overrides) -> Settings:
    settings = Settings(
        root=root,
        energy_db=root / "energy.sqlite3",
        energy_status=root / "status.json",
        briefing_db=root / "briefing.sqlite3",
        briefing_reports=root / "daily",
        audit_db=root / "audit.sqlite3",
        password_hash=PASSWORD_HASH,
        permission_ceiling=Permission.RECOMMEND,
        inspect_crontab=True,
    )
    return replace(settings, **overrides)


class Clock:
    def __init__(self, start: float | None = None) -> None:
        self.now = start if start is not None else datetime.now(timezone.utc).timestamp()

    def __call__(self) -> float:
        return self.now


@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)

    def headers_named(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def json(self):
        return json.loads(self.body)


@dataclass
class Client:
    app: CommandCentreApp
    host: str = HOST
    cookies: dict[str, str] = field(default_factory=dict)
    csrf: str | None = None

    def request(
        self,
        method,
        path,
        *,
        body=None,
        raw=None,
        headers=None,
        query="",
        origin=True,
        content_type=None,
        scheme=None,
        script_name="",
    ):
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "SCRIPT_NAME": script_name,
            "QUERY_STRING": query,
            "REMOTE_ADDR": "127.0.0.1",
            "CONTENT_LENGTH": str(len(data)),
            "CONTENT_TYPE": content_type or ("application/json" if data else ""),
            "wsgi.url_scheme": scheme or ("https" if getattr(self.app, "secure", False) else "http"),
            "wsgi.input": io.BytesIO(data),
        }
        if self.host is not None:
            environ["HTTP_HOST"] = self.host
        if origin and method not in ("GET", "HEAD"):
            environ["HTTP_ORIGIN"] = ORIGIN if origin is True else origin
        if self.cookies:
            environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        for key, value in (headers or {}).items():
            environ["HTTP_" + key.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers):
            captured["status"], captured["headers"] = int(status.split()[0]), response_headers

        body_bytes = b"".join(self.app(environ, start_response))
        response = Response(captured["status"], captured["headers"], body_bytes)
        for value in response.headers_named("Set-Cookie"):
            cookie = SimpleCookie(value)
            for name, morsel in cookie.items():
                if morsel["max-age"] == "0":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        return response

    def login(self, password=PASSWORD):
        response = self.request("POST", "/api/v1/session", body={"password": password})
        if response.status == 200:
            self.csrf = response.json()["csrf_token"]
        return response


def make_app(settings: Settings, *, clock=None, cron=("absent", ""), secure=False) -> CommandCentreApp:
    clock = clock or Clock()
    return CommandCentreApp(
        settings, allowed_hosts={HOST}, secure=secure, clock=clock, monotonic=clock, cron_reader=lambda: cron
    )


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def seed_energy(path: Path, *, sync_status="success", sync_age=timedelta(hours=1), days=10) -> None:
    """Current-schema database with complete electricity coverage for recent past days."""
    conn = connect(path)
    meter = upsert_meter(
        conn,
        fuel="electricity",
        point_id="MPAN-SECRET-POINT",
        serial="METER-SECRET-SERIAL",
        tariff_code="E-1R-GO-VAR-22-10-14-A",
        valid_from=None,
        valid_to=None,
        synced_at="seed",
    )
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    readings = []
    for day in range(days, 0, -1):
        start = today - timedelta(days=day)
        for slot in range(48):
            begin = start + timedelta(minutes=30 * slot)
            kwh = 5.0 if day == 2 and slot == 20 else 0.2
            readings.append(
                {"interval_start": iso(begin), "interval_end": iso(begin + timedelta(minutes=30)), "consumption": kwh}
            )
    upsert_consumption(conn, meter, readings)
    upsert_rate(
        conn,
        meter,
        "standard-unit-rates",
        {"valid_from": "2020-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 25.0},
    )
    upsert_rate(
        conn, meter, "standing-charges", {"valid_from": "2020-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 50.0}
    )
    with conn:
        recompute_daily_costs(conn, gas_units="unknown")
    ended = datetime.now(timezone.utc) - sync_age
    conn.execute(
        "INSERT INTO sync_history(started_at,ended_at,status,electricity_records,gas_records,error_category) "
        "VALUES(?,?,?,?,?,?)",
        (
            iso(ended - timedelta(seconds=5)),
            iso(ended),
            sync_status,
            len(readings),
            0,
            "OctopusAPIError" if sync_status == "failed" else None,
        ),
    )
    conn.commit()
    conn.close()


def seed_legacy_energy(path: Path) -> None:
    """Old schema where derived daily_costs columns are NOT NULL; the read-only path must refuse it."""
    schema = (
        (ROOT / "octopus_energy/schema.sql")
        .read_text(encoding="utf-8")
        .replace(
            "consumption_kwh REAL, unit_cost_gbp REAL, standing_charge_gbp REAL, total_cost_gbp REAL",
            "consumption_kwh REAL NOT NULL, unit_cost_gbp REAL NOT NULL, standing_charge_gbp REAL NOT NULL, "
            "total_cost_gbp REAL NOT NULL",
        )
    )
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO daily_costs VALUES('2026-01-01','gas',0,0,0,0)")
        conn.execute(
            "INSERT INTO sync_history(started_at,ended_at,status) VALUES('2026-01-01T00:00:00Z','2026-01-01T00:01:00Z','success')"
        )


def seed_briefing(path: Path, rows: list[dict]) -> None:
    from daily_briefing.storage import initialize, record_run

    initialize(path)
    for row in rows:
        record_run(
            path,
            report_date=row["report_date"],
            started_at=row.get("started_at", row["report_date"] + "T07:00:00+00:00"),
            ended_at=row.get("ended_at", row["report_date"] + "T07:00:01+00:00"),
            duration_ms=10,
            calendar_events=row.get("events", 2),
            tasks=row.get("tasks", 3),
            errors=row.get("errors", []),
        )
