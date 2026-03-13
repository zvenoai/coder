CREATE TABLE IF NOT EXISTS epic_states (
    epic_key TEXT PRIMARY KEY,
    epic_summary TEXT NOT NULL,
    phase TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS epic_children (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    epic_key TEXT NOT NULL,
    child_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on TEXT NOT NULL DEFAULT '[]',
    tracker_status TEXT NOT NULL DEFAULT '',
    last_comment_id INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '[]',
    UNIQUE(epic_key, child_key),
    FOREIGN KEY(epic_key) REFERENCES epic_states(epic_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_epic_children_epic ON epic_children(epic_key);
