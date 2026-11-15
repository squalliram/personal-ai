"""Energy view: the published /energy-usage snapshot and the Fetch latest data request flag."""

import json
import os
import re
import stat
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from command_centre_fixtures import ROOT, Client, Clock, make_app, make_settings

from command_centre import energy_refresh
from command_centre.security import Permission


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "last_attempt_at": None,
        "last_success_at": None,
        "result": None,
        "latest_reading_at": {"electricity": None, "gas": None},
        "request_state": None,
        "request_id": None,
        "error_category": None,
    }
    payload.update(overrides)
    return payload


class EnergyTestCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.refresh_dir = self.root / "energy-refresh"
        self.refresh_dir.mkdir(mode=0o700)
        self.refresh_dir.chmod(0o700)
        self.clock = Clock()
        self.settings = make_settings(self.root, energy_refresh_dir=self.refresh_dir)
        self.app = make_app(self.settings, clock=self.clock)
        self.client = Client(self.app)
        self.client.login()

    @property
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.now, timezone.utc)

    def write_status(self, **overrides):
        self.settings.energy_status.write_text(json.dumps(status_payload(**overrides)), encoding="utf-8")

    def post(self, **kwargs):
        return self.client.request(
            "POST", "/api/v1/energy/refresh", headers={"X-CSRF-Token": self.client.csrf}, **kwargs
        )

    def outcomes(self):
        return [e["outcome"] for e in self.app.audit.recent(100) if e["action"] == "energy_refresh"]


class DashboardSnapshotTests(EnergyTestCase):
    def test_returns_the_published_snapshot_with_only_dashboard_keys(self):
        data = {
            "generated_at": iso(self.now),
            "latest_reading_at": {"electricity": iso(self.now), "gas": None},
            "last_successful_sync_at": iso(self.now),
            "daily": [{"date": "2026-11-01", "fuel": "electricity", "kwh": 4.2, "cost": 1.1}],
            "hourly": [{"fuel": "electricity", "hour": 18, "kwh": 0.6}],
            "latest_day": {"electricity": None, "gas": None},
            "notice": "Whole-home meter data cannot identify individual appliances.",
            "unexpected": "dropped",
        }
        self.settings.energy_status.with_name("data.json").write_text(json.dumps(data), encoding="utf-8")
        body = self.client.request("GET", "/api/v1/energy/dashboard").json()
        self.assertEqual(body["state"], "available")
        self.assertNotIn("unexpected", body["dashboard"])
        self.assertEqual(body["dashboard"]["daily"], data["daily"])

    def test_missing_invalid_and_symlinked_snapshots_are_explicit(self):
        target = self.settings.energy_status.with_name("data.json")
        self.assertEqual(self.client.request("GET", "/api/v1/energy/dashboard").json()["state"], "not_published")
        for raw in ("{", "[]", json.dumps({"generated_at": "yesterday", "daily": [], "hourly": []})):
            target.write_text(raw, encoding="utf-8")
            body = self.client.request("GET", "/api/v1/energy/dashboard").json()
            self.assertEqual((body["state"], body["dashboard"]), ("invalid", None))
        target.unlink()
        real = self.root / "elsewhere.json"
        real.write_text(json.dumps({"generated_at": iso(self.now), "daily": [], "hourly": []}), encoding="utf-8")
        target.symlink_to(real)
        self.assertEqual(self.client.request("GET", "/api/v1/energy/dashboard").json()["state"], "invalid")

    def test_status_route_reports_missing_and_valid_status(self):
        self.assertEqual(
            self.client.request("GET", "/api/v1/energy/refresh").json(), {"state": "missing", "status": None}
        )
        self.write_status(last_success_at=iso(self.now), result="success")
        body = self.client.request("GET", "/api/v1/energy/refresh").json()
        self.assertEqual((body["state"], body["status"]["result"]), ("valid", "success"))

    def test_energy_routes_require_a_session(self):
        anonymous = Client(self.app)
        for method, path in (
            ("GET", "/api/v1/energy/dashboard"),
            ("GET", "/api/v1/energy/refresh"),
            ("POST", "/api/v1/energy/refresh"),
        ):
            self.assertEqual(anonymous.request(method, path).status, 401, path)


