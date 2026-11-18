"""Read-only Instagram baseline export. Run from the repository root."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from instagram_agent.cli import graph_client  # noqa: E402
from instagram_agent.graph import MEDIA_FIELDS, GraphAPIError  # noqa: E402

LONDON = ZoneInfo("Europe/London")
COMMON_METRICS = ("reach", "views", "likes", "comments", "saved", "shares", "total_interactions")
REEL_METRICS = ("ig_reels_avg_watch_time", "ig_reels_video_view_total_time")
POST_COLUMNS = (
    "id",
    "date",
    "day",
    "hour",
    "type",
    "reach",
    "views",
    "likes",
    "comments",
    "saves",
    "shares",
    "interactions",
    "engagement_rate_reach",
    "engagement_rate_followers",
    "outlier_multiple",
    "shares_per_reach",
    "caption_80",
    "permalink",
)


def redact(value, token):
    if isinstance(value, dict):
        return {
            redact(str(k), token): "[REDACTED]"
            if re.search(r"token|authorization|secret", str(k), re.I)
            else redact(v, token)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, token) for v in value]
    if not isinstance(value, str):
        return value
    if token:
        for secret in {token, quote(token, safe=""), quote_plus(token)}:
            value = value.replace(secret, "[REDACTED]")

    def safe_url(match):
        url = match.group()
        try:
            parts = urlsplit(url)
        except ValueError:
            return "[REDACTED_URL]"
        if (
            parts.scheme == "https"
            and parts.netloc.lower() in ("www.instagram.com", "instagram.com")
            and re.fullmatch(r"/(?:p|reel|tv)/[A-Za-z0-9_-]+/?", parts.path)
            and not parts.query
            and not parts.fragment
        ):
            return url
        return "[REDACTED_URL]"

    value = re.sub(r"https?://[^\s<>\"']+", safe_url, value, flags=re.I)
    return re.sub(r"(?i)(access_token|authorization|bearer)\s*[:= ]\s*[^\s&,]+", r"\1=[REDACTED]", value)


def ratio(a, b):
    return a / b if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > 0 else None


def metric_value(response):
    entries = response.get("data") or []
    if not entries:
        return None
    entry = entries[0]
    points = entry.get("values") or []
    value = points[0].get("value") if points else (entry.get("total_value") or {}).get("value")
    return value if isinstance(value, (int, float)) else None


def summarize(media, insights, profile):
    posts = []
    for item in media:
        kind = (
            "reel"
            if item.get("media_product_type") == "REELS"
            else "carousel"
            if item.get("media_type") == "CAROUSEL_ALBUM"
            else "image"
            if item.get("media_type") == "IMAGE"
            else "video"
        )
        stamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).astimezone(LONDON)
        values = {name: metric_value(response) for name, response in insights.get(item["id"], {}).items()}
        likes = values.get("likes") if values.get("likes") is not None else item.get("like_count")
        comments = values.get("comments") if values.get("comments") is not None else item.get("comments_count")
        components = [likes, comments, values.get("saved"), values.get("shares")]
        interactions = values.get("total_interactions")
        if interactions is None and all(isinstance(v, (int, float)) for v in components):
            interactions = sum(components)
        posts.append(
            dict(
                zip(
                    POST_COLUMNS,
                    [
                        item["id"],
                        stamp.isoformat(),
                        stamp.strftime("%A"),
                        stamp.hour,
                        kind,
                        values.get("reach"),
                        values.get("views") if values.get("views") is not None else values.get("plays"),
                        likes,
                        comments,
                        values.get("saved"),
                        values.get("shares"),
                        interactions,
                        ratio(interactions, values.get("reach")),
                        ratio(interactions, profile.get("followers_count")),
                        None,
                        ratio(values.get("shares"), values.get("reach")),
                        (item.get("caption") or "")[:80],
                        item.get("permalink"),
                    ],
                    strict=True,
                )
            )
        )
    views = [p["views"] for p in posts if p["views"] is not None]
    median_views = statistics.median(views) if views else None
    for post in posts:
        post["outlier_multiple"] = ratio(post["views"], median_views)
    groups = {}
    metrics = (
        "reach",
        "views",
        "likes",
        "comments",
        "saves",
        "shares",
        "engagement_rate_reach",
        "engagement_rate_followers",
    )
    for kind in ("reel", "image", "carousel", "video"):
        subset = [p for p in posts if p["type"] == kind]
        groups[kind] = {"posts": len(subset), "medians": {}, "coverage": {}}
        for name in metrics:
            available = [p[name] for p in subset if p[name] is not None]
            groups[kind]["medians"][name] = statistics.median(available) if available else None
            groups[kind]["coverage"][name] = len(available)
    rankings = {}
    for metric in ("outlier_multiple", "shares_per_reach", "engagement_rate_reach"):
        ranked = sorted((p for p in posts if p[metric] is not None), key=lambda p: (p[metric], p["id"]))
        rankings[metric] = {"top": list(reversed(ranked[-3:])), "bottom": ranked[:3]}
    dates = sorted(datetime.fromisoformat(p["date"]) for p in posts)
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(dates, dates[1:], strict=False)]
    summary = {
        "post_count": len(posts),
        "median_views": median_views,
        "by_type": groups,
        "rankings": rankings,
        "cadence": {
            "timezone": "Europe/London",
            "by_day": dict(Counter(p["day"] for p in posts)),
            "by_hour": dict(Counter(str(p["hour"]) for p in posts)),
            "median_gap_days": statistics.median(gaps) if gaps else None,
            "first_post": dates[0].isoformat() if dates else None,
            "last_post": dates[-1].isoformat() if dates else None,
        },
        "limitations": [
            "Post metrics are lifetime snapshots with mixed ages, not 28-day post performance.",
            "Follower engagement uses current followers, not followers at posting; no follower trend is inferred.",
            "Ratios are fractions. Missing/zero denominators yield null, not zero engagement.",
            "API metric support and permissions vary; see raw/errors.json for unavailable metrics.",
            "No causal hook/topic/length conclusions or revenue estimates are inferred from these rankings.",
            "Public Instagram permalinks are preserved; other URLs and credentials are redacted.",
        ],
    }
    return posts, summary


def export(client, output, token, now=None):
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Output directory must be empty; choose a new output directory")
    now = now or datetime.now(timezone.utc)
    until = now.astimezone(LONDON).replace(hour=0, minute=0, second=0, microsecond=0)
    since = until - timedelta(days=28)
    raw = output / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    errors = []

    def save(name, payload):
        (raw / name).write_text(json.dumps(redact(payload, token), indent=2) + "\n", encoding="utf-8")

    def get(path, params, label):
        try:
            return client.get(path, params)
        except GraphAPIError:
            errors.append({"request": label, "status": "failed", "error": "Graph API request failed"})
            return None

    profile = get(client.user_id, {"fields": "id,username,followers_count,follows_count,media_count"}, "profile")
    save("profile.json", profile)
    if profile is None or profile.get("username", "").lower() != "instafoodielove":
        save("errors.json", errors + [{"request": "profile", "error": "Target account could not be verified"}])
        raise ValueError("Target account could not be verified")
    media, pages, seen = [], 0, set()
    path, params = f"{client.user_id}/media", {"fields": MEDIA_FIELDS, "limit": "100"}
    complete = True
    while path:
        page = get(path, params, f"media_page_{pages + 1}")
        if page is None:
            complete = False
            break
        pages += 1
        save(f"media_page_{pages:05d}.json", page)
        media.extend(page.get("data", []))
        path, params = (page.get("paging") or {}).get("next"), None
        if path in seen:
            errors.append({"request": "media_pagination", "error": "Repeated pagination page"})
            complete = False
            break
        seen.add(path)
    media = list({item["id"]: item for item in media}.values())
    insights = {}
    for item in media:
        metrics = COMMON_METRICS
        if item.get("media_product_type") == "REELS":
            metrics += REEL_METRICS
        if item.get("media_type") == "VIDEO":
            metrics += ("plays",)
        values = {}
        for metric in metrics:
            response = get(f"{item['id']}/insights", {"metric": metric}, f"media:{item['id']}:{metric}")
            if response is not None:
                values[metric] = response
                if metric_value(response) is None:
                    errors.append({"request": f"media:{item['id']}:{metric}", "status": "no_value"})
        insights[item["id"]] = values
        save(f"insights_{item['id']}.json", values)
    account = {}
    for label in (
        "reach",
        "views",
        "profile_views",
        "follows_and_unfollows",
        "follower_demographics:age",
        "follower_demographics:gender",
        "follower_demographics:country",
        "follower_demographics:city",
    ):
        metric = label.split(":")[0]
        params = {
            "metric": metric,
            "period": "day",
            "since": str(int(since.timestamp())),
            "until": str(int(until.timestamp())),
        }
        if metric in ("views", "follows_and_unfollows"):
            params["metric_type"] = "total_value"
        if metric == "follows_and_unfollows":
            params["breakdown"] = "follow_type"
        if metric == "follower_demographics":
            params = {
                "metric": metric,
                "period": "lifetime",
                "metric_type": "total_value",
                "timeframe": "last_30_days",
                "breakdown": label.split(":")[1],
            }
        response = get(f"{client.user_id}/insights", params, f"account:{label}")
        account[label] = {"parameters": params, "response": response}
        if response is not None and not response.get("data"):
            errors.append({"request": f"account:{label}", "status": "no_data"})
    save("account_insights.json", account)
    save("errors.json", errors)
    posts, summary = summarize(media, insights, profile)
    summary.update(
        {
            "captured_at": now.isoformat(),
            "account": profile,
            "media_history_complete": complete,
            "failed_or_empty_requests": len(errors),
            "account_window": {"since": since.isoformat(), "until_exclusive": until.isoformat()},
            "demographics_window": "API last_30_days (not a custom 28-day window)",
        }
    )
    (output / "summary.json").write_text(json.dumps(redact(summary, token), indent=2) + "\n", encoding="utf-8")
    with (output / "posts.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=POST_COLUMNS)
        writer.writeheader()
        writer.writerows(redact(posts, token))
    if not complete:
        raise ValueError("Incomplete media history; partial export saved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        export(graph_client(os.environ), args.output, os.environ.get("INSTAGRAM_ACCESS_TOKEN", ""))
    except FileExistsError:
        print("Output directory must be empty; choose a new output directory.", file=sys.stderr)
        return 1
    except Exception:
        print("Baseline export failed; no credentials or response details logged.", file=sys.stderr)
        return 1
    print("Baseline export complete; see summary.json and raw/errors.json for coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
