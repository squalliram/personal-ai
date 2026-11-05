import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from octopus_energy.analytics import recompute_daily_costs
from octopus_energy.dashboard import dashboard_data, write_dashboard
from octopus_energy.storage import connect, upsert_consumption, upsert_meter

ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "energy.sqlite3"
        self.conn = connect(self.database)
        self.addCleanup(self.conn.close)
        self.meter = upsert_meter(
            self.conn,
            fuel="electricity",
            point_id="private-point",
            serial="private-serial",
            tariff_code="private-tariff",
            valid_from=None,
            valid_to=None,
            synced_at="2026-11-01T00:00:00Z",
        )
        self.conn.commit()

    def reading(self, start, end, kwh):
        upsert_consumption(
            self.conn,
            self.meter,
            {
                "interval_start": start,
                "interval_end": end,
                "consumption": kwh,
            },
        )
        self.conn.commit()

    def test_empty_export_does_not_claim_recent_readings_or_sync(self):
        data = dashboard_data(self.database)
        self.assertEqual(data["latest_reading_at"], {})
        self.assertIsNone(data["last_successful_sync_at"])
        self.assertEqual(data["daily"], [])
        self.assertEqual(data["hourly"], [])

    def test_hourly_profile_uses_actual_interval_duration(self):
        self.reading("2026-11-01T12:00:00Z", "2026-11-01T12:30:00Z", 1)
        self.reading("2026-11-02T12:00:00Z", "2026-11-02T13:00:00Z", 3)
        data = dashboard_data(self.database)
        self.assertEqual(data["hourly"], [{"fuel": "electricity", "hour": 12, "kwh": 2.667}])
        self.assertEqual(data["latest_reading_at"]["electricity"], "2026-11-02T13:00:00+00:00")
        self.assertNotIn("private-", json.dumps(data))

    def test_intervals_are_split_at_clock_hour_boundaries(self):
        self.reading("2026-11-01T11:45:00Z", "2026-11-01T12:15:00Z", 2)
        self.assertEqual(
            dashboard_data(self.database)["hourly"],
            [
                {"fuel": "electricity", "hour": 11, "kwh": 4},
                {"fuel": "electricity", "hour": 12, "kwh": 4},
            ],
        )

    def test_london_dst_repeated_hour_is_duration_weighted(self):
        self.reading("2026-10-25T00:00:00Z", "2026-10-25T01:00:00Z", 1)
        self.reading("2026-10-25T01:00:00Z", "2026-10-25T02:00:00Z", 3)
        data = dashboard_data(self.database)
        self.assertEqual(data["hourly"], [{"fuel": "electricity", "hour": 1, "kwh": 2}])

    def test_concurrent_electricity_meters_add_energy_not_duplicate_time(self):
        self.reading("2026-11-01T12:00:00Z", "2026-11-01T12:30:00Z", 1)
        other = upsert_meter(
            self.conn,
            fuel="electricity",
            point_id="second-synthetic-point",
            serial="second-synthetic",
            tariff_code="synthetic",
            valid_from=None,
            valid_to=None,
            synced_at="2026-11-01T00:00:00Z",
        )
        upsert_consumption(
            self.conn,
            other,
            {
                "interval_start": "2026-11-01T12:00:00Z",
                "interval_end": "2026-11-01T12:30:00Z",
                "consumption": 2,
            },
        )
        self.conn.commit()
        self.assertEqual(
            dashboard_data(self.database)["hourly"],
            [
                {"fuel": "electricity", "hour": 12, "kwh": 6},
            ],
        )

    def test_raw_gas_volume_is_not_exported_as_an_hourly_kwh_profile(self):
        gas = upsert_meter(
            self.conn,
            fuel="gas",
            point_id="synthetic-gas",
            serial="synthetic-gas",
            tariff_code="synthetic",
            valid_from=None,
            valid_to=None,
            synced_at="2026-11-01T00:00:00Z",
        )
        upsert_consumption(
            self.conn,
            gas,
            {
                "interval_start": "2026-11-01T12:00:00Z",
                "interval_end": "2026-11-01T12:30:00Z",
                "consumption": 2,
            },
        )
        recompute_daily_costs(self.conn)
        self.conn.commit()
        data = dashboard_data(self.database)
        self.assertEqual(data["hourly"], [])
        self.assertIn("gas", data["latest_reading_at"])
        self.assertEqual(len(data["daily"]), 1)
        self.assertIsNone(data["daily"][0]["kwh"])
        self.assertIsNone(data["daily"][0]["cost"])
        self.assertEqual(data["daily"][0]["cost_status"], "incomplete")
        self.assertIn("unknown_gas_units", data["daily"][0]["cost_reason"])

    def test_missing_tariffs_preserve_usage_but_never_export_free_energy(self):
        self.reading("2026-11-01T12:00:00Z", "2026-11-01T12:30:00Z", 1)
        recompute_daily_costs(self.conn)
        self.conn.commit()
        (row,) = dashboard_data(self.database)["daily"]
        self.assertEqual(row["kwh"], 1)
        self.assertIsNone(row["cost"])
        self.assertEqual(row["cost_status"], "incomplete")
        self.assertIn("missing_unit_rate", row["cost_reason"])

    def test_latest_successful_sync_excludes_later_failure_and_no_data(self):
        for ended, status in [
            ("2026-11-01T10:00:00Z", "success"),
            ("2026-11-02T10:00:00Z", "success"),
            ("2026-11-03T10:00:00Z", "failed"),
            ("2026-11-04T10:00:00Z", "no_data"),
        ]:
            self.conn.execute(
                "INSERT INTO sync_history(started_at,ended_at,status) VALUES(?,?,?)",
                (ended, ended, status),
            )
        self.conn.commit()
        self.assertEqual(dashboard_data(self.database)["last_successful_sync_at"], "2026-11-02T10:00:00Z")

    def test_export_embeds_script_and_only_identifier_free_metadata(self):
        self.reading("2026-11-01T12:00:00Z", "2026-11-01T12:30:00Z", 1)
        output = self.root / "dashboard"
        index = write_dashboard(self.database, output)
        self.assertIn("initializeDashboard()", index.read_text())
        data = json.loads((output / "data.json").read_text())
        self.assertNotIn("private-", json.dumps(data))
        self.assertEqual({path.name for path in output.iterdir()}, {"index.html", "data.json"})

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard JavaScript checks")
    def test_dashboard_javascript(self):
        result = subprocess.run(
            ["node", str(ROOT / "tests/dashboard.test.js")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PublishDashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "index.html").write_text("<html>synthetic dashboard</html>")
        (self.source / "data.json").write_text('{"daily":[],"hourly":[]}')
        (self.source / ".env").write_text("synthetic excluded file")
        self.target = self.root / "protected"
        self.password_file = self.root / "passwd"
        self.password_file.write_text("synthetic test fixture")

    def publish(self):
        env = dict(os.environ, DASHBOARD_DIR=str(self.source), DASHBOARD_TARGET=str(self.target))
        return subprocess.run(
            [str(ROOT / "scripts/publish-energy-dashboard.sh")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def protection(self, extra=""):
        self.target.mkdir(exist_ok=True)
        (self.target / ".htaccess").write_text(
            f'AuthType Basic\nAuthName "Private energy"\nAuthUserFile "{self.password_file}"\nRequire valid-user\n{extra}'
        )

    def test_refuses_to_create_unprotected_target(self):
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())

    def test_refuses_existing_target_without_protection_and_preserves_files(self):
        self.target.mkdir()
        (self.target / "index.html").write_text("existing dashboard")
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.target / "index.html").read_text(), "existing dashboard")
        self.assertFalse((self.target / "data.json").exists())

    def test_refuses_authorization_bypass_or_conditional_configuration(self):
        for extra in ("Require all granted\n", "Satisfy any\n", "Allow from all\n"):
            with self.subTest(extra=extra):
                self.protection(extra)
                self.assertNotEqual(self.publish().returncode, 0)
                self.assertFalse((self.target / "data.json").exists())
        self.protection()
        path = self.target / ".htaccess"
        path.write_text("<IfModule mod_auth_basic.c>\n" + path.read_text() + "</IfModule>\n")
        self.assertNotEqual(self.publish().returncode, 0)

    def test_requires_existing_password_file(self):
        self.protection()
        self.password_file.unlink()
        self.assertNotEqual(self.publish().returncode, 0)

    def test_publishes_only_two_files_and_preserves_protection(self):
        self.protection()
        protection = (self.target / ".htaccess").read_text()
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / ".htaccess").read_text(), protection)
        self.assertEqual((self.target / "data.json").read_text(), (self.source / "data.json").read_text())
        self.assertEqual({p.name for p in self.target.iterdir()}, {".htaccess", "index.html", "data.json"})

    def test_missing_source_file_does_not_replace_existing_data(self):
        self.protection()
        (self.target / "data.json").write_text("previous")
        (self.source / "index.html").unlink()
        self.assertNotEqual(self.publish().returncode, 0)
        self.assertEqual((self.target / "data.json").read_text(), "previous")


