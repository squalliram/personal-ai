import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .briefing import render
from .storage import initialize, record_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a read-only daily briefing from a connector snapshot.")
    parser.add_argument("--snapshot", required=True, help="Normalized JSON produced by an approved read-only connector bridge")
    parser.add_argument("--output-dir", default="reports/daily")
    parser.add_argument("--database", default="data/daily_briefing.sqlite3")
    args = parser.parse_args()
    start = time.monotonic()
    now = datetime.now(ZoneInfo("Europe/London"))
    errors = []
    try:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        events = snapshot.get("events", [])
        tasks = snapshot.get("tasks", [])
        errors = snapshot.get("errors", [])
    except (OSError, json.JSONDecodeError) as exc:
        events, tasks, errors = [], [], [f"Snapshot input failure: {type(exc).__name__}"]
    report = render(now.date(), now, events, tasks, errors)
    output = Path(args.output_dir) / f"{now.date().isoformat()}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    database = Path(args.database)
    initialize(database)
    record_run(database, report_date=now.date().isoformat(), started_at=now.isoformat(), ended_at=datetime.now(ZoneInfo("Europe/London")).isoformat(), duration_ms=int((time.monotonic()-start)*1000), calendar_events=len(events), tasks=len(tasks), errors=errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
