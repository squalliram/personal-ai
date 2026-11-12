import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from instagram_agent import analysis, cli, following, storage
from instagram_agent.graph import GraphAPIError, GraphClient

TOKEN = "EAAG-secret-token"
USER = "17841400000000001"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def media(media_id, when, likes, comments, caption="", media_type="CAROUSEL_ALBUM"):
    return {
        "id": media_id,
        "timestamp": when,
        "media_type": media_type,
        "permalink": f"https://www.instagram.com/p/{media_id}/",
        "caption": caption,
        "like_count": likes,
        "comments_count": comments,
    }


class FakeGraph(GraphClient):
    def __init__(self, responses):
        super().__init__(TOKEN, USER)
        self.responses, self.calls = responses, []

    def get(self, path_or_url, params=None):
        self.calls.append((path_or_url, params))
        key = path_or_url if not params or "fields" not in params else f"{path_or_url}?{params['fields'].split('{')[0]}"
        value = self.responses.get(key, self.responses.get(path_or_url, {}))
        if isinstance(value, Exception):
            raise value
        return value


def discovery(username, followers, posts):
    return {
        "business_discovery": {
            "username": username,
            "followers_count": followers,
            "media_count": 99,
            "media": {"data": posts},
        }
    }


class FollowingExportTests(unittest.TestCase):
    def test_reads_both_export_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "following.json")
            path.write_text(
                json.dumps(
                    {
                        "relationships_following": [
                            {
                                "string_list_data": [
                                    {"href": "https://www.instagram.com/bluebottle", "value": "BlueBottle"}
                                ]
                            },
                            {
                                "title": "dishoom",
                                "string_list_data": [{"href": "https://www.instagram.com/_u/dishoom"}],
                            },
                            {"string_list_data": [{"href": "https://www.instagram.com/_u/monmouthcoffee"}]},
                            {"string_list_data": [{"href": "https://evil.example/notme"}]},
                            {"title": "bluebottle"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(following.usernames(path), ["bluebottle", "dishoom", "monmouthcoffee"])


class GraphClientTests(unittest.TestCase):
    def test_errors_never_include_the_token(self):
        client = GraphClient(TOKEN, USER, retries=0)
        error = HTTPError(f"https://graph.facebook.com/v27.0/x?access_token={TOKEN}", 400, "bad", {}, io.BytesIO(b"{}"))
        with patch.object(client._opener, "open", side_effect=error), self.assertRaises(GraphAPIError) as caught:
            client.get(f"{USER}/media")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("HTTP 400", str(caught.exception))

    def test_paging_cannot_leave_the_graph_origin(self):
        client = GraphClient(TOKEN, USER)
        with self.assertRaises(GraphAPIError):
            client.get("https://attacker.example/v27.0/me")

    def test_business_discovery_rejects_field_injection(self):
        client = FakeGraph({})
        with self.assertRaises(ValueError):
            client.business_discovery("bluebottle){id}")

    def test_online_followers_averages_each_hour(self):
        client = FakeGraph(
            {
                f"{USER}/insights": {
                    "data": [
                        {
                            "name": "online_followers",
                            "values": [{"value": {"9": 100, "18": 300}}, {"value": {"9": 200, "18": 100, "25": 5}}],
                        }
                    ]
                }
            }
        )
        self.assertEqual(client.online_followers(), {9: 150, 18: 200})


class SyncAndAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = storage.connect(Path(self.tmp.name, "instagram.sqlite3"))
        storage.add_benchmarks(self.conn, ["bluebottle", "dishoom", "privatefoodie"], "following")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def graph(self):
        return FakeGraph(
            {
                f"{USER}/media": {
                    "data": [
                        media("101", "2026-11-10T17:05:00+0000", 120, 10, "Flat white heaven #coffee"),
                        media("102", "2026-11-09T08:00:00+0000", 40, 2),
                        media("103", "2026-09-01T08:00:00+0000", 10, 1),
                    ]
                },
                "101/insights": {
                    "data": [
                        {"name": "reach", "values": [{"value": 1000}]},
                        {"name": "saved", "values": [{"value": 30}]},
                        {"name": "likes", "total_value": {"value": 120}},
                    ]
                },
                "102/insights": GraphAPIError("Graph API request failed (HTTP 400)"),
                f"{USER}/insights": {
                    "data": [{"name": "online_followers", "values": [{"value": {"17": 500, "8": 100}}]}]
                },
                f"{USER}?business_discovery.username(bluebottle)": discovery(
                    "bluebottle",
                    260_000,
                    [
                        media("b1", "2026-11-08T10:00:00+0000", 9000, 400, "Pour over #coffee #ラテアート"),
                        media("b2", "2026-11-01T10:00:00+0000", 3000, 50, "#coffee #morning"),
                        media("b3", "2026-10-20T10:00:00+0000", 3100, 60),
                    ],
                ),
                f"{USER}?business_discovery.username(dishoom)": discovery(
                    "dishoom",
                    400_000,
                    [
                        media("d1", "2026-11-07T19:00:00+0000", 20000, 900, "Black daal #coffee #london"),
                        media("d2", "2026-11-02T19:00:00+0000", 8000, 100, "#london"),
                    ],
                ),
                f"{USER}?business_discovery.username(privatefoodie)": GraphAPIError(
                    "Graph API request failed (HTTP 400)"
                ),
            }
        )

    def test_sync_stores_posts_metrics_audience_and_benchmarks(self):
        counts = cli.sync(self.conn, self.graph(), now=NOW)
        self.assertEqual(counts, {"posts": 3, "metrics": 1, "metric_errors": 1, "benchmarks": 3, "benchmark_errors": 1})
        metric = self.conn.execute("SELECT * FROM post_metrics WHERE media_id='101'").fetchone()
        self.assertEqual(
            (metric["captured_on"], metric["reach"], metric["saved"], metric["likes"]), ("2026-11-12", 1000, 30, 120)
        )
        statuses = dict(self.conn.execute("SELECT username, status FROM benchmark_accounts"))
        self.assertEqual(statuses, {"bluebottle": "ok", "dishoom": "ok", "privatefoodie": "unavailable"})

    def test_rankings_outliers_and_hashtags(self):
        cli.sync(self.conn, self.graph(), now=NOW)
        self.assertEqual([a["username"] for a in analysis.benchmark_rankings(self.conn)], ["dishoom", "bluebottle"])
        top = analysis.top_benchmark_posts(self.conn, now=NOW)
        self.assertEqual(top[0]["permalink"], "https://www.instagram.com/p/b1/")
        self.assertEqual(top[0]["hashtags"], ["#coffee", "#ラテアート"])
        tags = analysis.trending_hashtags(self.conn, now=NOW)
        self.assertEqual(tags[0]["tag"], "#coffee")
        self.assertEqual(tags[0]["accounts"], 2)

    def test_best_hours_use_audience_then_own_reach(self):
        cli.sync(self.conn, self.graph(), now=NOW)
        self.assertEqual(analysis.best_hours(self.conn, top=2, audience_tz="UTC")[0], 17)
        self.assertEqual(analysis.best_hours(self.conn, top=1, min_posts=1), [17])

    def test_learned_examples_weight_approved_posts_by_performance(self):
        cli.sync(self.conn, self.graph(), now=NOW)
        with self.conn:
            storage.record_decision(self.conn, "d-1", "approved", "2026-11-10T11:00:00Z", topic="flat white")
            storage.link_published(self.conn, "d-1", "101")
            storage.record_decision(self.conn, "d-2", "rejected", "2026-11-10T11:00:00Z", topic="generic salad")
        learned = analysis.learned_examples(self.conn)
        self.assertEqual(learned["approved"][0]["media_id"], "101")
        self.assertEqual(learned["approved"][0]["weight"], 1.0)
        self.assertEqual([r["topic"] for r in learned["rejected"]], ["generic salad"])

    def test_unicode_hashtags_match_the_caption_rules(self):
        self.assertEqual(analysis.hashtags("Dal #हिन्दी #Street_Food, no#tag #street_food"), ["#हिन्दी", "#street_food"])


class CliTests(unittest.TestCase):
    def test_sync_without_credentials_fails_before_any_request(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            err = io.StringIO()
            with redirect_stderr(err), patch("instagram_agent.graph.GraphClient.get") as get:
                code = cli.main(
                    ["--env-file", str(Path(tmp, ".env")), "--database", str(Path(tmp, "i.sqlite3")), "sync"]
                )
            self.assertEqual(code, 2)
            get.assert_not_called()

    def test_import_following_then_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            export = Path(tmp, "following.json")
            export.write_text(json.dumps({"relationships_following": [{"title": "dishoom"}]}), encoding="utf-8")
            db = str(Path(tmp, "i.sqlite3"))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["--database", db, "import-following", str(export)]), 0)
                self.assertEqual(cli.main(["--database", db, "report"]), 0)
            self.assertIn("1 new benchmark accounts added", out.getvalue())
            self.assertIn('"best_hours_london": []', out.getvalue())


if __name__ == "__main__":
    unittest.main()
