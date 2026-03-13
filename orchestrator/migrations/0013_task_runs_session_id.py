"""Add session_id column to task_runs table."""

from __future__ import annotations

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add session_id column to task_runs if not present."""
    cursor = await db.execute("PRAGMA table_info(task_runs)")
    rows = await cursor.fetchall()
    existing_columns = {row[1] for row in rows}

    if "session_id" not in existing_columns:
        await db.execute("ALTER TABLE task_runs ADD COLUMN session_id TEXT")
