CREATE TABLE IF NOT EXISTS needs_info_tracking (
    issue_key TEXT PRIMARY KEY,
    last_seen_comment_id INTEGER NOT NULL,
    issue_summary TEXT NOT NULL,
    tracked_at REAL NOT NULL
);
