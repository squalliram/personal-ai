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
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertNotIn("text", run.call_args.kwargs)

    def test_unregistered_app_is_reported(self):
        result, _ = self.probe({"result": {"status": 1, "data": {}}})
        self.assertFalse(result["command_centre_registered"])

    def test_missing_api_means_unknown_not_unregistered(self):
        result, run = self.probe({}, uapi=False)
        self.assertIsNone(result["command_centre_registered"])
        run.assert_not_called()

    def test_selector_registry_is_domain_scoped_and_sanitized(self):
        response = {
            "result": "success",
            "selector_enabled": True,
            "available_versions": {
                "3.12": {
                    "status": "enabled",
                    "users": {
                        "owner": {
                            "applications": {
                                "command-centre-app": {
                                    "domain": "iramkhan.com",
                                    "app_uri": "command-centre",
                                    "env_vars": {"SECRET": "never-emit"},
                                }
                            }
                        }
                    },
                }
            },
        }
        output = io.StringIO()
        with (
            patch("sys.argv", [str(PROBE), "/nonexistent/personal-ai"]),
            patch("glob.glob", return_value=[]),
            patch("shutil.which", side_effect=lambda name: "/bin/selector" if name == "cloudlinux-selector" else None),
            patch("pathlib.Path.is_file", return_value=False),
            patch("subprocess.run", return_value=CompletedProcess([], 0, json.dumps(response), "")) as run,
            contextlib.redirect_stdout(output),
        ):
            runpy.run_path(str(PROBE), run_name="__main__")
        report = json.loads(output.getvalue())
        self.assertTrue(report["selector_python312_enabled"])
        self.assertTrue(report["selector_command_centre_registered"])
        self.assertNotIn("never-emit", output.getvalue())
        self.assertEqual(run.call_args.args[0][-2:], ["--domain", "iramkhan.com"])
