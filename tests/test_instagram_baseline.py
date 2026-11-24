import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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


class ConfigurationTests(unittest.TestCase):
    def test_help_does_not_import_publishing_pipeline(self):
        result = subprocess.run([sys.executable, str(SPEC.origin), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output", result.stdout)

    def test_login_host_and_version_configuration(self):
        for token, host in [
            ("IG_test", "https://graph.instagram.com/"),
            ("facebook_test", "https://graph.facebook.com/"),
        ]:
            with self.subTest(host=host):
                env = {"INSTAGRAM_ACCESS_TOKEN": token, "INSTAGRAM_USER_ID": "123"}
                self.assertEqual(BASELINE.graph_client(env).base_url, host + BASELINE.GRAPH_VERSION + "/")
                env["META_GRAPH_VERSION"] = "v26.0"
                self.assertEqual(BASELINE.graph_client(env).base_url, host + "v26.0/")

    def test_missing_or_invalid_credentials_fail_locally(self):
        with self.assertRaises(KeyError):
            BASELINE.graph_client({})
        for token, user in [("", "123"), ("test", "invalid")]:
            with self.assertRaises(ValueError):
                BASELINE.graph_client({"INSTAGRAM_ACCESS_TOKEN": token, "INSTAGRAM_USER_ID": user})


class SummaryTests(unittest.TestCase):
    def test_empty_views_stays_missing_but_zero_is_preserved(self):
        for views, expected in [({"data": []}, None), (response(0), 0)]:
            with self.subTest(expected=expected):
                posts, _ = BASELINE.summarize([media()], {"1": {"views": views}}, {})
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
    def test_permalink_sentence_punctuation_is_preserved(self):
        permalink = "HTTPS://www.instagram.com/reel/AbC_123/"
        for punctuation in (".", ",", ";", ":", "!", "?", ")", "]", "}", ")."):
            with self.subTest(punctuation=punctuation):
                text = "See " + permalink + punctuation
                self.assertEqual(BASELINE.redact(text, ""), text)
        for suffix in ("?tracking=1.", "#fragment.", "#"):
            self.assertNotIn(permalink, BASELINE.redact(permalink + suffix, ""))

    def test_mixed_case_url_schemes_and_public_permalink(self):
        for scheme in ("HTTPS", "hTtPs", "HTTP", "hTtP"):
            with self.subTest(scheme=scheme):
                self.assertEqual(BASELINE.redact(f"{scheme}://example.com/private", ""), "[REDACTED_URL]")
        permalink = "HtTpS://WWW.Instagram.COM/reel/AbC_123/"
        self.assertEqual(BASELINE.redact(permalink, ""), permalink)
        self.assertEqual(BASELINE.redact(permalink + "?access_token=private", "private"), "[REDACTED_URL]")

    def test_malformed_caption_url_is_redacted(self):
        self.assertEqual(
            BASELINE.redact({"caption": "Try https://[example.com today"}, "private"),
            {"caption": "Try [REDACTED_URL] today"},
        )

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
    def test_reel_missing_views_is_recorded_without_requesting_plays(self):
        class Client:
            user_id = "123"

            def __init__(self):
                self.metrics = []

            def get(self, path, params):
                if path == "123":
                    return {"username": "instafoodielove"}
                if path == "123/media":
                    return {"data": [media(media_type="VIDEO", media_product_type="REELS")]}
                if path in ("123/insights", "123/stories"):
                    return {"data": []}
                self.metrics.append(params["metric"])
                return response(None if params["metric"] == "views" else 42)

        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            output = Path(directory)
            BASELINE.export(client, output, "")
            self.assertNotIn("plays", client.metrics)
            self.assertIn("ig_reels_avg_watch_time", client.metrics)
            with (output / "posts.csv").open() as handle:
                self.assertEqual(list(csv.DictReader(handle))[0]["views"], "")
            summary = json.loads((output / "summary.json").read_text())
            self.assertIsNone(summary["by_type"]["reel"]["medians"]["views"])
            errors = json.loads((output / "raw/errors.json").read_text())
            self.assertIn({"request": "media:1:views", "status": "no_value"}, errors)

    def test_concurrent_claim_of_raw_directory_fails_before_api_call(self):
        class Client:
            def get(self, *args):
                raise AssertionError("API must not be called")

        mkdir = Path.mkdir
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            raw = output / "raw"

            def competing_mkdir(path, *args, **kwargs):
                if path == raw:
                    mkdir(path)
                    (path / "other-capture.json").write_text("{}")
                return mkdir(path, *args, **kwargs)

            with patch.object(Path, "mkdir", competing_mkdir):
                with self.assertRaisesRegex(FileExistsError, "already claimed"):
                    BASELINE.export(Client(), output, "")
            self.assertEqual(list(raw.iterdir()), [raw / "other-capture.json"])

    def test_second_export_requires_fresh_directory_to_avoid_stale_files(self):
        class Client:
            user_id = "123"

            def __init__(self, old=False):
                self.old = old
                self.calls = 0

            def get(self, path, params):
                self.calls += 1
                if path == "123":
                    return {"username": "instafoodielove"}
                if path == "123/media":
                    return {"data": [media("1")], **({"paging": {"next": "next"}} if self.old else {})}
                if path == "123/stories":
                    return {"data": []}
                if path == "next":
                    return {"data": [media("2")]}
                return response(1)

        with tempfile.TemporaryDirectory() as directory:
            old_output = Path(directory) / "old"
            BASELINE.export(Client(old=True), old_output, "")
            self.assertTrue((old_output / "raw/media_page_00002.json").exists())
            self.assertTrue((old_output / "raw/insights_2.json").exists())
            original = {p.relative_to(old_output): p.read_bytes() for p in old_output.rglob("*") if p.is_file()}
            client = Client()
            with self.assertRaisesRegex(FileExistsError, "Output directory must be empty"):
                BASELINE.export(client, old_output, "")
            self.assertEqual(client.calls, 0)
            self.assertEqual(
                original, {p.relative_to(old_output): p.read_bytes() for p in old_output.rglob("*") if p.is_file()}
            )
            new_output = Path(directory) / "new"
            BASELINE.export(client, new_output, "")
            self.assertFalse((new_output / "raw/media_page_00002.json").exists())
            self.assertFalse((new_output / "raw/insights_2.json").exists())
            self.assertEqual(json.loads((new_output / "summary.json").read_text())["post_count"], 1)

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
                if path in ("123/insights", "123/stories"):
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

    def test_recent_feed_and_active_story_conversion_are_reported_separately(self):
        now = datetime(2026, 11, 17, tzinfo=timezone.utc)

        class Client:
            user_id = "123"
            paths = []

            def get(self, path, params):
                self.paths.append(path)
                if path == "123":
                    return {"username": "instafoodielove"}
                if path == "123/media":
                    return {"data": [{**media("1"), "timestamp": now.isoformat()}]}
                if path == "123/stories":
                    return {"data": [media("2", media_product_type="STORY")]}
                if path == "123/insights":
                    return {"data": []}
                if path == "2/insights" and params.get("breakdown") == "story_navigation_action_type":
                    return response({"tap_forward": 8, "tap_back": 0})
                if params["metric"] == "profile_visits":
                    raise GraphAPIError("Unsupported metric")
                return response(0 if params["metric"] == "follows" else 30)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            client = Client()
            BASELINE.export(client, output, "", now)
            self.assertLess(client.paths.index("2/insights"), client.paths.index("123/media"))
            story = json.loads((output / "raw/active_stories.json").read_text())["data"][0]
            self.assertEqual((story["id"], story["insights"]["reach"]), ("2", 30))
            self.assertEqual(story["insights"]["follows"], 0)
            self.assertIsNone(story["insights"]["profile_visits"])
            self.assertEqual(story["insights"]["navigation"], 8)
            self.assertEqual(story["navigation_breakdown"], {"tap_forward": 8, "tap_back": 0})
            with (output / "posts.csv").open() as handle:
                self.assertEqual(
                    [(p["id"], p["follows"], p["profile_visits"]) for p in csv.DictReader(handle)], [("1", "0", "")]
                )

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
