import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from octopus_energy import cli
from octopus_energy.analytics import (
    _rate,
    anomalies,
    expensive_days,
    overnight_baseline,
    recompute_daily_costs,
    summary,
)
from octopus_energy.client import Meter
from octopus_energy.reports import write_reports
from octopus_energy.storage import connect, get_gas_units, upsert_consumption, upsert_meter, upsert_rate


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "energy.sqlite3"
        self.conn = connect(self.database)
        self.addCleanup(self.conn.close)

    def meter(
        self, fuel="electricity", point="synthetic-point", serial="synthetic-meter", valid_from=None, valid_to=None
    ):
        return upsert_meter(
            self.conn,
            fuel=fuel,
            point_id=point,
            serial=serial,
            tariff_code=None,
            valid_from=valid_from,
            valid_to=valid_to,
            synced_at="2026-11-01T00:00:00Z",
        )

    def reading(self, meter, start="2026-11-01T00:00:00Z", end="2026-11-01T00:30:00Z", value=2):
        upsert_consumption(
            self.conn,
            meter,
            {
                "interval_start": start,
                "interval_end": end,
                "consumption": value,
            },
        )

    def rate(self, meter, kind="standard-unit-rates", value=10, start="2026-01-01T00:00:00Z", end=None):
        upsert_rate(
            self.conn,
            meter,
            kind,
            {
                "valid_from": start,
                "valid_to": end,
                "value_inc_vat": value,
            },
        )

    def tariffs(self, meter, unit=10, standing=50):
        self.rate(meter, value=unit)
        self.rate(meter, "standing-charges", standing)

    def daily(self):
        recompute_daily_costs(self.conn)
        return [dict(row) for row in self.conn.execute("SELECT * FROM daily_costs ORDER BY date,fuel")]

    def test_missing_unit_rate_is_not_free_energy(self):
        meter = self.meter()
        self.reading(meter)
        self.rate(meter, "standing-charges", 50)
        row = self.daily()[0]
        self.assertEqual(row["consumption_kwh"], 2)
        self.assertIsNone(row["unit_cost_gbp"])
        self.assertEqual(row["standing_charge_gbp"], 0.5)
        self.assertIsNone(row["total_cost_gbp"])
        self.assertEqual(expensive_days(self.conn), [])
        self.assertIsNone(summary(self.conn)[0]["total_cost_gbp"])

    def test_missing_standing_rate_makes_total_unavailable(self):
        meter = self.meter()
        self.reading(meter)
        self.rate(meter)
        row = self.daily()[0]
        self.assertEqual(row["unit_cost_gbp"], 0.2)
        self.assertIsNone(row["standing_charge_gbp"])
        self.assertIsNone(row["total_cost_gbp"])

    def test_real_zero_and_negative_rates_are_valid(self):
        meter = self.meter()
        self.reading(meter)
        self.tariffs(meter, unit=0, standing=0)
        self.assertEqual(self.daily()[0]["total_cost_gbp"], 0)
        self.tariffs(meter, unit=-10, standing=0)
        self.assertEqual(self.daily()[0]["total_cost_gbp"], -0.2)

    def test_any_missing_interval_invalidates_whole_daily_unit_cost(self):
        meter = self.meter()
        self.reading(meter)
        self.reading(meter, "2026-11-01T00:30:00Z", "2026-11-01T01:00:00Z")
        self.rate(meter, end="2026-11-01T00:30:00Z")
        self.rate(meter, "standing-charges", 50)
        row = self.daily()[0]
        self.assertEqual(row["consumption_kwh"], 4)
        self.assertIsNone(row["unit_cost_gbp"])
        self.assertIsNone(row["total_cost_gbp"])

    def test_mixed_offset_rate_start_and_end_are_compared_as_instants(self):
        meter = self.meter()
        self.reading(meter, "2026-07-01T01:00:00+01:00", "2026-07-01T01:30:00+01:00")
        self.rate(meter, value=10, start="2026-06-30T23:00:00Z", end="2026-07-01T00:30:00Z")
        self.rate(meter, value=90, start="2026-07-01T01:30:00+01:00")
        self.rate(meter, "standing-charges", 50)
        self.assertAlmostEqual(self.daily()[0]["total_cost_gbp"], 0.7)
        self.assertEqual(_rate(self.conn, meter, "standard-unit-rates", "2026-07-01T00:30:00Z"), 90)

    def test_half_open_validity_does_not_include_expired_rate(self):
        meter = self.meter()
        self.rate(meter, end="2026-11-01T00:00:00Z")
        self.assertIsNone(_rate(self.conn, meter, "standard-unit-rates", "2026-11-01T00:00:00Z"))

    def test_rate_must_cover_entire_interval_without_gap(self):
        meter = self.meter()
        self.reading(meter)
        self.rate(meter, end="2026-11-01T00:15:00Z")
        self.rate(meter, start="2026-11-01T00:20:00Z")
        self.rate(meter, "standing-charges", 50)
        self.assertIsNone(self.daily()[0]["total_cost_gbp"])

    def test_rate_change_inside_reading_is_not_arbitrarily_allocated(self):
        meter = self.meter()
        self.reading(meter)
        self.rate(meter, value=10, end="2026-11-01T00:15:00Z")
        self.rate(meter, value=20, start="2026-11-01T00:15:00Z")
        self.rate(meter, "standing-charges", 50)
        self.assertIsNone(self.daily()[0]["total_cost_gbp"])

    def test_adjacent_identical_rates_cover_a_reading(self):
        meter = self.meter()
        self.reading(meter)
        self.rate(meter, end="2026-11-01T00:15:00Z")
        self.rate(meter, start="2026-11-01T00:15:00Z")
        self.rate(meter, "standing-charges", 50)
        self.assertAlmostEqual(self.daily()[0]["total_cost_gbp"], 0.7)

    def test_historical_meter_without_readings_adds_no_standing(self):
        old = self.meter(serial="old")
        current = self.meter(serial="current", valid_from="2026-11-02T00:00:00Z")
        self.tariffs(old, standing=80)
        self.tariffs(current, standing=50)
        self.reading(current)
        # The selected agreement date is not the meter's installation date.
        self.assertAlmostEqual(self.daily()[0]["total_cost_gbp"], 0.7)

    def test_replacement_meters_share_one_supply_standing_charge(self):
        first = self.meter(serial="first")
        second = self.meter(serial="second")
        for meter in (first, second):
            self.tariffs(meter)
        self.reading(first)
        self.reading(second, "2026-11-01T00:30:00Z", "2026-11-01T01:00:00Z")
        row = self.daily()[0]
        self.assertEqual(row["standing_charge_gbp"], 0.5)
        self.assertAlmostEqual(row["total_cost_gbp"], 0.9)

    def test_distinct_supply_points_have_distinct_standing_charges(self):
        first, second = self.meter(point="first"), self.meter(point="second")
        for meter in (first, second):
            self.tariffs(meter)
            self.reading(meter)
        self.assertAlmostEqual(self.daily()[0]["total_cost_gbp"], 1.4)

    def test_standing_charge_uses_london_midnight(self):
        meter = self.meter()
        self.reading(meter, "2026-07-01T01:00:00+01:00", "2026-07-01T01:30:00+01:00")
        self.rate(meter)
        self.rate(meter, "standing-charges", 40, end="2026-07-01T00:00:00Z")
        self.rate(meter, "standing-charges", 80, start="2026-07-01T00:00:00Z")
        row = self.daily()[0]
        self.assertEqual(row["date"], "2026-07-01")
        self.assertAlmostEqual(row["standing_charge_gbp"], 0.4)

    def test_unknown_gas_volume_is_never_reported_or_costed_as_kwh(self):
        meter = self.meter(fuel="gas")
        self.tariffs(meter)
        for day in range(1, 9):
            self.reading(meter, f"2026-11-{day:02}T00:00:00Z", f"2026-11-{day:02}T00:30:00Z", 100 if day == 8 else 1)
        rows = self.daily()
        self.assertTrue(all(row["consumption_kwh"] is None for row in rows))
        self.assertTrue(all(row["total_cost_gbp"] is None for row in rows))
        self.assertEqual(
            summary(self.conn),
            [
                {
                    "fuel": "gas",
                    "consumption_kwh": None,
                    "total_cost_gbp": None,
                }
            ],
        )
        self.assertEqual(overnight_baseline(self.conn), [])
        self.assertEqual(anomalies(self.conn), [])
        self.assertEqual(expensive_days(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT SUM(consumption_kwh) FROM consumption").fetchone()[0], 107)

    def test_period_summary_never_silently_sums_only_known_costs(self):
        meter = self.meter()
        self.tariffs(meter)
        self.reading(meter)
        self.reading(meter, "2026-11-02T00:00:00Z", "2026-11-02T00:30:00Z")
        self.conn.execute("UPDATE tariff_rates SET valid_to='2026-11-02T00:00:00Z' WHERE kind='standard-unit-rates'")
        self.daily()
        self.assertEqual(summary(self.conn)[0]["consumption_kwh"], 4)
        self.assertIsNone(summary(self.conn)[0]["total_cost_gbp"])
        self.assertAlmostEqual(summary(self.conn, end="2026-11-02")[0]["total_cost_gbp"], 0.7)
        self.assertIsNone(summary(self.conn, start="2026-11-02")[0]["total_cost_gbp"])
        self.assertEqual(len(expensive_days(self.conn)), 1)

    def test_legacy_not_null_database_is_migrated_and_recomputed_once(self):
        meter = self.meter(fuel="gas")
        self.tariffs(meter)
        self.reading(meter)
        self.conn.execute("DROP TABLE daily_costs")
        self.conn.execute(
            "CREATE TABLE daily_costs (date TEXT NOT NULL, fuel TEXT NOT NULL,"
            " consumption_kwh REAL NOT NULL, unit_cost_gbp REAL NOT NULL,"
            " standing_charge_gbp REAL NOT NULL, total_cost_gbp REAL NOT NULL, PRIMARY KEY(date,fuel))"
        )
        self.conn.execute("INSERT INTO daily_costs VALUES('2026-11-01','gas',2,.2,.5,.7)")
        self.conn.commit()
        with connect(self.database) as migrated:
            self.assertIsNone(migrated.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0])
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM consumption").fetchone()[0], 1)
            self.assertTrue(
                all(
                    not row["notnull"]
                    for row in migrated.execute("PRAGMA table_info(daily_costs)")
                    if row["name"] not in {"date", "fuel"}
                )
            )
        with connect(self.database) as repeated:
            self.assertEqual(repeated.execute("SELECT COUNT(*) FROM daily_costs").fetchone()[0], 1)

    def test_failed_migration_preserves_original_table(self):
        meter = self.meter()
        self.reading(meter, "invalid", "invalid")
        self.conn.execute("DROP TABLE daily_costs")
        self.conn.execute(
            "CREATE TABLE daily_costs (date TEXT NOT NULL, fuel TEXT NOT NULL,"
            " consumption_kwh REAL NOT NULL, unit_cost_gbp REAL NOT NULL,"
            " standing_charge_gbp REAL NOT NULL, total_cost_gbp REAL NOT NULL, PRIMARY KEY(date,fuel))"
        )
        self.conn.execute("INSERT INTO daily_costs VALUES('2026-11-01','electricity',2,.2,.5,.7)")
        self.conn.commit()
        with self.assertRaises(ValueError):
            connect(self.database)
        with sqlite3.connect(self.database) as original:
            self.assertEqual(original.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0], 0.7)
            self.assertIsNone(
                original.execute("SELECT name FROM sqlite_master WHERE name='energy_settings'").fetchone()
            )

    def test_reports_explain_unavailable_values_without_formatting_failure(self):
        meter = self.meter(fuel="gas")
        self.reading(meter)
        self.daily()
        self.conn.commit()
        paths = write_reports(self.database, Path(self.directory.name) / "reports")
        self.assertIn("gas source units unverified", paths[0].read_text())
        self.assertIn("cost unavailable/incomplete", paths[0].read_text())
        self.assertIn("No complete cost data", paths[1].read_text())

    def test_confirmed_gas_kwh_uses_same_tariff_and_standing_rules(self):
        first = self.meter(fuel="gas", serial="first")
        second = self.meter(fuel="gas", serial="second")
        self.tariffs(first)
        self.tariffs(second)
        self.reading(first)
        self.reading(second, "2026-11-01T00:30:00Z", "2026-11-01T01:00:00Z")
        recompute_daily_costs(self.conn, gas_units="kwh")
        row = dict(self.conn.execute("SELECT * FROM daily_costs").fetchone())
        self.assertEqual(row["consumption_kwh"], 4)
        self.assertAlmostEqual(row["unit_cost_gbp"], 0.4)
        self.assertEqual(row["standing_charge_gbp"], 0.5)
        self.assertAlmostEqual(row["total_cost_gbp"], 0.9)
        self.assertEqual(get_gas_units(self.conn), "kwh")
        self.assertEqual(overnight_baseline(self.conn)[0]["fuel"], "gas")
        self.assertEqual(expensive_days(self.conn)[0]["fuel"], "gas")

    def test_confirmed_gas_missing_rates_keeps_kwh_and_null_costs(self):
        meter = self.meter(fuel="gas")
        self.reading(meter)
        self.rate(meter, "standing-charges", 50)
        recompute_daily_costs(self.conn, gas_units="kwh")
        row = dict(self.conn.execute("SELECT * FROM daily_costs").fetchone())
        self.assertEqual(row["consumption_kwh"], 2)
        self.assertIsNone(row["unit_cost_gbp"])
        self.assertEqual(row["standing_charge_gbp"], 0.5)
        self.assertIsNone(row["total_cost_gbp"])
        self.assertEqual(overnight_baseline(self.conn)[0]["fuel"], "gas")
        self.conn.execute("DELETE FROM tariff_rates")
        self.rate(meter)
        recompute_daily_costs(self.conn, gas_units="kwh")
        row = dict(self.conn.execute("SELECT * FROM daily_costs").fetchone())
        self.assertAlmostEqual(row["unit_cost_gbp"], 0.2)
        self.assertIsNone(row["standing_charge_gbp"])
        self.assertIsNone(row["total_cost_gbp"])

    def test_confirmed_gas_participates_in_anomaly_analysis(self):
        meter = self.meter(fuel="gas")
        for day in range(1, 9):
            self.reading(meter, f"2026-11-{day:02}T00:00:00Z", f"2026-11-{day:02}T00:30:00Z", 100 if day == 8 else 1)
        recompute_daily_costs(self.conn, gas_units="kwh")
        self.assertEqual(anomalies(self.conn, "gas")[0]["date"], "2026-11-08")

    def test_report_recompute_switches_existing_gas_both_ways_and_preserves_mode(self):
        meter = self.meter(fuel="gas")
        self.reading(meter)
        self.tariffs(meter)
        self.conn.commit()
        output = Path(self.directory.name) / "reports"
        write_reports(self.database, output)
        self.assertIsNone(self.conn.execute("SELECT consumption_kwh FROM daily_costs").fetchone()[0])
        raw = [tuple(row) for row in self.conn.execute("SELECT * FROM consumption")]
        paths = write_reports(self.database, output, gas_units="kwh")
        self.assertEqual(self.conn.execute("SELECT consumption_kwh FROM daily_costs").fetchone()[0], 2)
        self.assertAlmostEqual(self.conn.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0], 0.7)
        self.assertIn("Gas readings are configured as kWh", paths[0].read_text())
        write_reports(self.database, output, gas_units=None)
        self.assertEqual(get_gas_units(self.conn), "kwh")
        self.assertEqual(self.conn.execute("SELECT consumption_kwh FROM daily_costs").fetchone()[0], 2)
        write_reports(self.database, output, gas_units="unknown")
        self.assertEqual(get_gas_units(self.conn), "unknown")
        self.assertIsNone(self.conn.execute("SELECT consumption_kwh FROM daily_costs").fetchone()[0])
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM consumption")], raw)

    def test_recompute_failure_rolls_back_metadata_creation_and_derived_data(self):
        meter = self.meter(fuel="gas")
        self.reading(meter, "invalid", "invalid")
        self.conn.execute("INSERT INTO daily_costs VALUES('2026-11-01','electricity',2,.2,.5,.7)")
        self.conn.commit()
        with self.assertRaises(ValueError):
            recompute_daily_costs(self.conn, gas_units="kwh")
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='energy_settings'").fetchone())
        self.assertEqual(get_gas_units(self.conn), "unknown")
        self.assertAlmostEqual(self.conn.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0], 0.7)

    def test_invalid_gas_units_fail_before_mutating_database_or_reports(self):
        meter = self.meter(fuel="gas")
        self.reading(meter)
        recompute_daily_costs(self.conn, gas_units="kwh")
        self.conn.commit()
        for value in ("m3", "", "KWH"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    recompute_daily_costs(self.conn, gas_units=value)
                output = Path(self.directory.name) / "unused-reports"
                with self.assertRaises(ValueError):
                    write_reports(self.database, output, gas_units=value)
                self.assertFalse(output.exists())
                self.assertEqual(get_gas_units(self.conn), "kwh")
                self.assertEqual(self.conn.execute("SELECT consumption_kwh FROM daily_costs").fetchone()[0], 2)

    def test_failed_switch_preserves_existing_unit_metadata_and_costs(self):
        meter = self.meter(fuel="gas")
        self.reading(meter)
        self.tariffs(meter)
        recompute_daily_costs(self.conn, gas_units="kwh")
        self.conn.commit()
        original = [tuple(row) for row in self.conn.execute("SELECT * FROM daily_costs")]
        self.reading(meter, "invalid", "invalid")
        self.conn.commit()
        with self.assertRaises(ValueError):
            recompute_daily_costs(self.conn, gas_units="unknown")
        self.assertEqual(get_gas_units(self.conn), "kwh")
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM daily_costs")], original)

    def test_storage_accepts_single_dict_and_batched_iterables_with_counts(self):
        meter = self.meter()
        first = {
            "interval_start": "2026-11-01T00:00:00Z",
            "interval_end": "2026-11-01T00:30:00Z",
            "consumption": 1,
        }
        second = {
            "interval_start": "2026-11-01T00:30:00Z",
            "interval_end": "2026-11-01T01:00:00Z",
            "consumption": 2,
        }
        self.assertEqual(upsert_consumption(self.conn, meter, first), 1)
        self.assertEqual(upsert_consumption(self.conn, meter, [first, second]), 2)
        self.assertEqual(upsert_consumption(self.conn, meter, (row for row in [first, second])), 2)
        self.assertEqual(upsert_consumption(self.conn, meter, []), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM consumption").fetchone()[0], 2)
        first_rate = {"valid_from": "2026-01-01T00:00:00Z", "value_inc_vat": 10}
        second_rate = {"valid_from": "2026-02-01T00:00:00Z", "valid_to": None, "value_inc_vat": 12}
        self.assertEqual(upsert_rate(self.conn, meter, "standard-unit-rates", first_rate), 1)
        self.assertEqual(upsert_rate(self.conn, meter, "standard-unit-rates", [first_rate, second_rate]), 2)
        self.assertEqual(
            upsert_rate(self.conn, meter, "standard-unit-rates", (row for row in [first_rate, second_rate])), 2
        )
        self.assertEqual(upsert_rate(self.conn, meter, "standard-unit-rates", iter(())), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0], 2)

    def test_batched_storage_preserves_callers_transaction_boundary(self):
        meter = self.meter()
        self.conn.commit()
        self.reading(meter)
        self.tariffs(meter)
        with sqlite3.connect(self.database) as observer:
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM consumption").fetchone()[0], 0)
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0], 0)
            self.assertTrue(self.conn.in_transaction)
            self.conn.commit()
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM consumption").fetchone()[0], 1)
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0], 2)
        self.assertEqual(
            upsert_consumption(
                self.conn,
                meter,
                [
                    {
                        "interval_start": "2026-11-01T00:00:00Z",
                        "interval_end": "2026-11-01T00:30:00Z",
                        "consumption": 9,
                    }
                ],
            ),
            1,
        )
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT consumption_kwh FROM consumption").fetchone()[0], 2)

    def test_nullable_migration_preserves_wal_reader_snapshot(self):
        meter = self.meter(fuel="gas")
        self.reading(meter)
        self.conn.execute("DROP TABLE daily_costs")
        self.conn.execute(
            "CREATE TABLE daily_costs (date TEXT NOT NULL, fuel TEXT NOT NULL,"
            " consumption_kwh REAL NOT NULL, unit_cost_gbp REAL NOT NULL,"
            " standing_charge_gbp REAL NOT NULL, total_cost_gbp REAL NOT NULL, PRIMARY KEY(date,fuel))"
        )
        self.conn.execute("INSERT INTO daily_costs VALUES('2026-11-01','gas',2,.2,.5,.7)")
        self.conn.commit()
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        self.conn.execute("BEGIN")
        self.assertEqual(self.conn.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0], 0.7)
        migrated = connect(self.database)
        try:
            self.assertIsNone(migrated.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0])
            self.assertFalse(migrated.in_transaction)
            self.assertEqual(self.conn.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0], 0.7)
            self.conn.rollback()
            self.assertIsNone(self.conn.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0])
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM consumption").fetchone()[0], 1)
        finally:
            migrated.close()


