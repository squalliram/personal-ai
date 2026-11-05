import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from octopus_energy.mcp_server import PROTOCOL_VERSIONS, TOOLS
from octopus_energy.reports import write_reports
from octopus_energy.storage import connect

ROOT = Path(__file__).resolve().parents[1]


def request(method, request_id, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def initialization(version="2025-11-25"):
    return [
        request(
            "initialize",
            1,
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "energy-test-client", "version": "1"},
            },
        ),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


class EnergyMCPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "energy.sqlite3"

    def run_server(self, messages):
        body = "\n".join(message if isinstance(message, str) else json.dumps(message) for message in messages) + "\n"
        process = subprocess.run(
            [sys.executable, "-m", "octopus_energy.mcp_server", str(self.database)],
            input=body,
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=10,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        return [json.loads(line) for line in process.stdout.splitlines()]

    def seed_database(self):
        conn = connect(self.database)
        conn.execute(
            "INSERT INTO daily_costs(date,fuel,consumption_kwh,unit_cost_gbp,standing_charge_gbp,total_cost_gbp) "
            "VALUES(?,?,?,?,?,?)",
            ("2026-11-01", "electricity", 2.0, 0.2, 0.5, 0.7),
        )
        conn.commit()
        conn.close()

    def test_standard_handshake_and_discovery_do_not_create_database(self):
        responses = self.run_server(initialization() + [request("tools/list", "list")])
        self.assertEqual([response["id"] for response in responses], [1, "list"])
        result = responses[0]["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertTrue(result["serverInfo"]["name"])
        self.assertTrue(result["serverInfo"]["version"])
        tools = responses[1]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], list(TOOLS))
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])
        daily = next(tool for tool in tools if tool["name"] == "get_daily_consumption")
        self.assertEqual(daily["inputSchema"]["required"], ["start", "end"])
        self.assertFalse(self.database.exists())

    def test_all_supported_protocol_versions_and_unknown_version_fallback(self):
        for version in (*PROTOCOL_VERSIONS, "2099-01-01"):
            with self.subTest(version=version):
                responses = self.run_server(initialization(version))
                expected = version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
                self.assertEqual(responses[0]["result"]["protocolVersion"], expected)

    def test_every_discovered_tool_can_be_called_read_only(self):
        self.seed_database()
        before = hashlib.sha256(self.database.read_bytes()).digest()
        calls = []
        for index, name in enumerate(TOOLS):
            arguments = {"start": "2026-11-01", "end": "2026-11-02"} if name == "get_daily_consumption" else {}
            calls.append(request("tools/call", index + 10, {"name": name, "arguments": arguments}))
        responses = self.run_server(initialization() + calls)
        self.assertEqual(len(responses), len(TOOLS) + 1)
        for index, response in enumerate(responses[1:]):
            self.assertEqual(response["id"], index + 10)
            self.assertNotIn("error", response)
            self.assertNotIn("isError", response["result"])
            self.assertIsInstance(json.loads(response["result"]["content"][0]["text"]), list)
        summary = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(summary[0]["consumption_kwh"], 2.0)
        self.assertEqual(summary[0]["total_cost_gbp"], 0.7)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), before)

    def test_notifications_are_silent_and_never_call_tools(self):
        messages = initialization() + [
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 50}},
            {"jsonrpc": "2.0", "method": "unknown-notification", "params": {"private": "do-not-echo"}},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_energy_summary"}},
            request("ping", 0),
        ]
        responses = self.run_server(messages)
        self.assertEqual([response["id"] for response in responses], [1, 0])
        self.assertEqual(responses[-1]["result"], {})
        self.assertFalse(self.database.exists())

    def test_tools_require_completed_initialization(self):
        responses = self.run_server(
            [
                request("tools/list", "before"),
                initialization()[0],
                request("tools/list", "before-notification"),
                initialization()[1],
                request("tools/list", "after"),
            ]
        )
        self.assertEqual(responses[0]["error"]["code"], -32002)
        self.assertEqual(responses[2]["error"]["code"], -32002)
        self.assertIn("tools", responses[3]["result"])

    def test_invalid_requests_and_parse_errors_recover_without_echoing_input(self):
        marker = "synthetic-sensitive-payload"
        responses = self.run_server(
            [
                '{"jsonrpc": "2.0", "private": "' + marker + '",',
                "null",
                "[]",
                "true",
                request("ping", {"invalid": marker}),
                {"jsonrpc": "1.0", "id": 90, "method": "ping"},
                '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
                request("ping", "recovered"),
            ]
        )
        self.assertEqual(
            [response["error"]["code"] for response in responses[:-1]],
            [-32700, -32600, -32600, -32600, -32600, -32600, -32700],
        )
        self.assertTrue(all(response["id"] is None for response in responses[:-1]))
        self.assertEqual(responses[-1], {"jsonrpc": "2.0", "id": "recovered", "result": {}})
        self.assertNotIn(marker, json.dumps(responses))

    def test_error_responses_preserve_request_ids_without_echoing_parameters(self):
        marker = "synthetic-sensitive-payload"
        responses = self.run_server(
            initialization()
            + [
                request("unknown-" + marker, "method-error", {}),
                request("tools/call", 21, {"name": marker}),
                request("tools/call", 22, [marker]),
                request("tools/list", 23, {"cursor": marker}),
            ]
        )
        self.assertEqual([response["id"] for response in responses[1:]], ["method-error", 21, 22, 23])
        self.assertEqual([response["error"]["code"] for response in responses[1:]], [-32601, -32602, -32602, -32602])
        self.assertNotIn(marker, json.dumps(responses))

    def test_argument_validation_matches_advertised_schema(self):
        invalid = [
            ("get_daily_consumption", {}),
            ("get_daily_consumption", {"start": "2026-11-01", "end": "2026-11-01"}),
            ("get_energy_summary", {"start": "2026-02-30", "end": "2026-03-01"}),
            ("get_energy_summary", {"start": 1, "end": 2}),
            ("get_expensive_days", {"limit": True}),
            ("get_expensive_days", {"limit": -1}),
            ("get_expensive_days", {"limit": 1001}),
            ("get_expensive_days", {"fuel": "private-invalid-fuel"}),
            ("get_tariff_details", {"unexpected": "private-invalid-argument"}),
            ("get_energy_summary", []),
        ]
        calls = [
            request("tools/call", index + 10, {"name": name, "arguments": arguments})
            for index, (name, arguments) in enumerate(invalid)
        ]
        responses = self.run_server(initialization() + calls)
        for response in responses[1:]:
            self.assertEqual(response["error"]["code"], -32602)
        self.assertFalse(self.database.exists())

    def test_missing_database_is_safe_tool_error_and_does_not_create_file(self):
        responses = self.run_server(
            initialization()
            + [
                request("tools/call", "missing", {"name": "get_energy_summary"}),
                request("ping", "still-running"),
            ]
        )
        error = responses[1]
        self.assertEqual(error["id"], "missing")
        self.assertTrue(error["result"]["isError"])
        self.assertNotIn(str(self.database), json.dumps(responses))
        self.assertFalse(self.database.exists())
        self.assertEqual(responses[2]["result"], {})

    def test_corrupt_database_is_safe_tool_error_without_disclosing_contents(self):
        marker = "synthetic-private-database-content"
        self.database.write_text(marker, encoding="utf-8")
        responses = self.run_server(
            initialization()
            + [
                request("tools/call", "corrupt", {"name": "get_energy_summary"}),
            ]
        )
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertNotIn(marker, json.dumps(responses))
        self.assertNotIn(str(self.database), json.dumps(responses))
        self.assertEqual(self.database.read_text(encoding="utf-8"), marker)

    def test_invalid_initialization_can_be_retried_and_preserves_id(self):
        responses = self.run_server(
            [
                request(
                    "initialize",
                    "invalid-init",
                    {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": []},
                ),
            ]
            + initialization()
            + [request("tools/list", "ready")]
        )
        self.assertEqual(responses[0]["id"], "invalid-init")
        self.assertEqual(responses[0]["error"]["code"], -32602)
        self.assertIn("serverInfo", responses[1]["result"])
        self.assertIn("tools", responses[2]["result"])

    def test_summary_supports_existing_independent_date_bounds(self):
        self.seed_database()
        responses = self.run_server(
            initialization()
            + [
                request("tools/call", "start", {"name": "get_energy_summary", "arguments": {"start": "2026-11-02"}}),
                request("tools/call", "end", {"name": "get_energy_summary", "arguments": {"end": "2026-11-01"}}),
            ]
        )
        for response in responses[1:]:
            self.assertEqual(json.loads(response["result"]["content"][0]["text"]), [])

    def test_monthly_totals_remain_unknown_when_any_day_is_unpriced(self):
        self.seed_database()
        conn = connect(self.database)
        conn.execute(
            "INSERT INTO daily_costs(date,fuel,consumption_kwh,unit_cost_gbp,standing_charge_gbp,total_cost_gbp) "
            "VALUES(?,?,?,?,?,?)",
            ("2026-11-02", "electricity", 3.0, None, 0.5, None),
        )
        conn.commit()
        conn.close()
        responses = self.run_server(
            initialization()
            + [
                request("tools/call", "monthly", {"name": "get_monthly_bill_comparison"}),
            ]
        )
        rows = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(rows[0]["consumption_kwh"], 5.0)
        self.assertIsNone(rows[0]["total_cost_gbp"])

    def test_internal_errors_preserve_id_and_hide_database_values(self):
        self.seed_database()
        conn = connect(self.database)
        conn.execute("UPDATE daily_costs SET consumption_kwh=?", (float("inf"),))
        conn.commit()
        conn.close()
        responses = self.run_server(
            initialization()
            + [
                request("tools/call", "internal", {"name": "get_energy_summary"}),
                request("ping", "recovered"),
            ]
        )
        self.assertEqual(
            responses[1], {"jsonrpc": "2.0", "id": "internal", "error": {"code": -32603, "message": "Internal error"}}
        )
        self.assertEqual(responses[2]["result"], {})
        self.assertNotIn(str(self.database), json.dumps(responses))

    def test_legacy_derived_values_require_refresh_without_mcp_writes(self):
        conn = connect(self.database)
        conn.execute(
            "INSERT INTO meters VALUES(1,'electricity','synthetic-point','synthetic-serial',NULL,NULL,NULL,'synthetic')"
        )
        conn.execute("INSERT INTO consumption VALUES(1,'2026-11-01T00:00:00Z','2026-11-01T00:30:00Z',2)")
        conn.execute("DROP TABLE daily_costs")
        conn.execute(
            "CREATE TABLE daily_costs (date TEXT NOT NULL,fuel TEXT NOT NULL,"
            "consumption_kwh REAL NOT NULL,unit_cost_gbp REAL NOT NULL,"
            "standing_charge_gbp REAL NOT NULL,total_cost_gbp REAL NOT NULL,PRIMARY KEY(date,fuel))"
        )
        conn.execute("INSERT INTO daily_costs VALUES('2026-11-01','electricity',2,0,0,0)")
        conn.commit()
        raw_before = conn.execute("SELECT * FROM consumption").fetchall()
        conn.close()
        before = hashlib.sha256(self.database.read_bytes()).digest()
        responses = self.run_server(
            initialization()
            + [
                request("tools/call", "legacy", {"name": "get_energy_summary"}),
            ]
        )
        self.assertEqual(responses[1]["id"], "legacy")
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertIn("requires a local refresh", responses[1]["result"]["content"][0]["text"])
        self.assertNotIn(str(self.database), json.dumps(responses))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), before)
        write_reports(self.database, Path(self.directory.name) / "reports")
        with connect(self.database) as migrated:
            self.assertEqual(migrated.execute("SELECT * FROM consumption").fetchall(), raw_before)
        refreshed = self.run_server(
            initialization()
            + [
                request("tools/call", "refreshed", {"name": "get_energy_summary"}),
            ]
        )
        rows = json.loads(refreshed[1]["result"]["content"][0]["text"])
        self.assertEqual(rows[0]["consumption_kwh"], 2)
        self.assertIsNone(rows[0]["total_cost_gbp"])
