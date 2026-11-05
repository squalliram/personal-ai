import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERROR = "Python 3.10 or newer is required (set PYTHON to its path)."


class EnergyPythonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="energy-python-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.scripts = self.root / "application with spaces" / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("find-python.sh", "start-energy.sh", "publish-energy-dashboard.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.scripts / name)
        self.capture = self.root / "capture.json"
        self.env = {
            **os.environ,
            "PATH": str(self.bin),
            "CAPTURE": str(self.capture),
            "DASHBOARD_TARGET": str(self.root / "never-published"),
        }
        for key in ("PYTHON", "PYTHONPATH", "PYTHONHOME"):
            self.env.pop(key, None)
        self.executable(self.bin / "dirname", '#!/bin/sh\nexec /usr/bin/dirname "$@"\n')

    @staticmethod
    def executable(path, body):
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def fake_python(self, name, version, noisy=False):
        probe = (
            "import sys; "
            + ("print('private-probe-output'); print('private-probe-error', file=sys.stderr); " if noisy else "")
            + f"sys.version_info={version!r}; exec(sys.argv[1])"
        )
        capture = (
            "import json,os,sys; "
            "payload={'interpreter':sys.argv[1],'argv':sys.argv[2:],'cwd':os.getcwd()}; "
            "open(os.environ['CAPTURE'],'w').write(json.dumps(payload))"
        )
        body = (
            '#!/bin/sh\nif [ "$1" = -c ]; then\n'
            f'  exec {shlex.quote(sys.executable)} -c {shlex.quote(probe)} "$2"\n'
            "fi\n"
            f'exec {shlex.quote(sys.executable)} -c {shlex.quote(capture)} {shlex.quote(name)} "$@"\n'
        )
        return self.executable(self.bin / name, body)

    def launch(self, *, python=None, script="start-energy.sh", cwd=ROOT, args=("--help",)):
        env = dict(self.env)
        if python is not None:
            env["PYTHON"] = str(python)
        return subprocess.run(
            [str(self.scripts / script), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def assert_rejected(self, result):
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), ERROR)
        self.assertFalse(self.capture.exists(), "Incompatible Python must never execute the application")
        self.assertFalse((self.root / "never-published").exists())

    def probe_without_cloudlinux_installations(self, *, python=None):
        # Isolate candidate discovery from any real /opt/alt installation on a test host.
        loader = r"""
command() {
  case "$2" in /opt/alt/*) return 1;; esac
  /bin/sh -c 'command -v "$1"' sh "$2"
}
. "$1"
"$PYTHON" -m octopus_energy.cli --help
"""
        env = dict(self.env)
        if python is not None:
            env["PYTHON"] = str(python)
        return subprocess.run(
            ["/bin/sh", "-eu", "-c", loader, "sh", str(self.scripts / "find-python.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_explicit_python_39_is_rejected_before_cli_without_private_output(self):
        python = self.fake_python("private incompatible path", (3, 9, 20), noisy=True)
        self.fake_python("python3", (3, 12, 10))
        self.assert_rejected(self.launch(python=python))

    def test_explicit_python_39_is_rejected_before_publisher(self):
        python = self.fake_python("python39", (3, 9, 20))
        self.assert_rejected(self.launch(python=python, script="publish-energy-dashboard.sh"))

    def test_invalid_explicit_override_is_not_replaced_by_compatible_candidate(self):
        self.fake_python("python3", (3, 12, 10))
        self.assert_rejected(self.launch(python=self.bin / "private missing interpreter"))

    def test_compatible_explicit_python_preserves_paths_arguments_and_cwd(self):
        python = self.fake_python("compatible python with spaces", (3, 10, 16))
        args = ("--database", str(self.root / "database with spaces.sqlite3"), "--days", "7")
        result = self.launch(python=python, cwd=self.root, args=args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        captured = json.loads(self.capture.read_text())
        self.assertEqual(captured["argv"], ["-m", "octopus_energy.cli", *args])
        self.assertEqual(captured["cwd"], str(self.root))

    def test_automatic_discovery_skips_39_and_accepts_310(self):
        self.fake_python("python3", (3, 9, 20), noisy=True)
        self.fake_python("python3.10", (3, 10, 16))
        result = self.probe_without_cloudlinux_installations()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(self.capture.read_text())["interpreter"], "python3.10")

    def test_only_automatic_python_39_fails_closed(self):
        self.fake_python("python3", (3, 9, 20))
        self.assert_rejected(self.probe_without_cloudlinux_installations())

    def test_no_automatic_candidates_fails_closed(self):
        self.assert_rejected(self.probe_without_cloudlinux_installations())

    def test_actual_supported_python_runs_cli_help_from_repository_cwd(self):
        self.assertGreaterEqual(sys.version_info, (3, 10))
        result = self.launch(python=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Read-only Octopus Energy collection and analysis", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.capture.exists())

    def test_helper_stays_sourced_and_launchers_stay_executable(self):
        self.assertEqual(stat.S_IMODE((ROOT / "scripts/find-python.sh").stat().st_mode), 0o644)
        for name in ("start-energy.sh", "publish-energy-dashboard.sh"):
            self.assertTrue((ROOT / "scripts" / name).stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
