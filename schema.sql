CREATE TABLE IF NOT EXISTS execution_history (
  id INTEGER PRIMARY KEY,
  report_date TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  calendar_events INTEGER NOT NULL,
  tasks_processed INTEGER NOT NULL,
  error_count INTEGER NOT NULL,
  errors TEXT NOT NULL
);
