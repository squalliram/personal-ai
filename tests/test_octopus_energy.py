import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from octopus_energy import cli
from octopus_energy.analytics import recompute_daily_costs
from octopus_energy.client import PAGE_SIZE, Agreement, Meter, OctopusClient, product_code
from octopus_energy.dashboard import dashboard_data, write_dashboard
from octopus_energy.mcp_server import dispatch
from octopus_energy.storage import connect, upsert_agreements, upsert_consumption, upsert_meter, upsert_rate

ACCOUNT = "A-PRIV4TE1"
MPAN = "1900000000001"
MPRN = "9876543210"
ELECTRIC_SERIAL = "21E5PRIVATE"
GAS_SERIAL = "G4PRIVATE77"
IDENTIFIERS = (ACCOUNT, MPAN, MPRN, ELECTRIC_SERIAL, GAS_SERIAL)


class FakeClient(OctopusClient):
    def __init__(self):
        self.pages = 0
        self.params = []

    def get(self, path, params=None):
        self.pages += 1
        self.params.append(params)
        return {"results": [{"n": self.pages}], "next": "https://next" if self.pages == 1 else None}


class EnergyTests(unittest.TestCase):
    def test_product_code_and_basic_auth(self):
        self.assertEqual(product_code("E-1R-GO-V1-22-01-01-A"), "GO-V1-22-01-01")
        self.assertTrue(OctopusClient("secret")._auth.startswith("Basic "))

    def test_pagination(self):
        self.assertEqual([x["n"] for x in FakeClient().paginated("anything")], [1, 2])

    def test_idempotent_cost_reconciliation_and_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "energy.sqlite3"
            conn = connect(db)
            meter = upsert_meter(
                conn,
                fuel="electricity",
                point_id="123",
                serial="abc",
                tariff_code="E-1R-GO-V1-22-01-01-A",
                valid_from=None,
                valid_to=None,
                synced_at="2026-01-01T00:00:00Z",
            )
            reading = {
                "interval_start": "2026-10-25T00:30:00Z",
                "interval_end": "2026-10-25T01:00:00Z",
                "consumption": 2.0,
            }
            upsert_consumption(conn, meter, [reading])
            upsert_consumption(conn, meter, [reading])
            upsert_rate(
                conn,
                meter,
                "standard-unit-rates",
                [{"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 10}],
            )
            upsert_rate(
                conn,
                meter,
                "standing-charges",
                [{"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 50}],
            )
            recompute_daily_costs(conn)
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM consumption").fetchone()[0], 1)
            self.assertAlmostEqual(conn.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0], 0.7)
            conn.close()
            self.assertEqual(dispatch("get_energy_summary", {}, db)[0]["total_cost_gbp"], 0.7)

    def test_costs_use_the_tariff_active_for_each_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "energy.sqlite3")
            meter = upsert_meter(
                conn,
                fuel="electricity",
                point_id="123",
                serial="abc",
                tariff_code="E-1R-NEW-26-01-01-A",
                valid_from="2026-11-01T00:00:00Z",
                valid_to=None,
                synced_at="2026-11-05T00:00:00Z",
            )
            upsert_agreements(conn, meter, [("E-1R-OLD-26-01-01-A", "2026-01-01T00:00:00Z", "2026-11-01T00:00:00Z")])
            # The old tariff's open-ended rate starts later than the new tariff's rate but must not apply after the switch.
            upsert_rate(conn, meter, "standard-unit-rates", [rate("2026-10-15T00:00:00Z", 30)], "E-1R-OLD-26-01-01-A")
            upsert_rate(conn, meter, "standard-unit-rates", [rate("2026-06-01T00:00:00Z", 20)], "E-1R-NEW-26-01-01-A")
            upsert_consumption(
                conn,
                meter,
                [
                    half_hour("2026-10-20T12:00:00Z", "2026-10-20T12:30:00Z"),
                    half_hour("2026-11-02T12:00:00Z", "2026-11-02T12:30:00Z"),
                ],
            )
            recompute_daily_costs(conn)
            costs = dict(conn.execute("SELECT date, unit_cost_gbp FROM daily_costs").fetchall())
            conn.close()
            self.assertEqual(costs, {"2026-10-20": 0.30, "2026-11-02": 0.20})

    def test_rate_lookup_compares_offset_and_utc_timestamps_as_instants(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "energy.sqlite3")
            meter = upsert_meter(
                conn,
                fuel="electricity",
                point_id="123",
                serial="abc",
                tariff_code="E-1R-AGILE-24-10-01-A",
                valid_from="2026-01-01T00:00:00Z",
                valid_to=None,
                synced_at="2026-11-05T00:00:00Z",
            )
            early = {"valid_from": "2026-06-01T00:00:00Z", "valid_to": "2026-06-01T00:45:00Z", "value_inc_vat": 10}
            upsert_rate(conn, meter, "standard-unit-rates", [early, rate("2026-06-01T00:45:00Z", 40)])
            # 01:30+01:00 is 00:30Z, inside the early rate; a plain string comparison would pick the later one.
            upsert_consumption(conn, meter, [half_hour("2026-06-01T01:30:00+01:00", "2026-06-01T02:00:00+01:00")])
            recompute_daily_costs(conn)
            stored = conn.execute("SELECT interval_start FROM consumption").fetchone()[0]
            cost = conn.execute("SELECT unit_cost_gbp FROM daily_costs").fetchone()[0]
            conn.close()
            self.assertEqual(stored, "2026-06-01T00:30:00Z")
            self.assertEqual(cost, 0.10)

    def test_connect_normalizes_readings_stored_with_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "energy.sqlite3"
            conn = connect(db)
            meter = upsert_meter(
                conn,
                fuel="electricity",
                point_id="123",
                serial="abc",
                tariff_code=None,
                valid_from=None,
                valid_to=None,
                synced_at="2026-11-05T00:00:00Z",
            )
            conn.execute(
                "INSERT INTO consumption VALUES(?,?,?,?)",
                (meter, "2026-06-01T01:30:00+01:00", "2026-06-01T02:00:00+01:00", 1.0),
            )
            conn.execute(
                "INSERT INTO consumption VALUES(?,?,?,?)", (meter, "2026-06-01T00:30:00Z", "2026-06-01T01:00:00Z", 1.5)
            )
            conn.commit()
            conn.close()
            conn = connect(db)
            rows = conn.execute("SELECT interval_start, interval_end, consumption_kwh FROM consumption").fetchall()
            conn.close()
            self.assertEqual([tuple(r) for r in rows], [("2026-06-01T00:30:00Z", "2026-06-01T01:00:00Z", 1.5)])

    def test_connect_drops_rates_stored_without_tariff_code(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "energy.sqlite3"
            legacy = sqlite3.connect(db)
            legacy.execute(
                "CREATE TABLE tariff_rates (meter_id INTEGER, kind TEXT, valid_from TEXT, valid_to TEXT, value_inc_vat REAL)"
            )
            legacy.execute("INSERT INTO tariff_rates VALUES (1, 'standing-charges', '2026-01-01T00:00:00Z', NULL, 50)")
            legacy.commit()
            legacy.close()
            conn = connect(db)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tariff_rates)")}
            count = conn.execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0]
            conn.close()
            self.assertIn("tariff_code", columns)
            self.assertEqual(count, 0)

    def test_meters_keep_agreement_history(self):
        account = {
            "properties": [
                {
                    "electricity_meter_points": [
                        {
                            "mpan": MPAN,
                            "meters": [{"serial_number": ELECTRIC_SERIAL}],
                            "agreements": [
                                {"tariff_code": "E-1R-OLD-26-01-01-A", "valid_from": "2026-01-01T00:00:00Z"},
                                {"tariff_code": "E-1R-NEW-26-01-01-A", "valid_from": "2026-11-01T00:00:00Z"},
                            ],
                        }
                    ]
                }
            ]
        }
        (meter,) = OctopusClient.meters(account)
        self.assertEqual(meter.tariff_code, "E-1R-NEW-26-01-01-A")
        self.assertEqual(
            meter.agreements,
            (
                Agreement("E-1R-OLD-26-01-01-A", "2026-01-01T00:00:00Z", None),
                Agreement("E-1R-NEW-26-01-01-A", "2026-11-01T00:00:00Z", None),
            ),
        )

    def test_rejects_unknown_mcp_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                dispatch("write_energy", {}, Path(directory) / "db.sqlite3")

    def test_dashboard_export_omits_identifiers_and_writes_static_files(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "energy.sqlite3"
            conn = connect(db)
            meter = upsert_meter(
                conn,
                fuel="electricity",
                point_id="private-point",
                serial="private-serial",
                tariff_code="E-1R-GO-V1-22-01-01-A",
                valid_from=None,
                valid_to=None,
                synced_at="2026-01-01T00:00:00Z",
            )
            upsert_consumption(
                conn,
                meter,
                [
                    {
                        "interval_start": "2026-10-25T01:00:00Z",
                        "interval_end": "2026-10-25T01:30:00Z",
                        "consumption": 1.5,
                    }
                ],
            )
            upsert_rate(
                conn,
                meter,
                "standard-unit-rates",
                [{"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 10}],
            )
            upsert_rate(
                conn,
                meter,
                "standing-charges",
                [{"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 50}],
            )
            recompute_daily_costs(conn)
            conn.commit()
            conn.close()
            data = dashboard_data(db)
            self.assertNotIn("private-point", str(data))
            self.assertNotIn("private-serial", str(data))
            output = Path(directory) / "dashboard"
            index = write_dashboard(db, output)
            self.assertTrue(index.exists())
            self.assertTrue((output / "data.json").exists())


class FakeSyncClient:
    """Stands in for OctopusClient in sync(); records the period_from of every request."""

    calls: list[tuple[str, str, str]] = []
    readings: dict[str, list[dict]] = {}
    rates: dict[str, list[dict]] = {}

    def __init__(self, api_key):
        pass

    def account(self, account_number):
        return {"number": account_number}

    def meters(self, account):
        return [
            Meter("electricity", MPAN, ELECTRIC_SERIAL, "E-1R-GO-V1-22-01-01-A", None, None),
            Meter("gas", MPRN, GAS_SERIAL, "G-1R-GO-V1-22-01-01-A", None, None),
        ]

    def consumption(self, meter, period_from=None):
        self.calls.append((meter.fuel, "consumption", period_from))
        return iter(self.readings.get(meter.fuel, []))

    def tariff_rates(self, meter, kind, period_from=None, period_to=None, tariff_code=None):
        self.calls.append((meter.fuel, kind, period_from))
        return iter(self.rates.get(kind, []))


def half_hour(start: str, end: str, kwh: float = 1.0) -> dict:
    return {"interval_start": start, "interval_end": end, "consumption": kwh}


def rate(valid_from: str, value: float) -> dict:
    return {"valid_from": valid_from, "valid_to": None, "value_inc_vat": value}


class SyncTests(unittest.TestCase):
    def setUp(self):
        FakeSyncClient.calls = []
        FakeSyncClient.readings = {
            "electricity": [
                half_hour("2026-11-01T00:00:00Z", "2026-11-01T00:30:00Z"),
                half_hour("2026-11-01T00:30:00Z", "2026-11-01T01:00:00Z"),
            ],
            "gas": [half_hour("2026-11-01T00:00:00Z", "2026-11-01T00:30:00Z", 2.0)],
        }
        FakeSyncClient.rates = {
            "standard-unit-rates": [rate("2026-10-01T00:00:00Z", 10), rate("2026-11-01T00:00:00Z", 12)],
            "standing-charges": [rate("2026-04-01T00:00:00Z", 50)],
        }
        patcher = patch.object(cli, "OctopusClient", FakeSyncClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "energy.sqlite3"

    def period_froms(self) -> dict[tuple[str, str], str]:
        return {(fuel, kind): period_from for fuel, kind, period_from in FakeSyncClient.calls}

    def test_first_sync_uses_days_window_then_resumes_from_watermarks(self):
        result = cli.sync(self.db, ACCOUNT, "key", days=3650)
        self.assertEqual((result["electricity"], result["gas"]), (2, 1))
        expected = datetime.now(timezone.utc) - timedelta(days=3650)
        for period_from in self.period_froms().values():
            actual = datetime.fromisoformat(period_from.replace("Z", "+00:00"))
            self.assertLess(abs(actual - expected), timedelta(minutes=1))

        FakeSyncClient.calls = []
        FakeSyncClient.readings["electricity"].append(half_hour("2026-11-01T01:00:00Z", "2026-11-01T01:30:00Z"))
        result = cli.sync(self.db, ACCOUNT, "key", days=3650)
        self.assertEqual(
            self.period_froms(),
            {
                ("electricity", "consumption"): "2026-10-25T00:30:00Z",
                ("gas", "consumption"): "2026-10-25T00:00:00Z",
                ("electricity", "standard-unit-rates"): "2026-11-01T00:00:00Z",
                ("gas", "standard-unit-rates"): "2026-11-01T00:00:00Z",
                ("electricity", "standing-charges"): "2026-04-01T00:00:00Z",
                ("gas", "standing-charges"): "2026-04-01T00:00:00Z",
            },
        )
        conn = connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM consumption").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0], 6)
        finally:
            conn.close()

    def test_rate_requests_follow_each_agreements_own_window(self):
        earlier = Agreement("E-1R-GO-V1-22-01-01-A", "2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z")
        old = Agreement("E-1R-OLD-26-01-01-A", "2026-03-01T00:00:00Z", "2026-11-01T00:00:00Z")
        current = Agreement("E-1R-GO-V1-22-01-01-A", "2026-11-01T00:00:00Z", None)
        history = (earlier, old, current)
        meters = [Meter("electricity", MPAN, ELECTRIC_SERIAL, current.tariff_code, current.valid_from, None, history)]
        conn = connect(self.db)
        meter = upsert_meter(
            conn,
            fuel="electricity",
            point_id=MPAN,
            serial=ELECTRIC_SERIAL,
            tariff_code=current.tariff_code,
            valid_from=current.valid_from,
            valid_to=None,
            synced_at="2026-11-05T00:00:00Z",
        )
        upsert_agreements(conn, meter, [(a.tariff_code, a.valid_from, a.valid_to) for a in history])
        for kind in ("standard-unit-rates", "standing-charges"):
            # The old agreement's rates already run to its end; the current tariff's rates don't cover `earlier`.
            upsert_rate(conn, meter, kind, [rate("2026-10-15T00:00:00Z", 30)], old.tariff_code)
            upsert_rate(conn, meter, kind, [rate("2026-11-01T00:00:00Z", 20)], current.tariff_code)
        conn.commit()
        conn.close()
        requested = []

        def tariff_rates(client, meter, kind, period_from=None, period_to=None, tariff_code=None):
            requested.append((tariff_code, period_from, period_to))
            return iter([])

        with (
            patch.object(FakeSyncClient, "meters", lambda client, account: meters),
            patch.object(FakeSyncClient, "tariff_rates", tariff_rates),
        ):
            cli.sync(self.db, ACCOUNT, "key", days=3650)
        self.assertEqual(
            requested,
            [
                (earlier.tariff_code, earlier.valid_from, earlier.valid_to),
                (earlier.tariff_code, earlier.valid_from, earlier.valid_to),
                (current.tariff_code, current.valid_from, None),
                (current.tariff_code, current.valid_from, None),
            ],
        )

    def test_consumption_lookback_is_clamped_to_days_window(self):
        resume = cli._consumption_resume_from
        self.assertEqual(resume(None, "2026-08-01T00:00:00Z"), "2026-08-01T00:00:00Z")
        self.assertEqual(resume("2026-11-01T00:30:00Z", "2026-08-01T00:00:00Z"), "2026-10-25T00:30:00Z")
        self.assertEqual(resume("2026-11-01T01:30:00+01:00", "2026-08-01T00:00:00Z"), "2026-10-25T00:30:00Z")
        self.assertEqual(resume("2026-11-01T00:30:00Z", "2026-10-30T00:00:00Z"), "2026-10-30T00:00:00Z")
        self.assertEqual(resume("2026-06-01T00:00:00Z", "2026-08-01T00:00:00Z"), "2026-06-01T00:00:00Z")

    def test_dashboard_data_json_and_cli_output_contain_no_identifiers(self):
        env_file = self.root / ".env"
        env_file.write_text(f"ACCOUNT_NUMBER={ACCOUNT}\nAPI_KEY=sk_live_private\n", encoding="utf-8")
        dashboard = self.root / "dashboard"
        argv = ["cli", "--database", str(self.db), "--env-file", str(env_file), "--days", "30"]
        argv += ["--report-dir", str(self.root / "reports"), "--dashboard-dir", str(dashboard)]
        stdout = io.StringIO()
        with patch("sys.argv", argv), patch.dict("os.environ", {}, clear=True), redirect_stdout(stdout):
            self.assertEqual(cli.main(), 0)
        data_json = (dashboard / "data.json").read_text(encoding="utf-8")
        self.assertIn('"daily"', data_json)
        for text in (data_json, stdout.getvalue()):
            for identifier in (*IDENTIFIERS, "sk_live_private"):
                self.assertNotIn(identifier, text)


class ProductCodeTests(unittest.TestCase):
    def test_valid_tariff_codes(self):
        self.assertEqual(product_code("E-1R-AGILE-24-10-01-A"), "AGILE-24-10-01")
        self.assertEqual(product_code("G-1R-VAR-22-11-01-P"), "VAR-22-11-01")
        self.assertEqual(product_code("E-2R-SILVER-FLEX-22-11-25-C"), "SILVER-FLEX-22-11-25")

    def test_rejects_malformed_tariff_codes(self):
        for code in (
            "",
            "E-1R",
            "X-1R-AGILE-24-10-01-A",
            "e-1r-agile-24-10-01-a",
            "E-1R-AGILE",
            "E-1R-AGILE-24-10-01-AB",
            "E-1R-AGILE-24-10-01-",
        ):
            with self.subTest(code=code), self.assertRaises(ValueError):
                product_code(code)


class PaginationParamsTests(unittest.TestCase):
    def test_page_size_added_without_overriding_caller(self):
        client = FakeClient()
        list(client.paginated("anything", {"period_from": "2026-01-01T00:00:00Z"}))
        self.assertEqual(client.params[0], {"page_size": str(PAGE_SIZE), "period_from": "2026-01-01T00:00:00Z"})
        self.assertIsNone(client.params[1])
        client = FakeClient()
        list(client.paginated("anything", {"page_size": "100"}))
        self.assertEqual(client.params[0], {"page_size": "100"})
