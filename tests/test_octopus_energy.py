import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from octopus_energy import cli
from octopus_energy.analytics import recompute_daily_costs
from octopus_energy.client import PAGE_SIZE, Meter, OctopusClient, product_code
from octopus_energy.dashboard import dashboard_data, write_dashboard
from octopus_energy.mcp_server import dispatch
from octopus_energy.storage import connect, upsert_consumption, upsert_meter, upsert_rate

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

    def tariff_rates(self, meter, kind, period_from=None):
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
        result = cli.sync(self.db, ACCOUNT, "key", days=30)
        self.assertEqual((result["electricity"], result["gas"]), (2, 1))
        expected = datetime.now(timezone.utc) - timedelta(days=30)
        for period_from in self.period_froms().values():
            actual = datetime.fromisoformat(period_from.replace("Z", "+00:00"))
            self.assertLess(abs(actual - expected), timedelta(minutes=1))

        FakeSyncClient.calls = []
        FakeSyncClient.readings["electricity"].append(half_hour("2026-11-01T01:00:00Z", "2026-11-01T01:30:00Z"))
        result = cli.sync(self.db, ACCOUNT, "key", days=30)
        self.assertEqual(
            self.period_froms(),
            {
                ("electricity", "consumption"): "2026-11-01T00:30:00Z",
                ("gas", "consumption"): "2026-11-01T00:00:00Z",
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
