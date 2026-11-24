"""Collect @instafoodielove analytics and benchmarks, write the daily drafts and publish due ones (see publish.py)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notifications import command_centre_link, notify
from octopus_energy.cli import load_env

from . import (
    analysis,
    clips,
    cloudflare_client,
    drafts,
    following,
    learnings,
    post_report,
    preflight,
    publish,
    recipe_series,
    render,
    storage,
    story_insights,
    token_refresh,
    uploads,
)
from .analysis import LONDON
from .cloudflare_client import CloudflareAI
from .graph import GRAPH_HOST, GRAPH_VERSION, INSTAGRAM_HOST, REEL_METRICS, GraphAPIError, GraphClient
from .openai_client import IMAGE_MODEL, IMAGE_SIZE, TEXT_MODEL, AIError, OpenAIClient

DEFAULT_MEDIA = Path("data/instagram/drafts")
DAILY_FROM_HOUR = 9
DAILY_ATTEMPTS = 3
DAILY_DRAFTS = 1

INSIGHT_WINDOW = timedelta(days=30)


def graph_client(env: dict, *, timeout: float = 20.0, retries: int = 3) -> GraphClient:
    """Instagram Login tokens (IG...) go to graph.instagram.com, Facebook Login tokens to graph.facebook.com."""
    token = env["INSTAGRAM_ACCESS_TOKEN"]
    return GraphClient(
        token,
        env["INSTAGRAM_USER_ID"],
        version=env.get("META_GRAPH_VERSION", GRAPH_VERSION),
        host=INSTAGRAM_HOST if token.startswith("IG") else GRAPH_HOST,
        timeout=timeout,
        retries=retries,
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
                    values = _insights(client, media)
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
        "learnings": learnings.summary(conn),
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
    """`make_drafts`, then tell the owner when a day's drafts are ready or a run went wrong."""
    now = now or _now()
    outcome = make_drafts(conn, env, media_dir, now)
    if outcome != "skipped":
        _notify_daily(conn, env, outcome, now)
    return outcome


def _short(note: str, limit: int = 60) -> str:
    return note if len(note) <= limit else note[: limit - 1].rstrip() + "…"


def _posting(env: dict, rows) -> str:
    """When the first of these drafts posts, for the notification title."""
    times = [
        publish.posts_at(r["scheduled_for"], r["created_at"])
        for r in rows
        if r["scheduled_for"] and publish.will_post(r["status"], r["review_score"], publish.held(r))
    ]
    if not publish.configured(env):
        return "posting is manual"
    if times:
        return ("posts at " if len(rows) == 1 else "first posts at ") + min(times).astimezone(LONDON).strftime("%H:%M")
    return "it won't post unless you approve it" if len(rows) == 1 else "none will post unless you approve one"


def _insights(client: GraphClient, media: dict) -> dict[str, int]:
    """A post's metrics; reels add watch time, falling back to the common set if Meta refuses those."""
    if media.get("media_product_type") == "REELS":
        try:
            values = client.media_insights(media["id"], REEL_METRICS)
        except GraphAPIError:
            return client.media_insights(media["id"])
        values["avg_watch_ms"] = values.pop("ig_reels_avg_watch_time", None)
        values["total_watch_ms"] = values.pop("ig_reels_video_view_total_time", None)
        return values
    return client.media_insights(media["id"])


def _notify_render(env: dict, now: datetime):
    """Tell the owner a reel didn't render; after the last try it is held rather than posted unedited."""
    page = command_centre_link(env, "instagram")

    def failed(row, error: str, final: bool) -> None:
        then = "it won't post; delete it or send the media again" if final else "it will try again on the next run"
        title = f"Reel for {_short(row['topic'])} didn't render ({error}): {then}"
        key = f"instagram-render:{row['draft_id']}:{'final' if final else 'retry'}"
        notify("instagram", "failed", title, page, key=key, env=env, now=now)

    return failed


