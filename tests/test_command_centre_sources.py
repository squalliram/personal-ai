import json
import os
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from command_centre_fixtures import (
    ROOT,
    Client,
    Clock,
    checksum,
    iso,
    make_app,
    make_settings,
    seed_briefing,
    seed_energy,
    seed_legacy_energy,
)

from command_centre import sources
from command_centre.automations import marked_entries

ALL_GETS = (
    "/api/v1/overview",
    "/api/v1/integrations",
    "/api/v1/automations",
    "/api/v1/energy",
    "/api/v1/modules",
    "/api/v1/agents",
)
FORBIDDEN = (
    "storage import connect",
    "storage.connect",
    "write_reports",
    "write_dashboard",
    "dashboard_data",
    "recompute_daily_costs",
    "set_gas_units",
    "upsert_",
    "octopus_energy.cli",
    "install_refresh",
    "refresh import refresh",
    "refresh.refresh(",
    "_refresh_locked",
    'crontab", "-"',
    "executescript",
)


def today():
    return datetime.now(timezone.utc).astimezone(sources.LONDON).date().isoformat()


class SourceTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = make_settings(self.root)

    def client(self, cron=("absent", ""), settings=None):
        client = Client(make_app(settings or self.settings, clock=Clock(), cron=cron))
        self.assertEqual(client.login().status, 200)
        return client

    def get(self, client, path):
        response = client.request("GET", path)
        self.assertEqual(response.status, 200, response.body)
        return response.json()

    def by_id(self, items):
        return {item["id"]: item for item in items}


