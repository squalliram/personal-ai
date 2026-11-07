import argparse
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .briefing import render
from .snapshot import load
from .storage import initialize, record_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a read-only daily briefing from a connector snapshot.")
    parser.add_argument(
        "--snapshot", required=True, help="Normalized JSON produced by an approved read-only connector bridge"
    )
    parser.add_argument("--output-dir", default="reports/daily")
    parser.add_argument("--database", default="data/daily_briefing.sqlite3")
    args = parser.parse_args()
    start = time.monotonic()
    now = datetime.now(ZoneInfo("Europe/London"))
    snapshot = load(args.snapshot)
    report = render(now.date(), now, snapshot.events, snapshot.tasks, snapshot.errors)
    output = Path(args.output_dir) / f"{now.date().isoformat()}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    database = Path(args.database)
    initialize(database)
    record_run(
        database,
        report_date=now.date().isoformat(),
        started_at=now.isoformat(),
        ended_at=datetime.now(ZoneInfo("Europe/London")).isoformat(),
        duration_ms=int((time.monotonic() - start) * 1000),
        calendar_events=len(snapshot.events),
        tasks=len(snapshot.tasks),
        errors=snapshot.errors,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