class RefreshRequestTests(EnergyTestCase):
    def flag(self) -> Path:
        return self.refresh_dir / "request.flag"

    def test_publishes_a_private_flag_with_a_random_request_id(self):
        response = self.post()
        self.assertEqual(response.status, 202)
        body = response.json()
        self.assertEqual(body["state"], "pending")
        self.assertRegex(body["request_id"], r"\A[0-9a-f]{32}\Z")
        self.assertEqual(self.flag().read_text(), body["request_id"])
        self.assertEqual(stat.S_IMODE(self.flag().stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.refresh_dir.iterdir()], ["request.flag"])
        self.assertEqual(self.outcomes(), ["pending"])

    def test_a_pending_flag_is_joined_not_replaced(self):
        first = self.post().json()
        second = self.post()
        self.assertEqual((second.status, second.json()["request_id"]), (202, first["request_id"]))
        self.assertEqual(self.flag().read_text(), first["request_id"])

    def test_running_claim_is_reported_as_already_running(self):
        request_id = "a" * 32
        (self.refresh_dir / "request.running").write_text(request_id)
        (self.refresh_dir / "request.running").chmod(0o600)
        self.write_status(
            last_attempt_at=iso(self.now), result="running", request_state="running", request_id=request_id
        )
        body = self.post().json()
        self.assertEqual((body["state"], body["request_id"]), ("already_running", request_id))

    def test_retry_queued_for_a_matching_failed_attempt(self):
        request_id = "b" * 32
        self.flag().write_text(request_id)
        self.flag().chmod(0o600)
        self.write_status(
            last_attempt_at=iso(self.now - timedelta(minutes=1)),
            result="failed",
            error_category="sync_failed",
            request_state="pending",
            request_id=request_id,
        )
        self.assertEqual(self.post().json()["state"], "retry_queued")

    def test_cooldown_after_a_recent_attempt(self):
        self.write_status(last_attempt_at=iso(self.now - timedelta(seconds=100)), result="no_data")
        response = self.post()
        self.assertEqual(response.status, 429)
        self.assertEqual(response.json(), {"state": "rate_limited", "retry_after": 200})
        self.assertEqual(response.header("Retry-After"), "200")
        self.assertFalse(self.flag().exists())
        self.write_status(last_attempt_at=iso(self.now - timedelta(seconds=301)), result="no_data")
        self.assertEqual(self.post().status, 202)

    def test_unsafe_directory_or_flags_are_unavailable(self):
        self.refresh_dir.chmod(0o750)
        self.assertEqual(self.post().json()["state"], "unavailable")
        self.refresh_dir.chmod(0o700)
        self.flag().write_text("c" * 32)
        self.flag().chmod(0o644)
        self.assertEqual(self.post().status, 503)
        self.flag().unlink()
        self.flag().symlink_to(self.root / "elsewhere")
        self.assertEqual(self.post().status, 503)
        self.flag().unlink()
        self.settings.energy_status.write_text("{}", encoding="utf-8")
        self.assertEqual(self.post().status, 503)
        self.assertFalse(self.flag().exists())

    def test_symlinked_or_unconfigured_directory_is_unavailable(self):
        link = self.root / "link"
        link.symlink_to(self.refresh_dir)
        for directory in (link, None, self.root / "absent"):
            code, body, _ = energy_refresh.request_refresh(
                replace(self.settings, energy_refresh_dir=directory), self.now
            )
            self.assertEqual((code, body["state"]), (503, "unavailable"))

    def test_requires_recommend_permission_csrf_and_same_origin(self):
        read_only = Client(make_app(replace(self.settings, permission_ceiling=Permission.READ), clock=self.clock))
        read_only.login()
        response = read_only.request("POST", "/api/v1/energy/refresh", headers={"X-CSRF-Token": read_only.csrf})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.client.request("POST", "/api/v1/energy/refresh").status, 403)
        self.assertEqual(self.post(origin="http://evil.example").status, 403)
        self.assertFalse(self.flag().exists())


class EnergyViewWiringTests(unittest.TestCase):
    def test_view_reuses_the_standalone_dashboard_helpers(self):
        app = make_app(make_settings(Path(TemporaryDirectory().name)))
        core = app.static["/static/energy-core.js"][0]
        self.assertEqual(core, (ROOT / "octopus_energy/dashboard.js").read_bytes())
        index = (ROOT / "command_centre/static/index.html").read_text(encoding="utf-8")
        self.assertLess(index.index('src="static/energy-core.js"'), index.index('src="static/app.js"'))
        script = (ROOT / "command_centre/static/app.js").read_text(encoding="utf-8")
        for helper in (
            "validateData",
            "viewGroups",
            "anomalyModel",
            "chartLayout",
            "validateRefreshStatus",
            "readingState",
        ):
            self.assertRegex(script, rf"\b{helper}\(", helper)
        # The standalone page starts itself only where its own markup exists.
        self.assertTrue(re.search(r"document\.querySelector\('#chart'\)\) initializeDashboard\(\)", core.decode()))

    def test_refresh_module_never_runs_the_sync(self):
        source = (ROOT / "command_centre/energy_refresh.py").read_text(encoding="utf-8")
        imports = re.findall(r"^(?:from|import) (\S+)", source, re.M)
        self.assertEqual(
            sorted(set(imports)),
            [".config", ".sources", "__future__", "datetime", "math", "os", "pathlib", "re", "secrets", "stat"],
        )
        for forbidden in ("subprocess", "dashboard_data", "write_reports", "connect(", "urlopen"):
            self.assertNotIn(forbidden, source)

    def test_hosted_settings_point_at_the_runner_refresh_directory(self):
        host = (ROOT / "deploy/command-centre/deploy_host.py").read_text(encoding="utf-8")
        self.assertIn('"ENERGY_REFRESH_DIR": str(checkout / "data/energy-refresh")', host)
        self.assertEqual(os.fspath(make_settings(Path("/x")).energy_refresh_dir or ""), "")


if __name__ == "__main__":
    unittest.main()
