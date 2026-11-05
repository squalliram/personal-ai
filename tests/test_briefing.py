import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from daily_briefing.briefing import calendar_overlaps, rank_tasks, render


class BriefingTests(unittest.TestCase):
    def test_detects_overlap(self):
        events = [{"id":"a", "start":"2026-11-05T09:00:00+00:00", "end":"2026-11-05T10:00:00+00:00"}, {"id":"b", "start":"2026-11-05T09:30:00+00:00", "end":"2026-11-05T11:00:00+00:00"}]
        self.assertEqual(len(calendar_overlaps(events)), 1)

    def test_overdue_high_task_ranks_first(self):
        tasks = [{"title":"Later", "due":"2026-11-07", "priority":"low"}, {"title":"Urgent", "due":"2026-11-04", "priority":"high"}]
        self.assertEqual(rank_tasks(tasks, date(2026, 11, 5))[0]["title"], "Urgent")

    def test_render_deduplicates_and_labels_suggestions(self):
        event = {"id":"same", "title":"Meet", "start":"2026-11-05T09:00:00+00:00", "end":"2026-11-05T10:00:00+00:00"}
        text = render(date(2026, 11, 5), datetime(2026, 11, 5, tzinfo=ZoneInfo("Europe/London")), [event, event], [], [])
        self.assertEqual(sum(line.endswith(": Meet") for line in text.splitlines()), 1)
        self.assertIn("These are suggestions, not commitments.", text)
