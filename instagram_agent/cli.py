"""Collect @instafoodielove analytics and benchmarks, write the daily drafts and publish due ones (see publish.py)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from octopus_energy.cli import load_env

from . import analysis, cloudflare_client, drafts, following, publish, storage
from .analysis import LONDON
from .cloudflare_client import CloudflareAI
from .graph import GRAPH_HOST, GRAPH_VERSION, INSTAGRAM_HOST, GraphAPIError, GraphClient
from .openai_client import IMAGE_MODEL, IMAGE_SIZE, TEXT_MODEL, AIError, OpenAIClient

DEFAULT_MEDIA = Path("data/instagram/drafts")
DAILY_FROM_HOUR = 9
DAILY_ATTEMPTS = 3
DAILY_DRAFTS = 3

INSIGHT_WINDOW = timedelta(days=30)


def graph_client(env: dict) -> GraphClient:
    """Instagram Login tokens (IG...) go to graph.instagram.com, Facebook Login tokens to graph.facebook.com."""
    token = env["INSTAGRAM_ACCESS_TOKEN"]
    return GraphClient(
        token,
        env["INSTAGRAM_USER_ID"],
        version=env.get("META_GRAPH_VERSION", GRAPH_VERSION),
        host=INSTAGRAM_HOST if token.startswith("IG") else GRAPH_HOST,
    )


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
        storage.link_published_drafts(conn)
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


def _read(path: str | None) -> str:
    if not path:
        return ""
    file = Path(path).expanduser()
    return file.read_text(encoding="utf-8") if file.is_file() and not file.is_symlink() else ""


def model_client(env: dict):
    """Cloudflare Workers AI (free daily allocation) or OpenAI, per INSTAGRAM_AI_PROVIDER or whichever is set."""
    cloudflare = bool(env.get("CLOUDFLARE_ACCOUNT_ID") and env.get("CLOUDFLARE_API_TOKEN"))
    provider = env.get("INSTAGRAM_AI_PROVIDER") or ("cloudflare" if cloudflare else "openai")
    if provider == "cloudflare" and env.get("CLOUDFLARE_ACCOUNT_ID") and env.get("CLOUDFLARE_API_TOKEN"):
        return CloudflareAI(
            env["CLOUDFLARE_ACCOUNT_ID"],
            env["CLOUDFLARE_API_TOKEN"],
            text_model=env.get("CLOUDFLARE_TEXT_MODEL") or cloudflare_client.TEXT_MODEL,
            image_model=env.get("CLOUDFLARE_IMAGE_MODEL") or cloudflare_client.IMAGE_MODEL,
            image_size=env.get("CLOUDFLARE_IMAGE_SIZE") or cloudflare_client.IMAGE_SIZE,
        )
    if provider == "openai" and env.get("OPENAI_API_KEY"):
        return OpenAIClient(
            env["OPENAI_API_KEY"],
            text_model=env.get("OPENAI_TEXT_MODEL") or TEXT_MODEL,
            image_model=env.get("OPENAI_IMAGE_MODEL") or IMAGE_MODEL,
            image_size=env.get("OPENAI_IMAGE_SIZE") or IMAGE_SIZE,
        )
    return None


def daily(conn, env: dict, media_dir: Path, now: datetime | None = None) -> str:
    """One cron tick: from 09:00 London make today's drafts, topping up to DAILY_DRAFTS until 12:00.

    A day stops after DAILY_ATTEMPTS failed or partial tries.
    """
    now = now or _now()
    local = now.astimezone(LONDON)
    today, stamp = local.date().isoformat(), now.isoformat(timespec="seconds")
    have = storage.drafts_on(conn, today)
    if local.hour < DAILY_FROM_HOUR or have >= DAILY_DRAFTS or (have and local.time() >= drafts.READY_BY):
        return "skipped"
    if storage.runs_on(conn, today) >= DAILY_ATTEMPTS:
        return "gave_up"
    ai = model_client(env)
    if ai is None:
        with conn:
            storage.record_run(conn, today, stamp, "not_configured")
        return "not_configured"
    if env.get("INSTAGRAM_ACCESS_TOKEN") and env.get("INSTAGRAM_USER_ID"):
        graph = graph_client(env)
        try:
            sync(conn, graph, benchmarks=int(env.get("INSTAGRAM_BENCHMARKS_PER_RUN", "50")), now=now)
        except GraphAPIError:
            pass  # Draft from yesterday's data rather than miss noon.
    skills = env.get("INSTAGRAM_SKILLS_DIR")
    try:
        found = drafts.generate(
            conn,
            ai,
            now,
            count=DAILY_DRAFTS - have,
            rules=drafts.load_rules(Path(skills).expanduser() if skills else None),
            voice=_read(env.get("INSTAGRAM_VOICE_FILE", "agents/instagram-voice.md")),
        )
        dropped: list[str] = []
        stored = drafts.render_and_store(conn, ai, found, media_dir, now, dropped)
    except (AIError, OSError) as exc:
        error = str(exc) if isinstance(exc, AIError) else type(exc).__name__
        with conn:
            storage.record_run(conn, today, stamp, "failed", error=error)
        return "failed"
    wanted = DAILY_DRAFTS - have
    outcome = "ok" if stored >= wanted else "partial" if stored else "failed"
    error = None
    if stored < wanted:
        error = f"{wanted - stored} of {wanted} drafts not made" + (": " + "; ".join(dropped) if dropped else "")
    with conn:
        storage.record_run(conn, today, stamp, outcome, stored, error[:500] if error else None)
    return outcome


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
    tick = commands.add_parser("daily", help="Cron tick: make today's drafts once, from 09:00 London")
    tick.add_argument("--media", type=Path, default=DEFAULT_MEDIA)
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
        if args.command == "daily":
            args.media.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(args.media / ".daily.lock", "w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 0  # The previous tick is still drafting.
                outcome = daily(conn, env, args.media)
                published = publish.tick(conn, env, args.media, _now())
            print(outcome, published, sep="; ")
            return 0 if outcome in ("ok", "partial", "skipped", "gave_up") else 1
        token, user_id = env.get("INSTAGRAM_ACCESS_TOKEN", ""), env.get("INSTAGRAM_USER_ID", "")
        if not token or not user_id:
            print("Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID.", file=sys.stderr)
            return 2
        client = graph_client(env)
        try:
            counts = sync(conn, client, benchmarks=args.benchmarks)
        except GraphAPIError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(counts))
        return 0
    finally:
        conn.close()
