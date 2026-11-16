"""Home page signals: per-fuel series, rule-based energy suggestions and automation-sourced attention."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from command_centre_fixtures import Client, make_app, make_settings, seed_energy

from command_centre import insights

LONDON = insights.LONDON
NOW = datetime(2026, 11, 15, 20, 0, tzinfo=LONDON)
TODAY = NOW.date()


def history(fuel="electricity", kwh=lambda day: 10.0, days=70, rate=0.25):
    rows = []
    for offset in range(days, 0, -1):
        day = TODAY - timedelta(days=offset)
        value = kwh(day)
        cost = None if value is None else round(value * rate, 2)
        rows.append({"date": day.isoformat(), "fuel": fuel, "consumption_kwh": value, "total_cost_gbp": cost})
    return rows


def last_week(value, otherwise=10.0):
    return lambda day: value if day >= TODAY - timedelta(days=7) else otherwise


def hourly(night, day=0.5, peak=1.0):
    return [
        {"fuel": "electricity", "hour": h, "kwh": night if 1 <= h <= 4 else peak if h == 18 else day} for h in range(24)
    ]


AVAILABLE = {"state": "available", "anomalies": []}


def fuels(rows, dashboard=None, energy=AVAILABLE):
    return insights.energy_fuels(energy, rows, dashboard, NOW)


class EnergyFuelsTests(unittest.TestCase):
    def test_non_finite_readings_are_treated_as_missing(self):
        rows = history(kwh=lambda day: float("nan") if day == TODAY - timedelta(days=1) else 10.0)
        rows += history("gas", lambda day: float("inf"))
        found = fuels(rows)
        self.assertEqual(found["electricity"]["days"][-1]["kwh"], None)
        self.assertIsNone(found["electricity"]["week"]["kwh"])
        self.assertEqual(found["gas"]["state"], "unavailable")
        json.dumps(insights.public_fuels(found), allow_nan=False)

    def test_each_fuel_gets_its_own_fourteen_complete_days(self):
        rows = history() + history("gas", lambda day: 30.0)
        rows.append({"date": TODAY.isoformat(), "fuel": "electricity", "consumption_kwh": 3.0, "total_cost_gbp": 1.0})
        found = insights.public_fuels(fuels(rows))
        electricity, gas = found["electricity"], found["gas"]
        self.assertEqual(electricity["through"], "2026-11-14")
        self.assertEqual(len(electricity["days"]), insights.CHART_DAYS)
        self.assertEqual(electricity["days"][-1], {"date": "2026-11-14", "kwh": 10.0, "cost": 2.5})
        self.assertEqual(electricity["week"]["kwh"], 70.0)
        self.assertEqual(electricity["week"]["change"], 0.0)
        self.assertEqual(electricity["basis"], {"kwh": "measured", "cost": "estimated"})
        self.assertEqual(gas["week"]["kwh"], 210.0)
        self.assertNotIn("_series", electricity)
        json.dumps(found)

    def test_unconfirmed_gas_units_and_missing_fuels_are_not_guessed(self):
        found = fuels(history() + history("gas", lambda day: None))
        self.assertEqual(found["gas"]["state"], "unavailable")
        self.assertIn("GAS_UNITS", found["gas"]["reason"])
        self.assertEqual(fuels(history())["gas"]["state"], "no_data")
        self.assertEqual(fuels(history(), energy={"state": "refresh_required"}), {})

    def test_published_latest_reading_limits_the_complete_days(self):
        dashboard = {"latest_reading_at": {"electricity": "2026-11-14T00:00:00+00:00"}}
        self.assertEqual(fuels(history(), dashboard)["electricity"]["through"], "2026-11-13")

    def test_a_gap_in_the_week_leaves_the_total_unknown(self):
        found = fuels(history(kwh=lambda day: None if day == TODAY - timedelta(days=3) else 10.0))
        self.assertIsNone(found["electricity"]["week"]["kwh"])
        self.assertIsNone(found["electricity"]["week"]["change"])


class EnergyInsightTests(unittest.TestCase):
    def insights(self, rows, dashboard=None, anomalies=()):
        energy = {"state": "available", "anomalies": list(anomalies)}
        return insights.energy_insights(energy, insights.energy_fuels(energy, rows, dashboard, NOW))

    def test_steady_usage_produces_no_filler(self):
        self.assertEqual(self.insights(history() + history("gas", lambda day: 30.0)), [])

    def test_a_weekly_rise_is_a_grounded_suggestion(self):
        (item,) = self.insights(history(kwh=last_week(13.0)))
        self.assertEqual(
            (item["id"], item["severity"], item["kind"], item["href"]),
            ("electricity-week", "warning", "suggestion", "#energy"),
        )
        self.assertEqual(item["title"], "Electricity up 30% this week")
        self.assertIn("91.0 kWh in the 7 days to Sat 14 Nov against 70.0 kWh", item["detail"])
        self.assertIn("£5.25 more, estimated", item["detail"])
        self.assertEqual((item["evidence"]["kwh"], item["evidence"]["previous_kwh"]), (91.0, 70.0))
        self.assertTrue(item["suggestion"])

    def test_weekly_cost_direction_follows_pounds_not_kwh(self):
        rows = history(kwh=last_week(13.0))
        for row in rows:
            if row["date"] >= (TODAY - timedelta(days=7)).isoformat():
                row["total_cost_gbp"] = 1.0
        (item,) = self.insights(rows)
        self.assertEqual(item["title"], "Electricity up 30% this week")
        self.assertIn("£10.50 less, estimated", item["detail"])

    def test_month_comparison_uses_daily_averages_across_unequal_months(self):
        through = date(2026, 3, 31)
        rows = []
        for offset in range(70):
            day = through - timedelta(days=offset)
            value = 10.5 if day.month == 3 else 10.0
            rows.append(
                {"date": day.isoformat(), "fuel": "electricity", "consumption_kwh": value, "total_cost_gbp": None}
            )
        now = datetime(2026, 4, 1, 20, 0, tzinfo=LONDON)
        found = insights.energy_fuels(AVAILABLE, rows, None, now)
        self.assertIsNone(insights._month_insight("electricity", found["electricity"]))

    def test_a_month_rise_of_exactly_the_threshold_is_reported(self):
        rows = history(kwh=lambda day: 11.5 if day.month == 11 else 10.0)
        (item,) = self.insights(rows)
        self.assertEqual((item["id"], item["evidence"]["change"]), ("electricity-month", 0.15))

    def test_a_drop_is_reported_once_as_good_news(self):
        (item,) = self.insights(history(kwh=last_week(7.0)))
        self.assertEqual(item["severity"], "good")
        self.assertEqual(item["fuel"], "electricity")

    def test_month_to_date_projects_an_estimated_cost(self):
        rows = history(kwh=lambda day: 14.0 if day.month == 11 else 10.0)
        (item,) = self.insights(rows)
        self.assertEqual(item["id"], "electricity-month")
        self.assertEqual(item["evidence"]["days"], 14)
        self.assertEqual(item["evidence"]["projected_cost"], 105.0)
        self.assertEqual(item["title"], "Electricity on track for about £105 in November")

    def test_unusual_day_compares_with_the_typical_day(self):
        anomalies = [
            {"date": "2026-11-12", "fuel": "gas", "consumption_kwh": 40.0},
            {"date": "2026-11-01", "fuel": "gas", "consumption_kwh": 90.0},
        ]
        (item,) = self.insights(history("gas"), anomalies=anomalies)
        self.assertEqual(item["id"], "gas-unusual")
        self.assertEqual(item["evidence"], {"date": "2026-11-12", "kwh": 40.0, "typical_kwh": 10.0})
        self.assertIn("4.0×", item["detail"])

    def test_always_on_load_needs_a_high_overnight_share(self):
        (item,) = self.insights(history(), {"hourly": hourly(0.3)})
        self.assertEqual(item["id"], "electricity-always-on")
        self.assertEqual(item["severity"], "info")
        self.assertEqual(item["evidence"]["peak_hour"], 18)
        self.assertIn("peaks at 18:00", item["detail"])
        self.assertEqual(self.insights(history(), {"hourly": hourly(0.05)}), [])
        self.assertEqual(self.insights(history(), {"hourly": hourly(0.3)[:20]}), [])

    def test_warnings_come_first_and_the_list_is_capped(self):
        rows = history(kwh=last_week(13.0)) + history("gas", last_week(39.0, 30.0))
        anomalies = [{"date": "2026-11-12", "fuel": fuel, "consumption_kwh": 99.0} for fuel in ("gas", "electricity")]
        found = self.insights(rows, {"hourly": hourly(0.3)}, anomalies)
        self.assertEqual(len(found), insights.MAX_INSIGHTS)
        self.assertEqual([item["severity"] for item in found], ["warning"] * 4)


def job(job_id, status, summary="Summary.", **latest):
    return {
        "id": job_id,
        "name": job_id.title(),
        "status": status,
        "summary": summary,
        "last_execution": latest or None,
    }


def health(item_id, status, **evidence):
    return {"id": item_id, "name": item_id, "status": status, "summary": "Summary.", "evidence": evidence}


def draft(**fields):
    return {"status": "pending", "created_on": "2026-11-15", "publish_error": None, "will_post": False, **fields}


class AttentionTests(unittest.TestCase):
    def data(self, jobs=(), integrations=(), instagram=None):
        return {
            "automations": list(jobs),
            "integrations": list(integrations),
            "instagram": instagram or {"state": "not_configured", "drafts": []},
        }

    def test_failing_automations_are_listed_and_healthy_ones_are_not(self):
        jobs = [
            job("energy-refresh", "failed", "Latest refresh failed."),
            job("daily-briefing", "degraded", "1 connector error."),
            job("instagram-daily", "operational"),
            job("energy-mcp", "unverified"),
        ]
        integrations = [health("octopus_energy", "degraded", latest_daily_date="2026-11-14")]
        found = insights.attention(self.data(jobs, integrations), NOW)
        self.assertEqual(
            [(a["source"], a["severity"]) for a in found],
            [("energy-refresh", "critical"), ("daily-briefing", "warning")],
        )
        self.assertEqual(found[0]["fact"], "Energy-Refresh: Latest refresh failed.")
        self.assertEqual(found[0]["href"], "#automations")

    def test_manual_briefing_going_stale_is_not_an_alarm(self):
        self.assertEqual(insights.attention(self.data([job("daily-briefing", "stale")]), NOW), [])

    def test_missing_ai_credentials_for_instagram_is_flagged(self):
        found = insights.attention(self.data([job("instagram-daily", "not_configured", outcome="not_configured")]), NOW)
        self.assertEqual([a["severity"] for a in found], ["warning"])

    def test_energy_source_problems_show_once(self):
        integrations = [health("octopus_energy", "stale", latest_daily_date="2026-11-14")]
        (item,) = insights.attention(self.data([job("energy-refresh", "unverified")], integrations), NOW)
        self.assertEqual((item["source"], item["href"]), ("octopus_energy", "#energy"))
        jobs = [job("energy-refresh", "stale")]
        self.assertEqual(
            [a["source"] for a in insights.attention(self.data(jobs, integrations), NOW)], ["energy-refresh"]
        )

    def test_successful_syncs_without_new_readings_need_attention(self):
        lagging = [health("octopus_energy", "operational", latest_daily_date="2026-11-05")]
        (item,) = insights.attention(self.data(integrations=lagging), NOW)
        self.assertIn("readings stop at Thu 5 Nov", item["fact"])
        fresh = [health("octopus_energy", "operational", latest_daily_date="2026-11-14")]
        self.assertEqual(insights.attention(self.data(integrations=fresh), NOW), [])

    def test_instagram_failed_publish_and_auto_post_deadline(self):
        instagram = {
            "state": "available",
            "auto_publish": True,
            "latest_attempt": {"outcome": "ok"},
            "drafts": [
                draft(status="failed", publish_error="secret upstream text"),
                draft(status="rejected", publish_error="later rejected"),
                draft(will_post=True, posts_at="2026-11-16T08:00:00+00:00"),
                draft(will_post=True, posts_at="2026-11-15T08:00:00+00:00"),
            ],
        }
        failed, pending = insights.attention(self.data(instagram=instagram), NOW)
        self.assertEqual((failed["severity"], failed["evidence"]), ("critical", {"failed_publishes": 1}))
        self.assertNotIn("secret", json.dumps(failed))
        self.assertIn("Review 2 Instagram drafts", pending["fact"])
        self.assertIn("next posts automatically at 08:00 on Mon 16 Nov unless you reject it", pending["fact"])
        self.assertEqual(pending["evidence"]["next_auto_post_at"], "2026-11-16T08:00+00:00")

    def test_no_drafts_today_is_flagged_after_midday_only(self):
        instagram = {
            "state": "available",
            "latest_attempt": {"outcome": "ok"},
            "drafts": [draft(status="approved", created_on="2026-11-14")],
        }
        (item,) = insights.attention(self.data(instagram=instagram), NOW)
        self.assertEqual((item["source"], item["severity"]), ("instagram-daily", "warning"))
        self.assertEqual(insights.attention(self.data(instagram=instagram), NOW.replace(hour=11)), [])
        flagged = [job("instagram-daily", "degraded")]
        self.assertEqual(
            [a["source"] for a in insights.attention(self.data(flagged, instagram=instagram), NOW)], ["instagram-daily"]
        )

    def test_status_counts_issues_actions_and_evidenced_automations(self):
        items = [{"severity": "warning"}, {"severity": "action"}]
        jobs = [job("a", "operational"), job("b", "degraded"), job("c", "unverified")]
        self.assertEqual(
            insights.system_status(items, jobs),
            {"state": "degraded", "issues": 1, "actions": 1, "automations_monitored": 2, "automations_healthy": 1},
        )
        self.assertEqual(insights.system_status([{"severity": "critical"}], jobs)["state"], "fault")
        self.assertEqual(insights.system_status([], jobs)["state"], "nominal")
        self.assertEqual(insights.system_status([], [job("c", "unverified")])["state"], "standby")

    def test_instagram_schedule_uses_london_dates_and_future_slots(self):
        instagram = {
            "drafts": [
                draft(posts_at="2026-11-15T19:00:00+00:00"),
                draft(status="approved", created_on="2026-11-14", posts_at="2026-11-16T08:00:00+00:00"),
                draft(status="rejected", posts_at="2026-11-15T21:00:00+00:00"),
            ]
        }
        self.assertEqual(
            insights.instagram_schedule(instagram, NOW), {"drafts_today": 2, "next_slot": "2026-11-16T08:00+00:00"}
        )


class OverviewPayloadTests(unittest.TestCase):
    def test_overview_carries_fuels_insights_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), inspect_crontab=False)
            seed_energy(settings.energy_db, days=20)
            client = Client(make_app(settings))
            client.login()
            data = json.loads(client.request("GET", "/api/v1/overview").body)
        self.assertEqual(
            set(data["status"]), {"state", "issues", "actions", "automations_monitored", "automations_healthy"}
        )
        self.assertIsInstance(data["insights"], list)
        fuels = data["energy"]["fuels"]
        self.assertEqual(fuels["electricity"]["state"], "available")
        self.assertEqual(len(fuels["electricity"]["days"]), insights.CHART_DAYS)
        self.assertTrue(all(isinstance(row["kwh"], float) for row in fuels["electricity"]["days"][-7:]))
        self.assertEqual(fuels["gas"]["state"], "no_data")
        self.assertEqual(set(data["instagram"]) >= {"drafts_today", "next_slot"}, True)
        self.assertNotIn("missing_integrations", data)
        self.assertNotIn("MPAN-SECRET", json.dumps(data))
        self.assertLessEqual(date.fromisoformat(fuels["electricity"]["through"]), date.today())


if __name__ == "__main__":
    unittest.main()