def _notify_daily(conn, env: dict, outcome: str, now: datetime) -> None:
    today = now.astimezone(LONDON).date().isoformat()
    page = command_centre_link(env, "instagram")
    if outcome in ("ok", "partial"):
        with conn:
            uploads.add_columns(conn)  # a clip can fill the day before uploads.oldest adds them
        for row in conn.execute(
            "SELECT u.upload_id, u.note, u.reel AS as_reel, d.kind, d.reel, d.status, d.review_score, d.scheduled_for, d.created_at "
            "FROM uploads u JOIN drafts d ON d.draft_id=u.draft_id WHERE d.created_on=?",
            (today,),
        ).fetchall():
            if row["kind"] == "reel":
                title = f'Your photos ("{_short(row["note"])}") are now a reel draft, {_posting(env, [row])}'
            else:
                unrendered = " (this server can't render reels yet)" if row["as_reel"] else ""
                title = f'Your photos ("{_short(row["note"])}") are now an Instagram draft{unrendered}, '
                title += _posting(env, [row])
            notify("instagram", "ok", title, page, key=f"instagram-upload-draft:{row['upload_id']}", env=env, now=now)
        for row in conn.execute(
            "SELECT c.clip_id, c.note, d.kind, d.reel, d.status, d.review_score, d.scheduled_for, d.created_at "
            "FROM clips c "
            "JOIN drafts d ON d.draft_id=c.draft_id WHERE d.created_on=?",
            (today,),
        ).fetchall():
            title = f'Your clip ("{_short(row["note"])}") is now a reel draft, {_posting(env, [row])}'
            notify("instagram", "ok", title, page, key=f"instagram-clip-draft:{row['clip_id']}", env=env, now=now)
    if outcome == "ok":
        rows = conn.execute(
            "SELECT status, review_score, scheduled_for, created_at FROM drafts WHERE created_on=? AND draft_id NOT "
            "IN (SELECT draft_id FROM uploads WHERE draft_id IS NOT NULL UNION "
            "SELECT draft_id FROM clips WHERE draft_id IS NOT NULL)",
            (today,),
        ).fetchall()
        if rows:
            title = f"{len(rows)} Instagram draft{'' if len(rows) == 1 else 's'} ready to review, {_posting(env, rows)}"
            notify("instagram", "ok", title, page, key=f"instagram-drafts-ready:{today}", env=env, now=now)
        return
    if outcome == "gave_up":
        title = "Instagram drafts gave up for today after repeated failures"
        notify("instagram", "failed", title, page, key=f"instagram-drafts-gave-up:{today}", env=env, now=now)
        return
    if outcome == "not_configured":
        title = "Instagram drafts not made: no AI provider key is set"
        notify("instagram", "failed", title, page, key=f"instagram-drafts-unconfigured:{today}", env=env, now=now)
        return
    run = conn.execute("SELECT error FROM daily_runs WHERE run_on=? ORDER BY id DESC LIMIT 1", (today,)).fetchone()
    error = run["error"] if run and run["error"] else "no reason recorded"
    notify("instagram", "failed", f"Instagram drafts {outcome}: {error}", page, env=env, now=now)


def _queued_clip(conn, media_dir, **filter_args):
    clip = clips.oldest(conn, **filter_args)
    while clip is not None and not clips.clip_file(media_dir, clip).is_file():
        clips.delete(conn, media_dir, clip["clip_id"])
        clip = clips.oldest(conn, **filter_args)
    return clip


def make_drafts(conn, env: dict, media_dir: Path, now: datetime | None = None) -> str:
    """One cron tick: from 09:00 London make today's draft(s), topping up to DAILY_DRAFTS until 12:00.

    A day stops after DAILY_ATTEMPTS failed or partial tries.
    """
    now = now or _now()
    local = now.astimezone(LONDON)
    today, stamp = local.date().isoformat(), now.isoformat(timespec="seconds")
    have = storage.drafts_on(conn, today)
    if local.hour < DAILY_FROM_HOUR or have >= DAILY_DRAFTS or (have and local.time() >= drafts.READY_BY):
        return "skipped"
    series = recipe_series.active(conn, now)
    slot_args = {}
    if series:
        if not recipe_series.posting_day(env, now) or not (slot := drafts.recipe_slot(conn, now)):
            return "skipped"
        slot_args = {"slot": slot}
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
    wanted, stored = DAILY_DRAFTS - have, 0
    try:
        rules = drafts.load_rules(Path(skills).expanduser() if skills else None)
        voice = _read(env.get("INSTAGRAM_VOICE_FILE", "agents/instagram-voice.md"))
        clip = _queued_clip(conn, media_dir, note_matches=recipe_series.is_recipe) if series else None
        recipe_upload = uploads.oldest(conn, note_matches=recipe_series.is_recipe) if series and clip is None else None
        if clip is None and recipe_upload is None:
            clip = _queued_clip(conn, media_dir)
        if clip is not None:
            rendered = render.can_render("clip")
            clip_rules = rules
            if series and recipe_series.is_recipe(clip["note"]):
                clip_rules = f"{recipe_series.GUIDANCE}\n{rules}"
            draft = drafts.generate_from_clip(
                conn, ai, now, clip, rules=clip_rules, voice=voice, rendered=rendered, media_dir=media_dir
            )
            if draft is None:
                raise AIError("the agents wrote no usable reel from your clip")
            stored += drafts.store_clip_draft(conn, draft, clip, media_dir, now, **slot_args) is not None
        if stored < wanted and not render.can_render("photos"):
            raise AIError("photo reel rendering unavailable; queued photos preserved")
        upload = (recipe_upload or uploads.oldest(conn)) if stored < wanted else None
        if upload is not None:
            upload_rules = rules
            if series and recipe_series.is_recipe(upload["note"]):
                upload_rules = f"{recipe_series.GUIDANCE}\n{rules}"
            draft = drafts.generate_from_upload(conn, ai, now, upload, media_dir, rules=upload_rules, voice=voice)
            if draft is None:
                raise AIError("the agents wrote no usable post from your photos")
            stored += drafts.store_upload_draft(conn, draft, upload, media_dir, now, **slot_args) is not None
        dropped: list[str] = []
        if stored < wanted:
            if series:
                rules = f"Draft only a quick home recipe. Do not invent the owner's experience.\n{recipe_series.GUIDANCE}\n{rules}"
            found = drafts.generate(conn, ai, now, count=wanted - stored, rules=rules, voice=voice)
            stored += drafts.render_and_store(conn, ai, found, media_dir, now, dropped, **slot_args)
    except (AIError, OSError) as exc:
        error = str(exc) if isinstance(exc, AIError) else type(exc).__name__
        with conn:
            storage.record_run(conn, today, stamp, "partial" if stored else "failed", stored, error=error[:500])
        return "partial" if stored else "failed"
    outcome = "ok" if stored >= wanted else "partial" if stored else "failed"
    error = None
    if stored < wanted:
        error = f"{wanted - stored} of {wanted} drafts not made" + (": " + "; ".join(dropped) if dropped else "")
    with conn:
        storage.record_run(conn, today, stamp, outcome, stored, error[:500] if error else None)
    return outcome


