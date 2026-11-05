import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


def initialize(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def record_run(database_path: Path, *, report_date: str, started_at: str,
               ended_at: str, duration_ms: int, calendar_events: int,
               tasks: int, errors: list[str]) -> None:
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """INSERT INTO execution_history
            (report_date, started_at, ended_at, duration_ms, calendar_events, tasks_processed, error_count, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (report_date, started_at, ended_at, duration_ms, calendar_events,
             tasks, len(errors), " | ".join(errors)),
        )
