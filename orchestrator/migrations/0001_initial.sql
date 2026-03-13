CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    duration_seconds REAL NOT NULL,
    success INTEGER NOT NULL,
    error_category TEXT,
    pr_url TEXT,
    needs_info INTEGER NOT NULL DEFAULT 0,
    resumed INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS supervisor_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_task_keys TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    duration_seconds REAL NOT NULL,
    success INTEGER NOT NULL,
    tasks_created TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    tracked_at REAL NOT NULL,
    merged_at REAL,
    review_iterations INTEGER NOT NULL DEFAULT 0,
    ci_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL,
    error_category TEXT NOT NULL,
    error_message TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_runs_finished ON task_runs(finished_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_key ON task_runs(task_key);
CREATE INDEX IF NOT EXISTS idx_error_log_timestamp ON error_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_task_key ON pr_lifecycle(task_key);