class DeploymentWorkflowTests(unittest.TestCase):
    def test_secret_configuration_gate_without_exposing_values(self):
        from test_energy_release import workflow_run_block

        workflow = (ROOT / ".github/workflows/deploy-dashboard.yml").read_text()
        command = textwrap.dedent(workflow_run_block(workflow, "Check deployment configuration"))
        names = (
            "CPANEL_SSH_HOST",
            "CPANEL_SSH_USERNAME",
            "CPANEL_SSH_PRIVATE_KEY",
            "CPANEL_SSH_KNOWN_HOSTS",
            "CPANEL_APP_PATH",
            "CPANEL_DASHBOARD_PATH",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            for missing in (*names, None):
                with self.subTest(missing=missing):
                    output.write_text("")
                    env = dict(os.environ, GITHUB_OUTPUT=str(output))
                    env.update({name: "" if name == missing else "synthetic-config-value" for name in names})
                    result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text().strip(), "ready=true" if missing is None else "ready=false")
                    self.assertNotIn("synthetic-config-value", result.stdout + result.stderr)
        job_condition = workflow.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0]
        self.assertNotIn("secrets.", job_condition)
        self.assertIn("github.event.workflow_run.event == 'push'", job_condition)
        self.assertIn("-o StrictHostKeyChecking=yes", workflow)
        self.assertIn('test \\"\\$(git rev-parse HEAD)\\" =', workflow)
