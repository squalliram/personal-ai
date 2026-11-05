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
        with sqlite3.connect(database) as conn:
            return tuple(conn.iterdump())

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


if __name__ == "__main__":
    unittest.main()
