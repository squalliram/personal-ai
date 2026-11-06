import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from daily_briefing.snapshot import load, parse


class ParseTests(unittest.TestCase):
    def test_valid_snapshot_passes_through_unchanged(self):
        data = {
            "events": [
                {"id": "e1", "title": "Planning", "start": "2026-11-05T09:00:00+00:00", "end": "2026-11-05T10:00:00Z"}
            ],
            "tasks": [{"title": "Deliver", "due": "2026-11-05", "priority": "high"}],
            "errors": ["connector note"],
            "future_field": {"nested": [1, 2]},
        }
        snapshot = parse(data)
        self.assertEqual(snapshot.events, data["events"])
        self.assertEqual(snapshot.tasks, data["tasks"])
        self.assertEqual(snapshot.errors, ["connector note"])

    def test_root_that_is_not_an_object_becomes_a_counted_error(self):
        for bad in ([], "oops", 42, None):
            snapshot = parse(bad)
            self.assertEqual((snapshot.events, snapshot.tasks), ([], []))
            self.assertEqual(snapshot.errors, ["Snapshot input failure: root is not an object"])

    def test_non_list_collections_are_reported(self):
        snapshot = parse({"events": "oops", "tasks": {"title": "x"}, "errors": "boom"})
        self.assertEqual((snapshot.events, snapshot.tasks), ([], []))
        self.assertEqual(
            snapshot.errors,
            [
                "Snapshot input failure: events is not a list",
                "Snapshot input failure: tasks is not a list",
                "Snapshot input failure: errors is not a list",
            ],
        )

    def test_event_with_bad_timestamp_is_dropped_and_counted(self):
        good = {"title": "Standup", "start": "2026-11-05T09:00:00+00:00", "end": "2026-11-05T09:15:00+00:00"}
        snapshot = parse({"events": [good, {"title": "Broken", "start": "not-a-date", "end": "also-bad"}, "junk"]})
        self.assertEqual(snapshot.events, [good])
        self.assertEqual(snapshot.errors, ["Snapshot input failure: dropped 2 invalid events"])

    def test_non_string_fields_drop_the_item(self):
        snapshot = parse({"tasks": [{"title": ["not", "text"]}, {"title": "ok", "due": 20261105}, {"title": "fine"}]})
        self.assertEqual(snapshot.tasks, [{"title": "fine"}])
        self.assertEqual(snapshot.errors, ["Snapshot input failure: dropped 2 invalid tasks"])

    def test_nullable_fields_and_unknown_keys_survive(self):
        event = {"title": "All day", "start": None, "end": None, "url": "https://example.com", "mystery": "kept"}
        self.assertEqual(parse({"events": [event]}).events, [event])

    def test_non_text_connector_errors_are_dropped(self):
        snapshot = parse({"errors": ["real", {"bad": 1}]})
        self.assertEqual(snapshot.errors, ["real", "Snapshot input failure: dropped non-text connector errors"])

    def test_missing_sections_default_to_empty(self):
        self.assertEqual(parse({}).errors, [])


class LoadTests(unittest.TestCase):
    def test_missing_file_is_a_snapshot_error_not_a_crash(self):
        snapshot = load("/nonexistent/snapshot.json")
        self.assertEqual(snapshot.errors, ["Snapshot input failure: FileNotFoundError"])

    def test_malformed_json_is_a_snapshot_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text("{not json", encoding="utf-8")
            snapshot = load(path)
            self.assertEqual(snapshot.errors, ["Snapshot input failure: JSONDecodeError"])


class CliRegressionTests(unittest.TestCase):
    def run_cli(self, root, payload):
        snapshot = root / "snapshot.json"
        snapshot.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        reports, database = root / "reports", root / "history.sqlite3"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "daily_briefing.cli",
                "--snapshot",
                str(snapshot),
                "--output-dir",
                str(reports),
                "--database",
                str(database),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result, reports, database

    def test_bad_timestamps_still_produce_a_report_and_run_row(self):
        # Regression: malformed start values used to crash with an uncaught ValueError,
        # writing no report and recording no run.
        with tempfile.TemporaryDirectory() as directory:
            result, reports, database = self.run_cli(
                Path(directory),
                {"events": [{"title": "Broken", "start": "not-a-date", "end": "also-bad"}], "tasks": []},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = next(reports.glob("*.md")).read_text(encoding="utf-8")
            self.assertIn("dropped 1 invalid events", report)
            self.assertNotIn("not-a-date", report)
            with sqlite3.connect(database) as conn:
                self.assertEqual(
                    conn.execute("SELECT calendar_events, error_count FROM execution_history").fetchone(),
                    (0, 1),
                )

    def test_non_object_root_still_produces_a_report_and_run_row(self):
        with tempfile.TemporaryDirectory() as directory:
            result, reports, database = self.run_cli(Path(directory), '{"events": "oops"}')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("events is not a list", next(reports.glob("*.md")).read_text(encoding="utf-8"))
            with sqlite3.connect(database) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_history").fetchone(), (1,))


if __name__ == "__main__":
    unittest.main()
