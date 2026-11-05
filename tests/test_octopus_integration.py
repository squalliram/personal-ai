import contextlib
import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.request import HTTPSHandler, Request, build_opener
from urllib.response import addinfourl

from octopus_energy.cli import main, sync
from octopus_energy.client import (
    PAGE_SIZE,
    Meter,
    OctopusAPIError,
    OctopusClient,
    TariffAgreement,
    _SameOriginRedirectHandler,
)
from octopus_energy.storage import connect, upsert_consumption, upsert_meter, upsert_rate


class IntegrationTests(unittest.TestCase):
    def test_missing_configuration_fails_without_traceback_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "sys.argv",
                    [
                        "octopus",
                        "--env-file",
                        str(root / "missing.env"),
                        "--database",
                        str(root / "unused.sqlite3"),
                    ],
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(), 1)
            self.assertIn("failed safely", output.getvalue())
            self.assertFalse((root / "unused.sqlite3").exists())

    def test_environment_only_configuration_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / "missing.env"
            for file_present in (False, True):
                if file_present:
                    env_path.write_text("ACCOUNT_NUMBER=file-account\nAPI_KEY=file-key\n", encoding="utf-8")
                with (
                    patch.dict(os.environ, {"ACCOUNT_NUMBER": "env-account", "API_KEY": "env-key"}, clear=True),
                    patch(
                        "sys.argv",
                        [
                            "octopus",
                            "--env-file",
                            str(env_path),
                            "--database",
                            str(root / "db.sqlite3"),
                        ],
                    ),
                    patch("octopus_energy.cli.sync", return_value={"status": "success"}) as run,
                    patch("octopus_energy.cli.write_reports"),
                    patch("octopus_energy.cli.write_dashboard"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(main(), 0)
                    run.assert_called_once_with(root / "db.sqlite3", "env-account", "env-key", 400, gas_units="unknown")

    def test_partial_failed_sync_preserves_previous_data(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "energy.sqlite3"
            conn = connect(database)
            meter_id = upsert_meter(
                conn,
                fuel="electricity",
                point_id="point",
                serial="serial",
                tariff_code=None,
                valid_from=None,
                valid_to=None,
                synced_at="old-sync",
            )
            original = {
                "interval_start": "2026-01-01T00:00:00Z",
                "interval_end": "2026-01-01T00:30:00Z",
                "consumption": 1,
            }
            upsert_consumption(conn, meter_id, original)
            conn.commit()
            conn.close()

            def partial_read(*args, **kwargs):
                yield {**original, "consumption": 2}
                yield {
                    "interval_start": "2026-01-02T00:00:00Z",
                    "interval_end": "2026-01-02T00:30:00Z",
                    "consumption": 3,
                }
                raise OctopusAPIError("simulated safe failure")

            with patch("octopus_energy.cli.OctopusClient") as client:
                client.return_value.meters.return_value = [Meter("electricity", "point", "serial", None, None, None)]
                client.return_value.consumption.side_effect = partial_read
                with self.assertRaises(OctopusAPIError):
                    sync(database, "account", "key", 1)
            conn = connect(database)
            self.assertEqual(conn.execute("SELECT consumption_kwh FROM consumption").fetchall()[0][0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM consumption").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT synced_at FROM meters").fetchone()[0], "old-sync")
            self.assertEqual(
                tuple(conn.execute("SELECT status,error_category,electricity_records FROM sync_history").fetchone()),
                ("failed", "OctopusAPIError", 0),
            )
            conn.close()

    def test_invalid_configuration_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "unused.sqlite3"
            for account, key, days in (
                ("", "key", 1),
                ("account", " ", 1),
                ("account", "key", 0),
                ("account", "key", -1),
            ):
                with self.assertRaises(ValueError):
                    sync(database, account, key, days)
                self.assertFalse(database.exists())

    def test_authenticated_requests_reject_foreign_pagination(self):
        client = OctopusClient("key")
        with patch.object(client._opener, "open") as request:
            for url in (
                "https://untrusted.example/next",
                "http://api.octopus.energy/v1/next",
                "https://user@api.octopus.energy/v1/next",
            ):
                with self.assertRaises(OctopusAPIError):
                    client.get(url)
            request.assert_not_called()

    def test_redirects_cannot_forward_credentials_to_another_origin(self):
        handler = _SameOriginRedirectHandler()
        request = Request("https://api.octopus.energy/v1/start", headers={"Authorization": "Basic test"})
        for url in ("https://untrusted.example/next", "http://api.octopus.energy/next"):
            with self.assertRaises(OctopusAPIError):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://api.octopus.energy/v1/next")
        self.assertEqual(redirected.get_header("Authorization"), "Basic test")

    def test_real_opener_redirect_chain_never_sends_foreign_authorization(self):
        class RedirectTransport(HTTPSHandler):
            def __init__(self, target):
                super().__init__()
                self.target = target
                self.sent = []

            def https_open(self, request):
                self.sent.append((request.full_url, request.get_header("Authorization")))
                headers = Message()
                headers["Location"] = self.target
                response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
                return response

        for target in ("https://untrusted.example/next", "http://api.octopus.energy/next"):
            transport = RedirectTransport(target)
            client = OctopusClient("key")
            client._opener = build_opener(_SameOriginRedirectHandler(), transport)
            with self.assertRaises(OctopusAPIError):
                client.get("start")
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(transport.sent[0][0], "https://api.octopus.energy/v1/start")
            self.assertTrue(transport.sent[0][1].startswith("Basic "))

    def test_successful_get_uses_https_and_basic_auth(self):
        client = OctopusClient("key")
        with patch.object(client._opener, "open") as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps({"results": []}).encode()
            self.assertEqual(client.get("accounts/example/"), {"results": []})
            self.assertEqual(request.call_args.args[0].full_url, "https://api.octopus.energy/v1/accounts/example/")
            self.assertTrue(request.call_args.args[0].get_header("Authorization").startswith("Basic "))

    def test_active_tariff_does_not_depend_on_agreement_order(self):
        active = {"tariff_code": "active", "valid_from": "2020-01-01T00:00:00Z", "valid_to": None}
        past = {"tariff_code": "past", "valid_from": "2019-01-01T00:00:00Z", "valid_to": "2020-01-01T00:00:00Z"}
        future = {"tariff_code": "future", "valid_from": "2099-01-01T00:00:00Z", "valid_to": None}
        for agreements in ([active, past, future], [future, past, active]):
            account = {
                "properties": [
                    {
                        "electricity_meter_points": [
                            {"mpan": "point", "agreements": agreements, "meters": [{"serial_number": "serial"}]}
                        ]
                    }
                ]
            }
            self.assertEqual(OctopusClient.meters(account)[0].tariff_code, "active")

    def test_historical_tariffs_overlap_window_and_clip_to_agreement_not_window(self):
        old = "E-1R-OLD-V1-26-01-01-A"
        current = "E-1R-NEW-V1-26-01-01-A"
        agreements = (
            TariffAgreement(old, "2026-09-01T00:00:00+01:00", "2026-10-01T00:00:00+01:00"),
            TariffAgreement(current, "2026-10-01T00:00:00+01:00", None),
            TariffAgreement("E-1R-EXPIRED-V1-25-01-01-A", "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"),
            TariffAgreement("E-1R-FUTURE-V1-99-01-01-A", "2099-01-01T00:00:00Z", None),
        )
        meter = Meter("electricity", "point", "serial", current, agreements[1].valid_from, None, agreements)
        client = OctopusClient("key")
        source_rate = {"valid_from": "2026-08-01T01:00:00+01:00", "valid_to": None, "value_inc_vat": 10}
        for kind in ("standard-unit-rates", "standing-charges"):
            with patch.object(client, "paginated", return_value=iter(())) as pages:
                pages.side_effect = lambda *args, **kwargs: iter([source_rate])
                rates = list(client.tariff_rates(meter, kind, "2026-09-15T00:00:00+01:00", "2026-11-05T00:00:00Z"))
                self.assertEqual(pages.call_count, 2)
                self.assertIn(old, pages.call_args_list[0].args[0])
                self.assertIn(current, pages.call_args_list[1].args[0])
                self.assertEqual(
                    pages.call_args_list[0].args[1],
                    {"period_from": "2026-09-14T23:00:00Z", "period_to": "2026-09-30T23:00:00Z"},
                )
                self.assertEqual(
                    pages.call_args_list[1].args[1],
                    {"period_from": "2026-09-30T23:00:00Z", "period_to": "2026-11-05T00:00:00Z"},
                )
                self.assertEqual(
                    [(r["valid_from"], r["valid_to"]) for r in rates],
                    [
                        ("2026-08-31T23:00:00Z", "2026-09-30T23:00:00Z"),
                        ("2026-09-30T23:00:00Z", None),
                    ],
                )
                self.assertEqual([r["value_inc_vat"] for r in rates], [10, 10])

    def test_tariffs_compare_offsets_as_instants_and_exclude_nonoverlapping_rates(self):
        meter = Meter("electricity", "point", "serial", "E-1R-NEW-V1-26-01-01-A", "2026-10-01T00:00:00+01:00", None)
        client = OctopusClient("key")
        rates = [
            {"valid_from": "2026-09-30T23:30:00Z", "valid_to": "2026-10-01T01:00:00+01:00", "value_inc_vat": 1},
            {"valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-09-30T23:00:00Z", "value_inc_vat": 2},
            {"valid_from": "2026-11-05T00:00:00Z", "valid_to": None, "value_inc_vat": 3},
        ]
        with patch.object(client, "paginated", return_value=iter(rates)):
            result = list(
                client.tariff_rates(meter, "standard-unit-rates", "2026-09-30T23:00:00Z", "2026-11-05T00:00:00Z")
            )
        self.assertEqual(
            result, [{"valid_from": "2026-09-30T23:30:00Z", "valid_to": "2026-10-01T00:00:00Z", "value_inc_vat": 1}]
        )

    def test_account_preserves_normalized_historical_agreements_without_inventing_active_tariff(self):
        account = {
            "properties": [
                {
                    "gas_meter_points": [
                        {
                            "mprn": "point",
                            "agreements": [
                                {
                                    "tariff_code": "old",
                                    "valid_from": "2020-08-01T00:00:00+01:00",
                                    "valid_to": "2020-09-01T00:00:00+01:00",
                                },
                                {"tariff_code": "future", "valid_from": "2099-01-01T00:00:00Z", "valid_to": None},
                            ],
                            "meters": [{"serial_number": "serial"}],
                        }
                    ]
                }
            ]
        }
        meter = OctopusClient.meters(account)[0]
        self.assertIsNone(meter.tariff_code)
        self.assertEqual(meter.agreements[0], TariffAgreement("old", "2020-07-31T23:00:00Z", "2020-08-31T23:00:00Z"))

    def test_repeated_sync_replaces_stale_unbounded_rates_with_agreement_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "energy.sqlite3"
            old, current = "E-1R-OLD-V1-26-01-01-A", "E-1R-NEW-V1-26-01-01-A"
            agreements = (
                TariffAgreement(old, "2026-09-01T00:00:00+01:00", "2026-10-01T00:00:00+01:00"),
                TariffAgreement(current, "2026-10-01T00:00:00+01:00", None),
            )
            meter = Meter("electricity", "point", "serial", current, agreements[1].valid_from, None, agreements)
            conn = connect(database)
            meter_id = upsert_meter(
                conn,
                fuel=meter.fuel,
                point_id=meter.point_id,
                serial=meter.serial_number,
                tariff_code=meter.tariff_code,
                valid_from=meter.valid_from,
                valid_to=None,
                synced_at="old-sync",
            )
            upsert_rate(
                conn,
                meter_id,
                "standard-unit-rates",
                {"valid_from": "2020-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 999},
            )
            conn.commit()
            conn.close()
            client = OctopusClient("key")
            reading = {
                "interval_start": "2026-11-01T00:00:00Z",
                "interval_end": "2026-11-01T00:30:00Z",
                "consumption": 1,
            }
            with (
                patch("octopus_energy.cli.OctopusClient", return_value=client),
                patch.object(client, "account", return_value={}),
                patch.object(client, "meters", return_value=[meter]),
                patch.object(client, "consumption", return_value=[reading]),
                patch.object(client, "paginated") as pages,
            ):
                pages.side_effect = lambda *args, **kwargs: iter(
                    [{"valid_from": "2020-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 10}]
                )
                for _ in range(2):
                    self.assertEqual(sync(database, "account", "key", 400)["status"], "success")
            conn = connect(database)
            rates = conn.execute(
                "SELECT kind,valid_from,valid_to,value_inc_vat FROM tariff_rates ORDER BY kind,valid_from"
            ).fetchall()
            self.assertEqual(len(rates), 4)
            for kind in ("standard-unit-rates", "standing-charges"):
                self.assertEqual(
                    [tuple(row)[1:] for row in rates if row["kind"] == kind],
                    [
                        ("2026-08-31T23:00:00Z", "2026-09-30T23:00:00Z", 10),
                        ("2026-09-30T23:00:00Z", None, 10),
                    ],
                )
            conn.close()

    def test_tariff_failure_restores_replaced_rates_and_readings(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "energy.sqlite3"
            conn = connect(database)
            meter_id = upsert_meter(
                conn,
                fuel="electricity",
                point_id="point",
                serial="serial",
                tariff_code=None,
                valid_from=None,
                valid_to=None,
                synced_at="old-sync",
            )
            reading = {
                "interval_start": "2026-11-01T00:00:00Z",
                "interval_end": "2026-11-01T00:30:00Z",
                "consumption": 1,
            }
            upsert_consumption(conn, meter_id, reading)
            rate = {"valid_from": "2020-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 10}
            upsert_rate(conn, meter_id, "standard-unit-rates", rate)
            conn.commit()
            conn.close()

            def partial_rates(meter, kind, **kwargs):
                if kind == "standing-charges":
                    raise OctopusAPIError("simulated tariff failure")
                yield {**rate, "value_inc_vat": 20}

            with patch("octopus_energy.cli.OctopusClient") as client:
                client.return_value.meters.return_value = [Meter("electricity", "point", "serial", None, None, None)]
                client.return_value.consumption.return_value = [{**reading, "consumption": 2}]
                client.return_value.tariff_rates.side_effect = partial_rates
                with self.assertRaises(OctopusAPIError):
                    sync(database, "account", "key", 400)
            conn = connect(database)
            self.assertEqual(conn.execute("SELECT consumption_kwh FROM consumption").fetchone()[0], 1)
            self.assertEqual(
                tuple(conn.execute("SELECT valid_from,valid_to,value_inc_vat FROM tariff_rates").fetchone()),
                ("2020-01-01T00:00:00Z", None, 10),
            )
            self.assertEqual(conn.execute("SELECT status FROM sync_history").fetchone()[0], "failed")
            conn.close()

    def test_empty_and_partial_meter_reads_preserve_previous_complete_snapshot(self):
        for partial in (False, True):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "energy.sqlite3"
                conn = connect(database)
                meter_id = upsert_meter(
                    conn,
                    fuel="electricity",
                    point_id="point",
                    serial="serial",
                    tariff_code=None,
                    valid_from=None,
                    valid_to=None,
                    synced_at="old-sync",
                )
                reading = {
                    "interval_start": "2026-11-01T00:00:00Z",
                    "interval_end": "2026-11-01T00:30:00Z",
                    "consumption": 1,
                }
                upsert_consumption(conn, meter_id, reading)
                rate = {"valid_from": "2020-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 10}
                upsert_rate(conn, meter_id, "standard-unit-rates", rate)
                conn.execute(
                    "INSERT INTO daily_costs(date,fuel,consumption_kwh,unit_cost_gbp,standing_charge_gbp,total_cost_gbp) VALUES('2026-11-01','electricity',1,0.1,0.5,0.6)"
                )
                conn.commit()
                conn.close()
                meters = [
                    Meter("electricity", "point", "serial", None, None, None),
                    Meter("gas", "gas-point", "gas-serial", None, None, None),
                ]
                with patch("octopus_energy.cli.OctopusClient") as client:
                    client.return_value.meters.return_value = meters
                    client.return_value.consumption.side_effect = [
                        [{**reading, "consumption": 2}] if partial else [],
                        [],
                    ]
                    client.return_value.tariff_rates.return_value = []
                    result = sync(database, "account", "key", 400)
                expected = "partial" if partial else "no_data"
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["has_readings"], partial)
                self.assertEqual(result["empty_meters"], 1 if partial else 2)
                conn = connect(database)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT synced_at FROM meters").fetchone()[0], "old-sync")
                self.assertEqual(conn.execute("SELECT consumption_kwh FROM consumption").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT value_inc_vat FROM tariff_rates").fetchone()[0], 10)
                self.assertEqual(conn.execute("SELECT total_cost_gbp FROM daily_costs").fetchone()[0], 0.6)
                self.assertEqual(conn.execute("SELECT status FROM sync_history").fetchone()[0], expected)
                conn.close()

    def test_incomplete_cli_sync_does_not_regenerate_outputs_or_claim_success(self):
        for status in ("no_data", "partial"):
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"ACCOUNT_NUMBER": "account", "API_KEY": "key"}, clear=True),
                patch("sys.argv", ["octopus"]),
                patch("octopus_energy.cli.load_env", return_value={}),
                patch("octopus_energy.cli.sync", return_value={"status": status}),
                patch("octopus_energy.cli.write_reports") as reports,
                patch("octopus_energy.cli.write_dashboard") as dashboard,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(), 1)
            reports.assert_not_called()
            dashboard.assert_not_called()
            self.assertIn("incomplete", output.getvalue())
            self.assertNotIn("succeeded", output.getvalue())

    def test_collection_pagination_uses_bounded_larger_pages(self):
        self.assertEqual(PAGE_SIZE, 25000)
        client = OctopusClient("key")
        with patch.object(
            client,
            "get",
            side_effect=[
                {"results": [{"n": 1}], "next": "https://api.octopus.energy/v1/next"},
                {"results": [{"n": 2}], "next": None},
            ],
        ) as get:
            self.assertEqual(
                list(client.paginated("readings", {"period_from": "2026-11-01T00:00:00Z"})), [{"n": 1}, {"n": 2}]
            )
            self.assertEqual(
                get.call_args_list[0].args[1], {"page_size": str(PAGE_SIZE), "period_from": "2026-11-01T00:00:00Z"}
            )
            self.assertIsNone(get.call_args_list[1].args[1])


if __name__ == "__main__":
    unittest.main()
