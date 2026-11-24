CREATE TABLE IF NOT EXISTS posts (
    media_id TEXT PRIMARY KEY,
    posted_at TEXT NOT NULL,
    media_type TEXT,
    permalink TEXT,
    caption TEXT,
    likes INTEGER,
    comments INTEGER,
    synced_at TEXT NOT NULL,
    media_product_type TEXT
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
    avg_watch_ms INTEGER,
    total_watch_ms INTEGER,
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
CREATE TABLE IF NOT EXISTS content_plan (
    id INTEGER PRIMARY KEY,
    week TEXT NOT NULL,
    pillar TEXT NOT NULL,
    slot TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'Idea' CHECK (status IN ('Idea','Scripted','Filmed','Edited'))
);
CREATE TABLE IF NOT EXISTS content_weekly_reports (
    week TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    worst TEXT,
    notified_at TEXT
);
CREATE TABLE IF NOT EXISTS content_pillar_replacements (
    old_pillar TEXT PRIMARY KEY,
    new_pillar TEXT NOT NULL UNIQUE,
    effective_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    plan_id INTEGER REFERENCES content_plan(id),
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
    media_id TEXT,
    kind TEXT NOT NULL DEFAULT 'carousel' CHECK (kind IN ('carousel', 'reel')),
    reel TEXT
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
CREATE TABLE IF NOT EXISTS publish_runs (
    id INTEGER PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    started_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('started', 'published', 'failed')),
    media_id TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS publisher_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    token_fingerprint TEXT,
    expires_at TEXT,
    last_refreshed_at TEXT,
    last_attempt_at TEXT,
    last_outcome TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS account_followers (
    captured_on TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    followers INTEGER NOT NULL CHECK (followers >= 0)
);

CREATE TABLE IF NOT EXISTS story_insights (
    media_id TEXT PRIMARY KEY,
    draft_id TEXT,
    posted_at TEXT NOT NULL,
    permalink TEXT,
    captured_at TEXT,
    attempted_at TEXT,
    refresh_complete INTEGER NOT NULL DEFAULT 1,
    metrics TEXT NOT NULL DEFAULT '{}',
    earlier_metrics TEXT NOT NULL DEFAULT '{}',
    navigation_breakdown TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS post_reports (
    media_id TEXT NOT NULL,
    checkpoint TEXT NOT NULL CHECK (checkpoint IN ('24h', '72h')),
    draft_id TEXT,
    format TEXT,
    posted_at TEXT,
    permalink TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT,
    reported_at TEXT,
    reach INTEGER,
    likes INTEGER,
    comments INTEGER,
    saved INTEGER,
    shares INTEGER,
    views INTEGER,
    total_interactions INTEGER,
    avg_watch_ms INTEGER,
    total_watch_ms INTEGER,
    follows INTEGER,
    profile_visits INTEGER,
    replies INTEGER,
    navigation INTEGER,
    creative TEXT,
    summary TEXT,
    notified_at TEXT,
    PRIMARY KEY (media_id, checkpoint)
);
