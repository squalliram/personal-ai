import tempfile
import unittest
from pathlib import Path

from octopus_energy.analytics import recompute_daily_costs
from octopus_energy.client import OctopusClient, product_code
from octopus_energy.dashboard import dashboard_data, write_dashboard
from octopus_energy.mcp_server import dispatch
from octopus_energy.storage import connect, upsert_consumption, upsert_meter, upsert_rate


class FakeClient(OctopusClient):
    def __init__(self):
        self.pages = 0

    def get(self, path, params=None):
        self.pages += 1
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
            upsert_consumption(conn, meter, reading)
            upsert_consumption(conn, meter, reading)
            upsert_rate(
                conn,
                meter,
                "standard-unit-rates",
                {"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 10},
            )
            upsert_rate(
                conn,
                meter,
                "standing-charges",
                {"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 50},
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
                {"interval_start": "2026-10-25T01:00:00Z", "interval_end": "2026-10-25T01:30:00Z", "consumption": 1.5},
            )
            upsert_rate(
                conn,
                meter,
                "standard-unit-rates",
                {"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 10},
            )
            upsert_rate(
                conn,
                meter,
                "standing-charges",
                {"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 50},
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
