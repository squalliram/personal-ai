import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliIntegrationTests(unittest.TestCase):
    def test_generates_report_and_records_non_sensitive_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps({"events": [{"id": "e1", "title": "Planning", "start": "2026-11-05T09:00:00+00:00", "end": "2026-11-05T10:00:00+00:00"}], "tasks": [{"title": "Deliver", "due": "2026-11-05", "priority": "high"}], "errors": []}), encoding="utf-8")
            reports, database = root / "reports", root / "history.sqlite3"
            result = subprocess.run([sys.executable, "-m", "daily_briefing.cli", "--snapshot", str(snapshot), "--output-dir", str(reports), "--database", str(database)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = next(reports.glob("*.md")).read_text(encoding="utf-8")
            self.assertIn("Planning", report)
            with sqlite3.connect(database) as conn:
                self.assertEqual(conn.execute("SELECT calendar_events, tasks_processed, error_count FROM execution_history").fetchone(), (1, 1, 0))
