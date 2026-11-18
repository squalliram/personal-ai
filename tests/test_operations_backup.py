import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class OperationsBackupTests(unittest.TestCase):
    def setUp(self):
        doc = (Path(__file__).resolve().parents[1] / "docs/command-centre/operations.md").read_text()
        self.snippet = doc.split("## Back up your data", 1)[1].split("```sh\n", 1)[1].split("```", 1)[0]

    def destination(self, home, checkout):
        checksum = subprocess.check_output(["cksum"], input=str(checkout).encode()).decode().split()[0]
        return home / "personal-ai-backups" / f"{checkout.name}-{checksum}"

    def run_backup(self, home, checkout):
        return subprocess.run(
            ["sh", "-c", self.snippet],
            cwd=checkout,
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            text=True,
        )

    def test_documented_backup_is_private_and_separate_per_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name, value in (("main/same checkout", 1), ("feature/same checkout", 2)):
                checkout = home / name
                (checkout / "data").mkdir(parents=True)
                with sqlite3.connect(checkout / "data/command_centre.sqlite3") as conn:
                    conn.execute("CREATE TABLE sample (value INTEGER)")
                    conn.execute("INSERT INTO sample VALUES (?)", (value,))
                result = subprocess.run(
                    ["sh", "-c", "umask 022\n" + self.snippet + '\nprintf "mask=%s" "$(umask)"'],
                    cwd=checkout,
                    env={**os.environ, "HOME": str(home)},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout, "mask=0022")
            for name, value in (("main/same checkout", 1), ("feature/same checkout", 2)):
                destination = self.destination(home, home / name)
                backup = destination / "command_centre.sqlite3"
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                with sqlite3.connect(backup) as conn:
                    self.assertEqual(conn.execute("SELECT value FROM sample").fetchone(), (value,))
            empty = home / "empty"
            empty.mkdir()
            self.assertEqual(self.run_backup(home, empty).returncode, 0)
            self.assertEqual(list(self.destination(home, empty).iterdir()), [])

    def test_existing_directory_and_backups_are_made_private(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            checkout = home / "checkout"
            (checkout / "data").mkdir(parents=True)
            with sqlite3.connect(checkout / "data/command_centre.sqlite3") as conn:
                conn.execute("CREATE TABLE sample (value INTEGER)")
            destination = self.destination(home, checkout)
            destination.mkdir(parents=True)
            destination.chmod(0o755)
            for name in ("command_centre", "daily_briefing"):
                backup = destination / f"{name}.sqlite3"
                backup.touch()
                backup.chmod(0o644)
            self.assertEqual(self.run_backup(home, checkout).returncode, 0)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            for backup in destination.iterdir():
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_failed_backup_stops_before_later_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            checkout = home / "checkout"
            (checkout / "data").mkdir(parents=True)
            (checkout / "data/command_centre.sqlite3").write_bytes(b"not a sqlite database")
            with sqlite3.connect(checkout / "data/daily_briefing.sqlite3") as conn:
                conn.execute("CREATE TABLE sample (value INTEGER)")
            result = self.run_backup(home, checkout)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a database", result.stderr)
            self.assertFalse((self.destination(home, checkout) / "daily_briefing.sqlite3").exists())
