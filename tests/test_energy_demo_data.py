import contextlib
import importlib.util
import io
import json
import runpy
import sqlite3
import statistics
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from octopus_energy.cli import sync
from octopus_energy.client import Meter

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "energy-demo-data.py"
SPEC = importlib.util.spec_from_file_location("energy_demo_data", SCRIPT)
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)


class EnergyDemoDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def snapshot(self, database):
        with contextlib.closing(sqlite3.connect(database)) as conn:
            return tuple(conn.iterdump())

    def assert_force_refuses_without_changes(self, database, output, *, cli=False):
        before = self.snapshot(database)
        database_bytes = database.read_bytes()
        wal = Path(str(database) + "-wal")
        wal_bytes = wal.read_bytes() if wal.exists() else None
        files = {path.name: path.read_bytes() for path in output.iterdir()}
        if cli:
            argv = [str(SCRIPT), "--database", str(database), "--output-dir", str(output), "--force"]
            with (
                patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit) as exited,
            ):
                runpy.run_path(str(SCRIPT), run_name="__main__")
            self.assertEqual(exited.exception.code, 2)
            self.assertIn("not exclusively synthetic demo data", stderr.getvalue())
        else:
            with self.assertRaisesRegex(FileExistsError, "not exclusively synthetic demo data"):
                DEMO.generate_demo(database, output, force=True)
        self.assertEqual(self.snapshot(database), before)
        self.assertEqual(database.read_bytes(), database_bytes)
        if wal_bytes:
            self.assertEqual(wal.read_bytes(), wal_bytes)
        self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, files)

    def test_cli_generates_both_fuels_anomalies_and_public_safe_exports_without_network(self):
        database, output = self.root / "demo.sqlite3", self.root / "site"
        argv = [str(SCRIPT), "--database", str(database), "--output-dir", str(output), "--seed", "37"]
        with (
            patch.object(sys, "argv", argv),
            patch("socket.socket.connect", side_effect=AssertionError("No network")) as connect,
            patch("socket.create_connection", side_effect=AssertionError("No network")) as create,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            self.assertRaises(SystemExit) as exited,
        ):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(exited.exception.code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["synthetic"])
        connect.assert_not_called()
        create.assert_not_called()
        with sqlite3.connect(database) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_costs").fetchone()[0], 800)
            self.assertEqual(conn.execute("SELECT COUNT(DISTINCT tariff_code) FROM meters").fetchone()[0], 2)
            self.assertEqual(
                conn.execute("SELECT value FROM energy_settings WHERE name='gas_units'").fetchone()[0], "kwh"
            )
            self.assertEqual(conn.execute("SELECT account_number FROM accounts").fetchone()[0], "DEMO-ACCOUNT-NOT-REAL")
            for row in conn.execute("SELECT point_id,serial_number,tariff_code FROM meters"):
                self.assertTrue(all(value.startswith("DEMO-") for value in row))
            for fuel in ("electricity", "gas"):
                rows = conn.execute(
                    "SELECT consumption_kwh,total_cost_gbp FROM daily_costs WHERE fuel=? ORDER BY date DESC LIMIT 90",
                    (fuel,),
                ).fetchall()
                values = [row[0] for row in rows]
                self.assertTrue(all(row[1] is not None and row[1] > 0 for row in rows))
                self.assertGreater(max(values), statistics.mean(values) + 2 * statistics.pstdev(values))
            dates = Counter(
                datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/London")).date()
                for (start,) in conn.execute(
                    "SELECT interval_start FROM consumption WHERE meter_id=(SELECT id FROM meters WHERE fuel='electricity')"
                )
            )
            self.assertEqual(len(dates), 400)
            self.assertEqual(dates[date(2026, 3, 29)], 46)
            self.assertEqual(dates[date(2025, 10, 26)], 50)
            self.assertEqual(dates[date(2026, 10, 25)], 50)
        data_text = (output / "data.json").read_text()
        self.assertEqual({row["fuel"] for row in json.loads(data_text)["daily"]}, {"electricity", "gas"})
        self.assertNotIn("DEMO-ACCOUNT-NOT-REAL", data_text)
        self.assertNotIn("DEMO-ELECTRICITY-METER", data_text)
        self.assertIn("synthetic", json.loads(data_text)["notice"])
        self.assertIn("SYNTHETIC DEMO · FAKE DATA", (output / "index.html").read_text())
        self.assertTrue((output / "monthly-cost-analysis.md").is_file())
        self.assertEqual(len(list(output.glob("????-??-??.md"))), 1)

    def test_seed_is_reproducible_and_force_regeneration_has_no_duplicates(self):
        database, other = self.root / "first.sqlite3", self.root / "second.sqlite3"
        with patch("socket.socket.connect", side_effect=AssertionError("No network")):
            DEMO.generate_demo(database, self.root / "site", seed=91)
            initial = self.snapshot(database)
            DEMO.generate_demo(other, self.root / "other-site", seed=91)
            self.assertEqual(self.snapshot(other), initial)
            DEMO.generate_demo(database, self.root / "site", seed=91, force=True)
            self.assertEqual(self.snapshot(database), initial)
            DEMO.generate_demo(database, self.root / "site", seed=92, force=True)
            self.assertNotEqual(self.snapshot(database), initial)
        with sqlite3.connect(database) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_costs").fetchone()[0], 800)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_history").fetchone()[0], 1)

    def test_existing_non_demo_database_is_never_overwritten_even_with_force(self):
        database = self.root / "existing.sqlite3"
        database.write_bytes(b"Not a demo database; preserve it.")
        for force in (False, True):
            with self.assertRaises(FileExistsError):
                DEMO.generate_demo(database, self.root / "site", force=force)
            self.assertEqual(database.read_bytes(), b"Not a demo database; preserve it.")
        self.assertFalse((self.root / "site").exists())

    def test_existing_demo_requires_explicit_force(self):
        database = self.root / "existing.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE energy_settings (name TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO energy_settings VALUES('synthetic_demo',?)", (DEMO.DEMO_MARKER,))
        before = database.read_bytes()
        with self.assertRaises(FileExistsError):
            DEMO.generate_demo(database, self.root / "site")
        self.assertEqual(database.read_bytes(), before)

    def test_force_preserves_non_demo_data_added_by_real_sync_even_in_wal(self):
        for hold_wal in (False, True):
            with self.subTest(hold_wal=hold_wal), contextlib.ExitStack() as stack:
                database = self.root / f"mixed-{hold_wal}.sqlite3"
                output = self.root / f"site-{hold_wal}"
                DEMO.generate_demo(database, output)
                if hold_wal:
                    keeper = stack.enter_context(contextlib.closing(sqlite3.connect(database)))
                    keeper.execute("PRAGMA wal_autocheckpoint=0")
                    keeper.execute("SELECT COUNT(*) FROM accounts").fetchone()
                with (
                    patch("octopus_energy.cli.OctopusClient") as client,
                    patch("octopus_energy.cli.utc_now", return_value="2026-11-05T12:00:00Z"),
                    patch("socket.socket.connect", side_effect=AssertionError("No network")),
                    patch("socket.create_connection", side_effect=AssertionError("No network")),
                ):
                    client.return_value.account.return_value = {}
                    client.return_value.meters.return_value = [
                        Meter("electricity", "non-demo-point", "non-demo-meter", "non-demo-tariff", None, None)
                    ]
                    client.return_value.consumption.return_value = [
                        {
                            "interval_start": "2026-11-04T12:00:00Z",
                            "interval_end": "2026-11-04T12:30:00Z",
                            "consumption": 1.25,
                        }
                    ]
                    client.return_value.tariff_rates.return_value = [
                        {
                            "valid_from": "2020-01-01T00:00:00Z",
                            "valid_to": None,
                            "value_inc_vat": 21,
                        }
                    ]
                    result = sync(database, "non-demo-account", "test-key", 2, gas_units="kwh")
                self.assertEqual(result["status"], "success")
                with contextlib.closing(sqlite3.connect(database)) as conn:
                    self.assertEqual(
                        conn.execute("SELECT value FROM energy_settings WHERE name='synthetic_demo'").fetchone(),
                        (DEMO.DEMO_MARKER,),
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT account_number FROM accounts WHERE account_number='non-demo-account'"
                        ).fetchone(),
                        ("non-demo-account",),
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT c.consumption_kwh FROM consumption c JOIN meters m ON m.id=c.meter_id "
                            "WHERE m.point_id='non-demo-point'"
                        ).fetchone(),
                        (1.25,),
                    )
                if hold_wal:
                    self.assertGreater(Path(str(database) + "-wal").stat().st_size, 0)
                self.assertFalse(DEMO._is_demo_database(database))
                self.assert_force_refuses_without_changes(database, output, cli=hold_wal)

    def test_force_requires_exact_demo_identities_and_no_other_identity_storage(self):
        source, output = self.root / "source.sqlite3", self.root / "site"
        DEMO.generate_demo(source, output)
        pristine = source.read_bytes()
        mutations = {
            "extra-account": "INSERT INTO accounts VALUES('non-demo-account','{}','test')",
            "similar-demo-account": "UPDATE accounts SET account_number='DEMO-ACCOUNT-NOT-REAL-OTHER'",
            "missing-account": "DELETE FROM accounts",
            "account-payload": """UPDATE accounts SET payload_json='{"account":"non-demo-account"}'""",
            "extra-meter": (
                "INSERT INTO meters(fuel,point_id,serial_number,tariff_code,synced_at) "
                "VALUES('gas','DEMO-OTHER-POINT','DEMO-OTHER-METER','DEMO-OTHER-TARIFF','test')"
            ),
            "meter-point": "UPDATE meters SET point_id='non-demo-point' WHERE id=1",
            "meter-serial": "UPDATE meters SET serial_number='non-demo-meter' WHERE id=1",
            "meter-tariff": "UPDATE meters SET tariff_code='non-demo-tariff' WHERE id=1",
            "missing-meter": "DELETE FROM meters WHERE id=2",
            "other-settings": "INSERT INTO energy_settings VALUES('account_number','non-demo-account')",
            "other-identity-table": (
                "CREATE TABLE other_accounts(account_number TEXT);INSERT INTO other_accounts VALUES('non-demo-account')"
            ),
            "sqlite-like-identity-table": (
                "CREATE TABLE sqliteXaccounts(account_number TEXT);"
                "INSERT INTO sqliteXaccounts VALUES('non-demo-account')"
            ),
            "orphan-consumption": (
                "INSERT INTO consumption VALUES(999,'2026-11-04T12:00:00Z','2026-11-04T12:30:00Z',1)"
            ),
            "orphan-tariff": (
                "INSERT INTO tariff_rates VALUES(999,'standard-unit-rates','2026-11-04T12:00:00Z',NULL,1)"
            ),
            "other-history": "INSERT INTO sync_history(started_at,status) VALUES('test','failed')",
            "other-anomaly": (
                "INSERT INTO anomalies(detected_at,date,fuel,kind,detail) "
                "VALUES('test','2026-11-04','electricity','test','non-demo detail')"
            ),
            "other-recommendation": (
                "INSERT INTO savings_recommendations(created_at,category,recommendation) "
                "VALUES('test','test','non-demo recommendation')"
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                database = self.root / f"{name}.sqlite3"
                database.write_bytes(pristine)
                with contextlib.closing(sqlite3.connect(database)) as conn:
                    conn.executescript(mutation)
                self.assert_force_refuses_without_changes(database, output)

    def test_force_rechecks_identities_after_preflight_before_deleting(self):
        database, output = self.root / "race.sqlite3", self.root / "site"
        DEMO.generate_demo(database, output)
        original_check = DEMO._is_demo_database
        expected = None
        files = {path.name: path.read_bytes() for path in output.iterdir()}

        def add_account_after_preflight(path):
            nonlocal expected
            self.assertTrue(original_check(path))
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.execute("INSERT INTO accounts VALUES('non-demo-account','{}','test')")
                conn.commit()
            expected = self.snapshot(path)
            return True

        with (
            patch.object(DEMO, "_is_demo_database", side_effect=add_account_after_preflight),
            self.assertRaisesRegex(FileExistsError, "not exclusively synthetic demo data"),
        ):
            DEMO.generate_demo(database, output, force=True)
        self.assertIsNotNone(expected)
        self.assertEqual(self.snapshot(database), expected)
        self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, files)


if __name__ == "__main__":
    unittest.main()
