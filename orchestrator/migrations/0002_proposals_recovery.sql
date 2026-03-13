CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL UNIQUE,
    source_task_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);

CREATE TABLE IF NOT EXISTS recovery_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    no_pr_count INTEGER NOT NULL DEFAULT 0,
    provider_rate_limited INTEGER NOT NULL DEFAULT 0,
    last_output TEXT,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_issue_key ON recovery_states(issue_key);

CREATE TABLE IF NOT EXISTS recovery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key TEXT NOT NULL,
    timestamp REAL NOT NULL,
    category TEXT NOT NULL,
    error_message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_attempts_issue ON recovery_attempts(issue_key);
