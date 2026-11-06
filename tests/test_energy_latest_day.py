import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from octopus_energy.analytics import recompute_daily_costs
from octopus_energy.dashboard import dashboard_data
from octopus_energy.storage import connect, set_gas_units, upsert_consumption, upsert_meter, upsert_rate

LONDON = ZoneInfo("Europe/London")


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 5, 12, tzinfo=timezone.utc).astimezone(tz)


class LatestDayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "energy.sqlite3"
        self.conn = connect(self.database)
        self.addCleanup(self.conn.close)
        clock = patch("octopus_energy.dashboard.datetime", Clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.electricity = self.meter("electricity", "private-point", "private-meter")

    def meter(self, fuel, point, serial):
        return upsert_meter(
            self.conn,
            fuel=fuel,
            point_id=point,
            serial=serial,
            tariff_code="private-tariff",
            valid_from=None,
            valid_to=None,
            synced_at="2026-11-01T00:00:00Z",
        )

    def reading(self, meter, start, end, kwh):
        upsert_consumption(
            self.conn, meter, {"interval_start": start.isoformat(), "interval_end": end.isoformat(), "consumption": kwh}
        )

    def fill(self, date, meter=None, first=0, last=None):
        start = datetime.fromisoformat(date).replace(tzinfo=LONDON).astimezone(timezone.utc)
        end = (datetime.fromisoformat(date) + timedelta(days=1)).replace(tzinfo=LONDON).astimezone(timezone.utc)
        count = int((end - start).total_seconds() / 1800)
        for index in range(first, count if last is None else last):
            self.reading(
                meter or self.electricity,
                start + timedelta(minutes=30 * index),
                start + timedelta(minutes=30 * (index + 1)),
                (index // 2 + 1) / 2,
            )
        return start, end

    def rate(self, meter, kind="standard-unit-rates", value=20, start="2026-01-01T00:00:00Z", end=None):
        upsert_rate(self.conn, meter, kind, {"valid_from": start, "valid_to": end, "value_inc_vat": value})

    def data(self):
        self.conn.commit()
        return dashboard_data(self.database)

    def test_normal_spring_and_fall_days_have_actual_hours_and_distinct_offsets(self):
        for date, expected in [("2026-03-29", 23), ("2026-10-25", 25), ("2026-11-01", 24)]:
            with self.subTest(date=date):
                self.fill(date)
                day = self.data()["latest_day"]["electricity"]
                self.assertEqual(day["date"], date)
                self.assertTrue(day["complete"])
                self.assertEqual(len(day["hours"]), expected)
                self.assertEqual([row["kwh"] for row in day["hours"]], list(range(1, expected + 1)))
                self.assertEqual(len({row["start"] for row in day["hours"]}), expected)
                self.assertTrue(all(row["cost"] is None for row in day["hours"]))
                if expected == 25:
                    self.assertEqual(day["hours"][1]["start"], "2026-10-25T01:00:00+01:00")
                    self.assertEqual(day["hours"][2]["start"], "2026-10-25T01:00:00+00:00")
                if expected == 23:
                    self.assertFalse(any("T01:" in row["start"] for row in day["hours"]))

    def test_latest_complete_day_skips_gap_and_current_or_future_days(self):
        self.fill("2026-11-01")
        self.fill("2026-11-03", last=20)
        self.fill("2026-11-03", first=21)
        self.fill("2026-11-04", last=47)
        self.fill("2026-11-05")
        self.fill("2026-11-06")
        self.assertEqual(self.data()["latest_day"]["electricity"]["date"], "2026-11-01")

    def test_replacement_meters_union_coverage_but_each_observed_supply_must_be_complete(self):
        self.fill("2026-11-01", last=24)
        replacement = self.meter("electricity", "private-point", "replacement-meter")
        self.fill("2026-11-01", replacement, first=24)
        self.assertEqual(len(self.data()["latest_day"]["electricity"]["hours"]), 24)
        other = self.meter("electricity", "second-private-point", "other-meter")
        self.fill("2026-11-01", other, last=1)
        self.assertIsNone(self.data()["latest_day"]["electricity"])
        self.fill("2026-11-01", other, first=1)
        day = self.data()["latest_day"]["electricity"]
        self.assertEqual(day["hours"][0]["kwh"], 2)
        self.assertNotIn("private", json.dumps(day))

    def test_usage_cost_excludes_standing_charge_without_changing_daily_total(self):
        self.fill("2026-11-01")
        self.rate(self.electricity)
        self.rate(self.electricity, "standing-charges", 1000)
        recompute_daily_costs(self.conn)
        data = self.data()
        day = data["latest_day"]["electricity"]
        self.assertEqual(day["cost_basis"], "usage_excludes_standing_charge")
        self.assertAlmostEqual(sum(row["cost"] for row in day["hours"]), 60)
        self.assertEqual(data["daily"][0]["cost"], 70)

    def test_missing_rate_propagates_null_per_hour_and_long_reading_rate_changes_stay_unknown(self):
        start, end = self.fill("2026-11-01")
        self.rate(self.electricity, end="2026-11-01T12:00:00Z")
        hours = self.data()["latest_day"]["electricity"]["hours"]
        self.assertTrue(all(row["cost"] is not None for row in hours[:12]))
        self.assertTrue(all(row["cost"] is None for row in hours[12:]))
        self.conn.execute("DELETE FROM consumption")
        self.reading(self.electricity, start, end, 24)
        self.rate(self.electricity, value=30, start="2026-11-01T12:00:00Z")
        self.assertTrue(all(row["cost"] is None for row in self.data()["latest_day"]["electricity"]["hours"]))

    def test_cross_hour_and_cross_midnight_readings_are_duration_split(self):
        start = datetime(2026, 10, 31, 23, 45, tzinfo=timezone.utc)
        self.reading(self.electricity, start, start + timedelta(minutes=30), 2)
        self.reading(self.electricity, start + timedelta(minutes=30), start + timedelta(days=1, minutes=30), 96)
        hours = self.data()["latest_day"]["electricity"]["hours"]
        self.assertEqual(len(hours), 24)
        self.assertEqual(hours[0]["kwh"], 4)
        self.assertEqual(hours[-1]["kwh"], 4)

    def test_fuels_select_independent_days_and_gas_requires_confirmed_kwh(self):
        self.fill("2026-11-02")
        gas = self.meter("gas", "private-gas", "private-gas-meter")
        self.fill("2026-11-01", gas)
        self.assertIsNone(self.data()["latest_day"]["gas"])
        set_gas_units(self.conn, "kwh")
        data = self.data()
        self.assertEqual(data["latest_day"]["electricity"]["date"], "2026-11-02")
        self.assertEqual(data["latest_day"]["gas"]["date"], "2026-11-01")
        self.assertNotIn("private-", json.dumps(data))
