import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class OperationsBackupTests(unittest.TestCase):
    def test_documented_backup_is_private_and_separate_per_checkout(self):
        doc = (Path(__file__).resolve().parents[1] / "docs/command-centre/operations.md").read_text()
        snippet = doc.split("## Back up your data", 1)[1].split("```sh\n", 1)[1].split("```", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name, value in (("main checkout", 1), ("feature checkout", 2)):
                checkout = home / name
                (checkout / "data").mkdir(parents=True)
                with sqlite3.connect(checkout / "data/command_centre.sqlite3") as conn:
                    conn.execute("CREATE TABLE sample (value INTEGER)")
                    conn.execute("INSERT INTO sample VALUES (?)", (value,))
                result = subprocess.run(
                    ["sh", "-c", "umask 022\n" + snippet + '\nprintf "mask=%s" "$(umask)"'],
                    cwd=checkout,
                    env={**os.environ, "HOME": str(home)},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout, "mask=0022")
            for name, value in (("main checkout", 1), ("feature checkout", 2)):
                destination = home / "personal-ai-backups" / name
                backup = destination / "command_centre.sqlite3"
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                with sqlite3.connect(backup) as conn:
                    self.assertEqual(conn.execute("SELECT value FROM sample").fetchone(), (value,))
            empty = home / "empty"
            empty.mkdir()
            subprocess.run(["sh", "-c", snippet], cwd=empty, env={**os.environ, "HOME": str(home)}, check=True)
            self.assertEqual(list((home / "personal-ai-backups/empty").iterdir()), [])
