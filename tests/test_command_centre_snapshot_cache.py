import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from command_centre_fixtures import Client, Clock, make_app, make_settings

from command_centre import app as app_module
from command_centre import sources

SHARED_ROUTES = ("/api/v1/overview", "/api/v1/integrations", "/api/v1/automations", "/api/v1/modules")


class SnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.clock = Clock()
        self.settings = make_settings(self.root)
        self.client = Client(make_app(self.settings, clock=self.clock))
        self.client.login()
        patcher = mock.patch.object(app_module.sources, "energy_snapshot", wraps=sources.energy_snapshot)
        self.snapshot = patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, path="/api/v1/overview"):
        response = self.client.request("GET", path)
        self.assertEqual(response.status, 200)
        return response

    def test_section_routes_share_one_snapshot(self):
        for path in SHARED_ROUTES:
            self.get(path)
        self.assertEqual(self.snapshot.call_count, 1)

    def test_snapshot_expires_so_time_based_health_stays_current(self):
        self.get()
        self.clock.now += app_module.COLLECT_TTL_SECONDS - 1
        self.get()
        self.assertEqual(self.snapshot.call_count, 1)
        self.clock.now += 1
        self.get()
        self.assertEqual(self.snapshot.call_count, 2)

    def test_changed_source_file_is_read_at_once(self):
        self.get()
        self.settings.energy_status.write_text(json.dumps({"last_successful_sync_at": None}))
        self.get()
        self.assertEqual(self.snapshot.call_count, 2)
        self.settings.briefing_reports.mkdir()
        self.get()
        self.assertEqual(self.snapshot.call_count, 3)

    def test_routes_still_require_a_session(self):
        self.get()
        anonymous = Client(self.client.app)
        for path in SHARED_ROUTES:
            self.assertEqual(anonymous.request("GET", path).status, 401)

    def test_responses_report_server_time(self):
        self.assertRegex(self.get().header("Server-Timing"), r"^app;dur=\d+\.\d$")


if __name__ == "__main__":
    unittest.main()
