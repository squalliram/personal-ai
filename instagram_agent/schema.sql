CREATE TABLE IF NOT EXISTS posts (
    media_id TEXT PRIMARY KEY,
    posted_at TEXT NOT NULL,
    media_type TEXT,
    permalink TEXT,
    caption TEXT,
    likes INTEGER,
    comments INTEGER,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_metrics (
    media_id TEXT NOT NULL REFERENCES posts(media_id),
    captured_on TEXT NOT NULL,
    reach INTEGER,
    likes INTEGER,
    comments INTEGER,
    saved INTEGER,
    shares INTEGER,
    views INTEGER,
    total_interactions INTEGER,
    PRIMARY KEY (media_id, captured_on)
);
CREATE TABLE IF NOT EXISTS audience_hours (
    hour INTEGER PRIMARY KEY CHECK (hour BETWEEN 0 AND 23),
    followers_online INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmark_accounts (
    username TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    followers INTEGER,
    media_count INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    synced_at TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_posts (
    media_id TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES benchmark_accounts(username),
    posted_at TEXT,
    media_type TEXT,
    permalink TEXT,
    caption TEXT,
    likes INTEGER,
    comments INTEGER,
    views INTEGER,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS examples (
    draft_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_at TEXT NOT NULL,
    topic TEXT,
    caption TEXT,
    hashtags TEXT,
    media_id TEXT REFERENCES posts(media_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    created_on TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'published', 'failed')),
    category TEXT NOT NULL,
    topic TEXT NOT NULL,
    angle TEXT,
    slides TEXT NOT NULL,
    caption TEXT NOT NULL,
    hashtags TEXT NOT NULL,
    review_score INTEGER,
    review_notes TEXT,
    scheduled_for TEXT,
    decided_at TEXT,
    media_id TEXT
);
CREATE INDEX IF NOT EXISTS drafts_created_on ON drafts(created_on);
CREATE TABLE IF NOT EXISTS daily_runs (
    id INTEGER PRIMARY KEY,
    run_on TEXT NOT NULL,
    started_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    drafts INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