class ReadOnlyGuaranteeTests(SourceTestCase):
    def test_adapters_never_reference_writing_entry_points(self):
        for name in ("sources.py", "automations.py", "app.py"):
            text = (ROOT / "command_centre" / name).read_text(encoding="utf-8")
            for forbidden in FORBIDDEN:
                with self.subTest(module=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_legacy_database_is_reported_and_left_byte_identical(self):
        seed_legacy_energy(self.settings.energy_db)
        before = checksum(self.settings.energy_db)
        with closing(sqlite3.connect(self.settings.energy_db)) as conn:
            schema_before = conn.execute("SELECT group_concat(sql) FROM sqlite_master").fetchone()[0]
        client = self.client()
        for path in ALL_GETS:
            self.get(client, path)
        energy = self.get(client, "/api/v1/energy")
        self.assertEqual(energy["state"], "refresh_required")
        self.assertEqual(energy["health"]["status"], "refresh_required")
        self.assertEqual(energy["totals"], [])
        self.assertEqual(energy["sync_history"][0]["status"], "success")
        self.assertEqual(checksum(self.settings.energy_db), before)
        self.assertFalse(Path(str(self.settings.energy_db) + "-wal").exists())
        with closing(sqlite3.connect(self.settings.energy_db)) as conn:
            self.assertEqual(conn.execute("SELECT group_concat(sql) FROM sqlite_master").fetchone()[0], schema_before)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        overview = self.get(client, "/api/v1/overview")
        self.assertIn("octopus_energy", [a["source"] for a in overview["attention"]])

    def test_current_database_is_unchanged_by_reads(self):
        seed_energy(self.settings.energy_db)
        before = checksum(self.settings.energy_db)
        client = self.client()
        for path in ALL_GETS * 2:
            self.get(client, path)
        self.assertEqual(checksum(self.settings.energy_db), before)

    def test_briefing_database_is_unchanged_by_reads(self):
        seed_briefing(self.settings.briefing_db, [{"report_date": today()}])
        before = checksum(self.settings.briefing_db)
        client = self.client()
        for path in (*ALL_GETS, "/api/v1/briefing/runs"):
            self.get(client, path)
        self.assertEqual(checksum(self.settings.briefing_db), before)

    def test_symlinked_database_is_not_followed(self):
        real = self.root / "real.sqlite3"
        seed_energy(real)
        os.symlink(real, self.settings.energy_db)
        energy = self.get(self.client(), "/api/v1/energy")
        self.assertEqual(energy["state"], "not_configured")


class EnergyHealthTests(SourceTestCase):
    def test_nothing_configured_is_never_operational(self):
        client = self.client()
        found = self.get(client, "/api/v1/integrations")["integrations"]
        statuses = {item["id"]: item["status"] for item in found}
        self.assertEqual(statuses["octopus_energy"], "not_configured")
        self.assertEqual(statuses["daily_briefing"], "not_configured")
        self.assertEqual(statuses["calendar_snapshot"], "unverified")
        self.assertEqual(statuses["notion_snapshot"], "unverified")
        for missing in ("career", "finance", "family", "health_fitness", "caldav"):
            self.assertEqual(statuses[missing], "not_implemented")
        self.assertNotIn("operational", statuses.values())
        jobs = self.by_id(self.get(client, "/api/v1/automations")["automations"])
        self.assertNotIn("operational", {job["status"] for job in jobs.values()})
        overview = self.get(client, "/api/v1/overview")
        self.assertEqual(len(overview["missing_integrations"]), 5)
        self.assertEqual(overview["energy"]["state"], "not_configured")

    def test_recent_success_is_operational_with_labelled_measured_and_estimated_values(self):
        seed_energy(self.settings.energy_db)
        energy = self.get(self.client(), "/api/v1/energy")
        self.assertEqual(energy["state"], "available")
        self.assertEqual(energy["health"]["status"], "operational")
        self.assertEqual(energy["basis"]["consumption_kwh"], "measured")
        self.assertEqual(energy["basis"]["total_cost_gbp"], "estimated")
        self.assertEqual(energy["basis"]["anomalies"], "inferred")
        self.assertFalse(energy["actual_bills"]["available"])
        electricity = next(row for row in energy["totals"] if row["fuel"] == "electricity")
        # 10 days x 48 x 0.2 kWh, plus one 5 kWh spike replacing a 0.2 kWh slot.
        self.assertAlmostEqual(electricity["consumption_kwh"], 10 * 48 * 0.2 + 4.8, places=6)
        # 25 p/kWh unit rate plus 50 p/day standing charge.
        self.assertAlmostEqual(
            electricity["total_cost_gbp"], round(electricity["consumption_kwh"] * 0.25 + 10 * 0.5, 2)
        )
        self.assertEqual(len(energy["daily"]), 10)
        self.assertEqual(energy["tariffs"][0]["tariff_code"], "E-1R-GO-VAR-22-10-14-A")
        self.assertEqual([row["consumption_kwh"] > row["threshold_kwh"] for row in energy["anomalies"]], [True])

    def test_sync_outcomes_map_to_evidence_based_states(self):
        cases = [
            ({"sync_status": "failed"}, "failed"),
            ({"sync_status": "partial"}, "degraded"),
            ({"sync_status": "no_data"}, "degraded"),
            ({"sync_status": "running"}, "running"),
            ({"sync_age": timedelta(hours=49)}, "stale"),
        ]
        for kwargs, expected in cases:
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                self.settings.energy_db.unlink(missing_ok=True)
                seed_energy(self.settings.energy_db, **kwargs)
                self.assertEqual(self.get(self.client(), "/api/v1/energy")["health"]["status"], expected)

    def test_unreadable_database_is_failed_not_empty(self):
        self.settings.energy_db.write_bytes(b"not a database" * 100)
        energy = self.get(self.client(), "/api/v1/energy")
        self.assertEqual(energy["state"], "unavailable")
        self.assertEqual(energy["health"]["status"], "failed")


class BriefingTests(SourceTestCase):
    def test_connector_error_text_is_never_exposed(self):
        hostile = "<script>alert(1)</script> javascript:alert(1) private task title"
        seed_briefing(self.settings.briefing_db, [{"report_date": today(), "errors": [hostile]}])
        client = self.client()
        runs = self.get(client, "/api/v1/briefing/runs")["runs"]
        self.assertEqual(runs[0]["error_count"], 1)
        self.assertNotIn("errors", runs[0])
        for path in (*ALL_GETS, "/api/v1/briefing/runs"):
            body = client.request("GET", path).body
            self.assertNotIn(b"<script>", body)
            self.assertNotIn(b"private task title", body)
        health = self.by_id(self.get(client, "/api/v1/integrations")["integrations"])["daily_briefing"]
        self.assertEqual(health["status"], "degraded")

    def test_today_clean_run_is_operational_and_older_run_is_stale(self):
        seed_briefing(self.settings.briefing_db, [{"report_date": "2020-01-01"}])
        found = self.by_id(self.get(self.client(), "/api/v1/integrations")["integrations"])
        self.assertEqual(found["daily_briefing"]["status"], "stale")
        seed_briefing(self.settings.briefing_db, [{"report_date": today(), "events": 4, "tasks": 7}])
        found = self.by_id(self.get(self.client(), "/api/v1/integrations")["integrations"])
        self.assertEqual(found["daily_briefing"]["status"], "operational")
        self.assertEqual(found["calendar_snapshot"]["evidence"]["latest_events_in_briefing"], 4)
        self.assertEqual(found["calendar_snapshot"]["status"], "unverified")

    def test_latest_report_file_is_detected_without_reading_content(self):
        self.settings.briefing_reports.mkdir()
        (self.settings.briefing_reports / "2026-01-02.md").write_text("secret agenda", encoding="utf-8")
        (self.settings.briefing_reports / "notes.md").write_text("x", encoding="utf-8")
        overview = self.client().request("GET", "/api/v1/overview")
        self.assertEqual(overview.json()["briefing"]["latest_report_date"], "2026-01-02")
        self.assertNotIn(b"secret agenda", overview.body)


class AutomationTests(SourceTestCase):
    def write_status(self, **overrides):
        status = {
            "schema_version": 1,
            "last_attempt_at": iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
            "last_success_at": iso(datetime.now(timezone.utc) - timedelta(minutes=4)),
            "result": "success",
            "latest_reading_at": {"electricity": None, "gas": None},
            "request_state": None,
            "request_id": None,
            "error_category": None,
        }
        status.update(overrides)
        self.settings.energy_status.write_text(json.dumps(status), encoding="utf-8")

    def energy_job(self, cron=("absent", "")):
        return self.by_id(self.get(self.client(cron), "/api/v1/automations")["automations"])["energy-refresh"]

    def test_status_file_drives_execution_health(self):
        self.assertEqual(self.energy_job()["status"], "unverified")
        self.write_status()
        job = self.energy_job()
        self.assertEqual(job["status"], "operational")
        self.assertEqual(job["last_execution"]["status_file"], "valid")
        self.write_status(result="failed", error_category="provider_error")
        self.assertEqual(self.energy_job()["status"], "failed")
        self.write_status(last_success_at=iso(datetime.now(timezone.utc) - timedelta(days=3)))
        self.assertEqual(self.energy_job()["status"], "stale")
        self.settings.energy_status.write_text('{"result": "success"}', encoding="utf-8")
        job = self.energy_job()
        self.assertEqual(job["status"], "unverified")
        self.assertEqual(job["last_execution"]["status_file"], "invalid")

    def test_symlinked_status_file_is_refused(self):
        real = self.root / "elsewhere.json"
        self.settings.energy_status.write_text("{}", encoding="utf-8")
        self.settings.energy_status.rename(real)
        os.symlink(real, self.settings.energy_status)
        self.assertEqual(self.energy_job()["last_execution"]["status_file"], "invalid")

    def test_duplicate_cron_entries_are_flagged_and_cron_text_is_never_returned(self):
        line = "* * * * * cd /app && ./scripts/refresh-energy.sh >/dev/null 2>&1 # personal-ai-energy-refresh\n"
        other = "0 3 * * * /usr/bin/backup --token=CRON_SECRET\n"
        self.write_status()
        client = self.client(("available", other + line + line))
        response = client.request("GET", "/api/v1/automations")
        job = self.by_id(response.json()["automations"])["energy-refresh"]
        self.assertEqual(job["status"], "degraded")
        self.assertEqual(job["alerts"], ["duplicate_cron_entries"])
        self.assertEqual(job["local_cron"]["marked_entries"], 2)
        self.assertNotIn(b"CRON_SECRET", response.body)
        self.assertNotIn(b"refresh-energy.sh >/dev/null", response.body)
        self.assertEqual(self.energy_job(("available", line))["local_cron"]["marked_entries"], 1)
        self.assertEqual(self.energy_job(("unavailable", ""))["local_cron"]["marked_entries"], None)
        self.assertEqual(marked_entries("# " + line), 0)

    def test_stop_scripts_are_reported_truthfully(self):
        jobs = self.by_id(self.get(self.client(), "/api/v1/automations")["automations"])
        self.assertIs(jobs["energy-refresh"]["stop_control"]["effective"], False)
        self.assertEqual(jobs["daily-briefing"]["stop_control"]["effective"], "not_applicable")
        for job in jobs.values():
            self.assertFalse(any(control["available"] for control in job["controls"].values()))
        self.assertEqual(jobs["energy-mcp"]["status"], "unverified")
        for script in ("stop.sh", "stop-energy.sh"):
            text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
            self.assertIn("exit 0", text)
            self.assertNotIn("crontab", text)


class ModuleTests(SourceTestCase):
    def test_navigation_covers_all_sections_with_honest_states(self):
        modules = self.get(self.client(), "/api/v1/modules")["modules"]
        self.assertEqual(
            [m["id"] for m in modules],
            [
                "overview",
                "my-day",
                "tasks",
                "calendar",
                "energy",
                "career",
                "finance",
                "family",
                "health",
                "agents",
                "automations",
                "integrations",
                "settings",
            ],
        )
        states = {m["id"]: m["state"] for m in modules}
        for missing in ("career", "finance", "family", "health"):
            self.assertEqual(states[missing], "not_implemented")


class AgentTests(SourceTestCase):
    def test_registry_lists_every_agent_definition(self):
        agents = self.get(self.client(), "/api/v1/agents")["agents"]
        self.assertEqual([a["id"] for a in agents], ["daily-briefing", "energy-analyst", "instagram"])
        for agent in agents:
            self.assertEqual(
                set(agent), {"id", "name", "status", "summary", "definition", "runs_in", "guardrail", "commands"}
            )
            self.assertTrue((ROOT / agent["definition"]).is_file(), agent["definition"])
        self.assertEqual(
            sorted(path.name for path in (ROOT / "agents").glob("*.md")),
            sorted(Path(a["definition"]).name for a in agents),
        )

    def test_instagram_agent_offers_the_ig_skill_commands_and_drafts_only(self):
        instagram = self.by_id(self.get(self.client(), "/api/v1/agents")["agents"])["instagram"]
        self.assertEqual(instagram["name"], "Instagram")
        self.assertEqual(instagram["status"], "available")
        self.assertIn("nothing is posted automatically", instagram["summary"])
        commands = [c["command"] for c in instagram["commands"]]
        self.assertEqual(len(commands), 13)
        self.assertEqual(len(set(commands)), 13)
        self.assertTrue(all(c.startswith("/ig-") for c in commands))
        definition = (ROOT / instagram["definition"]).read_text(encoding="utf-8")
        for command in commands:
            self.assertIn(f"`{command}`", definition)


if __name__ == "__main__":
    unittest.main()
