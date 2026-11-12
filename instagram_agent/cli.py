"""Collect @instafoodielove analytics and benchmark data. Reads from Instagram only; never posts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from octopus_energy.cli import load_env

from . import analysis, following, storage
from .analysis import LONDON
from .graph import GRAPH_VERSION, GraphAPIError, GraphClient

INSIGHT_WINDOW = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sync(conn, client: GraphClient, *, benchmarks: int = 50, now: datetime | None = None) -> dict[str, int]:
    now = now or _now()
    stamp = now.isoformat().replace("+00:00", "Z")
    today = now.astimezone(LONDON).date().isoformat()
    counts = {
        "posts": 0,
        "metrics": 0,
        "metric_errors": 0,
        "audience_errors": 0,
        "benchmarks": 0,
        "benchmark_errors": 0,
    }
    with conn:
        for media in client.own_media():
            storage.upsert_post(conn, media, stamp)
            counts["posts"] += 1
            if analysis._instant(media["timestamp"]) >= now - INSIGHT_WINDOW:
                try:
                    values = client.media_insights(media["id"])
                except GraphAPIError:
                    counts["metric_errors"] += 1
                    continue
                storage.upsert_metrics(conn, media["id"], today, values)
                counts["metrics"] += 1
        try:
            storage.replace_audience_hours(conn, client.online_followers(), stamp)
        except GraphAPIError:
            # Accounts under 100 followers have no audience insights; keep the rest of the run.
            counts["audience_errors"] += 1
    due = conn.execute(
        "SELECT username FROM benchmark_accounts ORDER BY synced_at IS NOT NULL, synced_at LIMIT ?", (benchmarks,)
    ).fetchall()
    for (username,) in due:
        try:
            found = client.business_discovery(username)
        except (GraphAPIError, ValueError):
            # A failed refresh keeps the last good data; only a never-synced account becomes unavailable.
            counts["benchmark_errors"] += 1
            with conn:
                storage.mark_benchmark_failed(conn, username, stamp)
            continue
        with conn:
            storage.update_benchmark(conn, username, found, stamp)
        counts["benchmarks"] += 1
    return counts


def report(conn) -> dict:
    return {
        "benchmarks": analysis.benchmark_rankings(conn)[:20],
        "top_benchmark_posts": analysis.top_benchmark_posts(conn),
        "trending_hashtags": analysis.trending_hashtags(conn),
        "own_posts": analysis.post_performance(conn)[:30],
        "best_hours_london": analysis.best_hours(conn),
        "examples": analysis.learned_examples(conn),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--database", type=Path, default=storage.DEFAULT_DATABASE)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("sync", help="Fetch own posts, insights, audience hours and benchmark accounts")
    collect.add_argument("--benchmarks", type=int, default=50, help="Benchmark accounts to refresh this run")
    imported = commands.add_parser("import-following", help="Add accounts from Instagram's following.json export")
    imported.add_argument("path", type=Path)
    commands.add_parser("report", help="Print rankings, hashtags, best hours and learned examples as JSON")
    args = parser.parse_args(argv)

    conn = storage.connect(args.database)
    try:
        if args.command == "import-following":
            names = following.usernames(args.path)
            with conn:
                added = storage.add_benchmarks(conn, names, "following")
            print(f"{len(names)} accounts read, {added} new benchmark accounts added.")
            return 0
        if args.command == "report":
            print(json.dumps(report(conn), indent=2, ensure_ascii=False))
            return 0
        env = {**load_env(Path(args.env_file)), **os.environ}
        token, user_id = env.get("INSTAGRAM_ACCESS_TOKEN", ""), env.get("INSTAGRAM_USER_ID", "")
        if not token or not user_id:
            print("Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID.", file=sys.stderr)
            return 2
        client = GraphClient(token, user_id, version=env.get("META_GRAPH_VERSION", GRAPH_VERSION))
        try:
            counts = sync(conn, client, benchmarks=args.benchmarks)
        except GraphAPIError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(counts))
        return 0
    finally:
        conn.close()
