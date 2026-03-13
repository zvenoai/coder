CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    task_key TEXT NOT NULL,
    data TEXT NOT NULL,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task_key ON events(task_key);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
