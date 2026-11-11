import contextlib
import io
import json
import runpy
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

PROBE = Path(__file__).resolve().parents[1] / "deploy/command-centre/probe_host.py"


class HostingProbeTests(unittest.TestCase):
    def probe(self, response, uapi=True):
        output = io.StringIO()
        with (
            patch("sys.argv", [str(PROBE), "/nonexistent/personal-ai"]),
            patch("glob.glob", return_value=[]),
            patch("shutil.which", side_effect=lambda name: "/bin/uapi" if name == "uapi" and uapi else None),
            patch("pathlib.Path.is_file", return_value=False),
            patch("subprocess.run", return_value=CompletedProcess([], 0, json.dumps(response), "")) as run,
            contextlib.redirect_stdout(output),
        ):
            runpy.run_path(str(PROBE), run_name="__main__")
        return json.loads(output.getvalue()), run

    def test_reports_registration_without_disclosing_registry_values(self):
        result, run = self.probe(
            {"result": {"status": 1, "data": {"app": {"base_uri": "/command-centre", "secret": "do-not-emit"}}}}
        )
        self.assertTrue(result["command_centre_registered"])
        self.assertNotIn("do-not-emit", json.dumps(result))
        self.assertEqual(run.call_args.args[0][-2:], ["PassengerApps", "list_applications"])

    def test_unregistered_app_is_reported(self):
        result, _ = self.probe({"result": {"status": 1, "data": {}}})
        self.assertFalse(result["command_centre_registered"])

    def test_missing_api_means_unknown_not_unregistered(self):
        result, run = self.probe({}, uapi=False)
        self.assertIsNone(result["command_centre_registered"])
        run.assert_not_called()
