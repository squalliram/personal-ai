import csv
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from instagram_agent.graph import GraphAPIError

SPEC = importlib.util.spec_from_file_location(
    "instagram_baseline", Path(__file__).resolve().parents[1] / "scripts/instagram-baseline.py"
)
BASELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASELINE)


def response(value):
    return {"data": [{"values": [{"value": value}]}]}


def media(media_id="1", **kwargs):
    return {"id": media_id, "timestamp": "2026-07-01T23:30:00Z", "media_type": "IMAGE", **kwargs}


class SummaryTests(unittest.TestCase):
    def test_empty_views_falls_back_to_plays_but_zero_does_not(self):
        for views, expected in [({"data": []}, 42), (response(0), 0)]:
            with self.subTest(expected=expected):
                posts, _ = BASELINE.summarize([media()], {"1": {"views": views, "plays": response(42)}}, {})
                self.assertEqual(posts[0]["views"], expected)

    def test_rates_medians_rankings_and_london_cadence(self):
        items = [
            media(),
            media("2", media_type="VIDEO", media_product_type="REELS"),
            media("3", media_type="CAROUSEL_ALBUM"),
        ]
        insights = {
            i: {
                k: response(v)
                for k, v in {"reach": 100, "views": views, "likes": 5, "comments": 1, "saved": 2, "shares": 2}.items()
            }
            for i, views in [("1", 10), ("2", 30), ("3", 20)]
        }
        posts, summary = BASELINE.summarize(items, insights, {"followers_count": 200})
        self.assertEqual(posts[0]["engagement_rate_reach"], 0.1)
        self.assertEqual(posts[0]["engagement_rate_followers"], 0.05)
        self.assertEqual(posts[1]["outlier_multiple"], 1.5)
        self.assertEqual(summary["by_type"]["reel"]["medians"]["views"], 30)
        self.assertEqual(summary["rankings"]["outlier_multiple"]["top"][0]["id"], "2")
        self.assertEqual(summary["cadence"]["by_day"], {"Thursday": 3})
        self.assertEqual(summary["cadence"]["by_hour"], {"0": 3})

    def test_empty_and_missing_are_not_zero(self):
        posts, summary = BASELINE.summarize([], {}, {})
        self.assertEqual(posts, [])
        self.assertIsNone(summary["median_views"])
        self.assertIsNone(summary["by_type"]["image"]["medians"]["reach"])
        posts, _ = BASELINE.summarize([media(like_count=2)], {"1": {"reach": response(0)}}, {"followers_count": 0})
        self.assertIsNone(posts[0]["interactions"])
        self.assertIsNone(posts[0]["engagement_rate_reach"])

    def test_total_interactions_precedes_sum(self):
        posts, _ = BASELINE.summarize([media()], {"1": {"total_interactions": response(12), "reach": response(24)}}, {})
        self.assertEqual(posts[0]["engagement_rate_reach"], 0.5)


class RedactionTests(unittest.TestCase):
    def test_preserves_public_permalink_but_not_token_bearing_urls(self):
        permalink = "https://www.instagram.com/reel/Abc_123/"
        self.assertEqual(BASELINE.redact({"permalink": permalink}, "private"), {"permalink": permalink})
        for url in (permalink + "?access_token=private", "https://graph.instagram.com/media?access_token=private"):
            self.assertNotIn("https://", BASELINE.redact(url, "private"))

    def test_nested_tokens_encoded_tokens_and_urls(self):
        token = "sentinel+private/token"
        payload = {
            "access_token": token,
            "nested": [
                "https://graph.example/?access_token=" + token,
                "sentinel%2Bprivate%2Ftoken",
                "Bearer abc123",
                token,
            ],
            token: "value",
        }
        result = json.dumps(BASELINE.redact(payload, token))
        for secret in (token, "sentinel%2Bprivate%2Ftoken", "https://", "abc123"):
            self.assertNotIn(secret, result)
        self.assertIn("REDACTED", result)


class ExportTests(unittest.TestCase):
    def test_full_pagination_and_independent_metric_errors(self):
        class Client:
            user_id = "123"

            def get(self, path, params):
                if path == "123":
                    return {"username": "instafoodielove", "followers_count": 50}
                if path == "123/media":
                    return {
                        "data": [media(str(i), permalink="https://www.instagram.com/p/Abc_123/") for i in range(101)],
                        "paging": {"next": "https://graph.example/next?access_token=private"},
                    }
                if path.startswith("https://"):
                    return {"data": [media("102", permalink="https://www.instagram.com/p/Abc_123/")]}
                if path == "123/insights":
                    return {"data": []}
                if params["metric"] == "saved":
                    raise GraphAPIError("unsafe private https://graph.example/error")
                return response(5)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            BASELINE.export(Client(), output, "private", datetime(2026, 11, 17, tzinfo=timezone.utc))
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["post_count"], 102)
            self.assertTrue(summary["media_history_complete"])
            self.assertEqual(summary["account_window"]["since"], "2026-10-20T00:00:00+01:00")
            with (output / "posts.csv").open() as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 102)
            for path in output.rglob("*"):
                if path.is_file():
                    self.assertNotIn("private", path.read_text())
                    self.assertNotIn("https://graph.", path.read_text())
            self.assertIn("https://www.instagram.com/p/Abc_123/", (output / "posts.csv").read_text())
            self.assertIn("https://www.instagram.com/p/Abc_123/", (output / "summary.json").read_text())
            errors = json.loads((output / "raw/errors.json").read_text())
            self.assertEqual(sum(e["request"].endswith(":saved") for e in errors), 102)

    def test_profile_error_is_sanitized_and_stops_collection(self):
        class Client:
            user_id = "123"

            def get(self, path, params):
                raise GraphAPIError("token https://unsafe")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "could not be verified"):
                BASELINE.export(Client(), Path(directory), "token")
            self.assertNotIn("https://unsafe", (Path(directory) / "raw/errors.json").read_text())


if __name__ == "__main__":
    unittest.main()
