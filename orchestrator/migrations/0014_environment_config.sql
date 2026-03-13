CREATE TABLE IF NOT EXISTS environment_config (
    name TEXT PRIMARY KEY,
    config TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
