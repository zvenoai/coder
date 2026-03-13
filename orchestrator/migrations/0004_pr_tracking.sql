CREATE TABLE IF NOT EXISTS pr_tracking (
    task_key TEXT PRIMARY KEY,
    pr_url TEXT NOT NULL,
    issue_summary TEXT NOT NULL DEFAULT '',
    seen_thread_ids TEXT NOT NULL DEFAULT '[]',
    seen_failed_checks TEXT NOT NULL DEFAULT '[]'
);
