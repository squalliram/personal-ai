import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from octopus_energy import refresh_install

ROOT = Path(__file__).resolve().parents[1]


class RefreshInstallationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.app = self.root / "application with spaces and 'quotes'"
        (self.app / "scripts").mkdir(parents=True)
        self.runner = self.app / "scripts/refresh-energy.sh"
        self.runner.write_text('#!/bin/sh\n: > "$CRON_EXECUTED"\n')
        self.runner.chmod(0o755)
        self.target = self.root / "protected"
        self.target.mkdir()
        self.private = self.app / "data/energy-refresh"
        self.cron = self.root / "fake-crontab"
        self.cron.write_bytes(b"")
        self.writes = self.root / "cron-writes"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        crontab = self.bin / "crontab"
        crontab.write_text(
            f"#!{sys.executable}\n"
            "import os,pwd,sys\nfrom pathlib import Path\n"
            "cron=Path(os.environ['CRON_STORAGE'])\n"
            "if sys.argv[1]=='-l':\n"
            "    mode=os.environ.get('CRON_LIST_MODE','normal')\n"
            "    if mode=='absent':\n"
            "        print('no crontab for '+pwd.getpwuid(os.getuid()).pw_name,file=sys.stderr);sys.exit(1)\n"
            "    if mode=='denied':\n"
            "        print('synthetic private permission failure',file=sys.stderr);sys.exit(1)\n"
            "    sys.stdout.buffer.write(cron.read_bytes())\n"
            "elif sys.argv[1]=='-':\n"
            "    if os.environ.get('CRON_WRITE_FAILURE'): sys.exit(1)\n"
            "    cron.write_bytes(sys.stdin.buffer.read())\n"
            "    with Path(os.environ['CRON_WRITES']).open('ab') as stream: stream.write(b'write\\n')\n"
            "else: sys.exit(99)\n"
        )
        crontab.chmod(0o755)
        self.env = {
            **os.environ,
            "PYTHON": sys.executable,
            "PYTHONPATH": str(ROOT),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "DASHBOARD_TARGET": str(self.target),
            "CRON_STORAGE": str(self.cron),
            "CRON_WRITES": str(self.writes),
            "CRON_EXECUTED": str(self.root / "cron-executed"),
        }

    def install(self, **env):
        return subprocess.run(
            [str(ROOT / "scripts/install-energy-refresh.sh")],
            cwd=self.app,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_installs_private_exact_config_and_one_idempotent_marked_cron(self):
        unrelated = b"# preserve this comment\r\nMAILTO=synthetic@example.invalid\n\n17 2 * * * /synthetic/backup  \n"
        old = b"* * * * * cd /old && ./scripts/refresh-energy.sh # personal-ai-energy-refresh\n"
        self.cron.write_bytes(unrelated + old + old)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = (
            f"* * * * * cd {shlex.quote(str(self.app))} && ./scripts/refresh-energy.sh >/dev/null 2>&1 "
            "# personal-ai-energy-refresh\n"
        ).encode()
        self.assertEqual(self.cron.read_bytes(), unrelated + expected)
        config = self.private / "config.json"
        self.assertEqual(
            json.loads(config.read_bytes()),
            {
                "DASHBOARD_TARGET": str(self.target),
                "ENERGY_REFRESH_INTERVAL_MINUTES": 60,
            },
        )
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.cron.read_bytes(), unrelated + expected)
        self.assertEqual(self.writes.read_bytes(), b"write\n")
        # Execute only the new line's command in a shell, never an actual crontab.
        command = expected.decode().split(" ", 5)[5]
        invoked = subprocess.run(["sh", "-c", command], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(invoked.returncode, 0)
        self.assertTrue(Path(self.env["CRON_EXECUTED"]).exists())

    def test_preserves_unmarked_jobs_comments_and_an_unterminated_last_line(self):
        unrelated = (
            b"# documented # personal-ai-energy-refresh\n"
            b"* * * * * /synthetic/refresh-without-marker\n"
            b"9 3 * * * /synthetic/another-job"
        )
        self.cron.write_bytes(unrelated)
        self.assertEqual(self.install().returncode, 0)
        self.assertTrue(self.cron.read_bytes().startswith(unrelated + b"\n"))
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.cron.read_bytes().count(b"&& ./scripts/refresh-energy.sh"), 1)

    def test_absent_crontab_is_valid_but_unavailable_crontab_does_not_create_config(self):
        result = self.install(CRON_LIST_MODE="denied")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("synthetic private permission failure", result.stderr)
        self.assertFalse(self.private.exists())
        self.assertFalse(self.writes.exists())
        result = self.install(CRON_LIST_MODE="absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.private / "config.json").is_file())

    def test_missing_crontab_command_fails_before_any_directory_or_config_write(self):
        with patch("octopus_energy.refresh_install.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                refresh_install.install_refresh(self.app, self.target)
        self.assertFalse(self.private.exists())

    def test_failed_cron_write_restores_previous_config_and_preserves_other_flags(self):
        self.private.mkdir(parents=True, mode=0o700)
        config = self.private / "config.json"
        config.write_bytes(b'{"synthetic":"previous config"}\n')
        config.chmod(0o600)
        request = self.private / "request.flag"
        request.write_bytes(b"a" * 32)
        request.chmod(0o600)
        previous_cron = b"5 4 * * * /synthetic/unrelated\n"
        self.cron.write_bytes(previous_cron)
        result = self.install(CRON_WRITE_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(config.read_bytes(), b'{"synthetic":"previous config"}\n')
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(request.read_bytes(), b"a" * 32)
        self.assertEqual(stat.S_IMODE(request.stat().st_mode), 0o600)
        self.assertEqual(self.cron.read_bytes(), previous_cron)
        self.assertFalse(self.writes.exists())

    def test_redeploy_preserves_id_only_flags_and_atomic_php_temp_files(self):
        self.private.mkdir(parents=True, mode=0o700)
        names = ("request.flag", "request.running", ".request-" + "c" * 32 + ".tmp")
        for index, name in enumerate(names):
            path = self.private / name
            path.write_bytes(bytes([ord("a") + index]) * 32)
            path.chmod(0o600)

        def snapshot():
            values = {}
            for name in names:
                path = self.private / name
                info = path.stat()
                values[name] = (path.read_bytes(), info.st_mode, info.st_mtime_ns, info.st_ino)
            return values

        before = snapshot()
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(snapshot(), before)
        self.assertEqual(
            set(json.loads((self.private / "config.json").read_bytes())),
            {
                "DASHBOARD_TARGET",
                "ENERGY_REFRESH_INTERVAL_MINUTES",
            },
        )

    def test_failed_first_cron_write_removes_only_new_config(self):
        self.assertNotEqual(self.install(CRON_WRITE_FAILURE="1").returncode, 0)
        self.assertFalse((self.private / "config.json").exists())
        self.assertEqual(self.cron.read_bytes(), b"")

    def test_invalid_interval_paths_or_existing_public_state_fail_closed(self):
        for interval in ("0", "-1", "not-an-integer"):
            with self.subTest(interval=interval):
                self.assertNotEqual(self.install(ENERGY_REFRESH_INTERVAL_MINUTES=interval).returncode, 0)
        self.assertFalse(self.private.exists())
        self.private.mkdir(parents=True, mode=0o755)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse((self.private / "config.json").exists())
        self.private.chmod(0o700)
        for app, target in ((Path("relative"), self.target), (self.app, self.app), (self.app, self.private)):
            with self.subTest(app=app, target=target), self.assertRaises(ValueError):
                refresh_install.install_refresh(app, target)
        self.assertFalse(self.writes.exists())

    def test_configurable_positive_interval_and_ignored_private_state(self):
        result = self.install(ENERGY_REFRESH_INTERVAL_MINUTES="120")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads((self.private / "config.json").read_bytes())["ENERGY_REFRESH_INTERVAL_MINUTES"], 120
        )
        for name in (
            "config.json",
            "request.flag",
            "request.running",
            "refresh.log",
            "publication.lock",
            ".request-" + "a" * 32 + ".tmp",
            "nested/request.claim",
        ):
            result = subprocess.run(
                ["git", "-C", str(ROOT), "check-ignore", "--quiet", "--", "data/energy-refresh/" + name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, name)

    def test_ci_includes_php_syntax_check(self):
        workflow = (ROOT / ".github/workflows/test.yml").read_text()
        self.assertIn("php -l octopus_energy/refresh.php", workflow)
        self.assertIn("command -v php", workflow)
        self.assertIn("php --version", workflow)
        self.assertIn('ENERGY_REQUIRE_PHP: "1"', workflow)