class GasUnitsCLITests(unittest.TestCase):
    def test_invalid_sync_gas_units_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "unused.sqlite3"
            with self.assertRaises(ValueError):
                cli.sync(database, "synthetic", "synthetic", gas_units="m3")
            self.assertFalse(database.exists())

    def test_cli_env_and_dotenv_precedence_and_invalid_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            for file_units, env_units, flag, expected in (
                (None, None, None, "unknown"),
                ("kwh", None, None, "kwh"),
                ("kwh", "unknown", None, "unknown"),
                ("unknown", "kwh", None, "kwh"),
                ("kwh", "kwh", "unknown", "unknown"),
                ("unknown", "unknown", "kwh", "kwh"),
                ("unknown", "m3", "kwh", "kwh"),
            ):
                with self.subTest(file=file_units, env=env_units, flag=flag):
                    env_file.write_text(
                        "ACCOUNT_NUMBER=synthetic\nAPI_KEY=synthetic\n"
                        + (f"GAS_UNITS={file_units}\n" if file_units is not None else "")
                    )
                    env = {"GAS_UNITS": env_units} if env_units is not None else {}
                    args = ["energy", "--env-file", str(env_file)]
                    if flag:
                        args += ["--gas-units", flag]
                    with (
                        patch.dict(os.environ, env, clear=True),
                        patch("sys.argv", args),
                        patch("octopus_energy.cli.sync", return_value={"status": "success"}) as sync,
                        patch("octopus_energy.cli.write_reports") as reports,
                        patch("octopus_energy.cli.write_dashboard"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        self.assertEqual(cli.main(), 0)
                    self.assertEqual(sync.call_args.kwargs["gas_units"], expected)
                    self.assertEqual(reports.call_args.kwargs["gas_units"], expected)
            with (
                patch.dict(os.environ, {"GAS_UNITS": "m3"}, clear=True),
                patch("sys.argv", ["energy", "--env-file", str(env_file)]),
                patch("octopus_energy.cli.sync") as sync,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.main(), 1)
                self.assertIn("failed safely", output.getvalue())
                sync.assert_not_called()
            with (
                patch("sys.argv", ["energy", "--gas-units", "m3"]),
                patch("octopus_energy.cli.sync") as sync,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                cli.main()
            self.assertEqual(error.exception.code, 2)
            sync.assert_not_called()

    def test_unconfigured_cli_reverts_previously_confirmed_gas_to_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "energy.sqlite3"
            args = [
                "energy",
                "--env-file",
                str(root / "missing.env"),
                "--database",
                str(database),
                "--report-dir",
                str(root / "reports"),
                "--dashboard-dir",
                str(root / "dashboard"),
            ]
            reading = {
                "interval_start": "2026-11-01T00:00:00Z",
                "interval_end": "2026-11-01T00:30:00Z",
                "consumption": 2,
            }
            with (
                patch.dict(os.environ, {"ACCOUNT_NUMBER": "synthetic", "API_KEY": "synthetic"}, clear=True),
                patch("octopus_energy.cli.OctopusClient") as client,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                client.return_value.meters.return_value = [Meter("gas", "synthetic", "synthetic", None, None, None)]
                client.return_value.consumption.return_value = [reading]
                client.return_value.tariff_rates.return_value = [
                    {"valid_from": "2020-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 10}
                ]
                with patch("sys.argv", args + ["--gas-units", "kwh"]):
                    self.assertEqual(cli.main(), 0)
                with connect(database) as conn:
                    self.assertEqual(get_gas_units(conn), "kwh")
                    self.assertEqual(conn.execute("SELECT consumption_kwh FROM daily_costs").fetchone()[0], 2)
                with patch("sys.argv", args):
                    self.assertEqual(cli.main(), 0)
                with connect(database) as conn:
                    self.assertEqual(get_gas_units(conn), "unknown")
                    self.assertIsNone(conn.execute("SELECT consumption_kwh FROM daily_costs").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