def _capture_followers(conn, env: dict, now: datetime) -> str:
    if not (env.get("INSTAGRAM_ACCESS_TOKEN") and env.get("INSTAGRAM_USER_ID")):
        return "follower snapshot off"
    day = now.astimezone(LONDON).date().isoformat()
    try:
        if conn.execute("SELECT 1 FROM account_followers WHERE captured_on=?", (day,)).fetchone():
            return "follower snapshot current"
        client = graph_client(env)
        count = client.get(client.user_id, {"fields": "followers_count"}).get("followers_count")
        if type(count) is not int or count < 0:
            return "follower snapshot unavailable"
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO account_followers(captured_on,captured_at,followers) VALUES(?,?,?)",
                (day, now.isoformat(timespec="seconds"), count),
            )
        return "follower snapshot captured"
    except (GraphAPIError, sqlite3.Error) as exc:
        return f"follower snapshot failed ({type(exc).__name__})"


def _capture_stories(conn, env: dict) -> str:
    if not (env.get("INSTAGRAM_ACCESS_TOKEN") and env.get("INSTAGRAM_USER_ID")):
        return "Story insights off"
    try:
        return f"Story insights {story_insights.capture(conn, graph_client(env, timeout=5, retries=0))}"
    except Exception as exc:  # noqa: BLE001
        return f"Story insights failed ({type(exc).__name__})"


def _post_reports(conn, env: dict) -> str:
    """24h/72h post reports after publishing; a problem here never fails the tick."""
    try:
        return post_report.tick(conn, env, _now())
    except Exception as exc:  # noqa: BLE001
        return f"reports failed ({type(exc).__name__})"


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
    check = commands.add_parser("preflight", help="Read-only account and draft checks for one controlled first post")
    check.add_argument("--draft", required=True, help="Draft ID to check")
    check.add_argument("--media", type=Path, default=DEFAULT_MEDIA)
    tick = commands.add_parser("daily", help="Cron tick: make today's drafts once, from 09:00 London")
    tick.add_argument("--media", type=Path, default=DEFAULT_MEDIA)
    args = parser.parse_args(argv)

    if args.command == "preflight":
        env = {**load_env(Path(args.env_file)), **os.environ}
        try:
            with closing(storage.connect(args.database, read_only=True)) as conn:
                conn.execute("BEGIN")
                result = preflight.run(conn, env, args.media, _now(), draft_id=args.draft)
        except sqlite3.Error:
            print("Preflight requires an existing, initialized Instagram database.", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1

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
        env_file = Path(args.env_file)
        if args.command == "daily":
            args.media.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(args.media / ".daily.lock", "w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 0  # The previous tick is still drafting.
                env = {**load_env(env_file), **os.environ}
                refreshed = token_refresh.tick(conn, env, env_file, _now())
                if refreshed in ("refreshed", "failed", "metadata_failed", "token_changed"):
                    env = {**load_env(env_file), **os.environ}
                if publish.configured(env):
                    recipe_series.start(conn, _now())
                outcome = daily(conn, env, args.media)
                render.render_due(conn, args.media, _notify_render(env, _now()))
                published = publish.tick(conn, env, args.media, _now())
                reports = _post_reports(conn, env)
                followers = _capture_followers(conn, env, _now())
            with open(args.media / ".story-insights.lock", "w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stories = "Story insights busy"
                else:
                    stories = _capture_stories(conn, env)
            print(outcome, refreshed, published, stories, reports, followers, sep="; ")
            return 0 if outcome in ("ok", "partial", "skipped", "gave_up") else 1
        env = {**load_env(env_file), **os.environ}
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
