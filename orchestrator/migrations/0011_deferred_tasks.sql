CREATE TABLE IF NOT EXISTS deferred_tasks (
    issue_key TEXT PRIMARY KEY,
    issue_summary TEXT NOT NULL,
    blockers TEXT NOT NULL DEFAULT '[]',
    deferred_at REAL NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0
);
