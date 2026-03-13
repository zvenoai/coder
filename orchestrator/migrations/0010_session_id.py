"""Add session_id column to pr_tracking and needs_info_tracking tables."""

from __future__ import annotations

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add session_id column to tracking tables if not present."""
    for table in ("pr_tracking", "needs_info_tracking"):
        # table name is from hardcoded tuple, not user input
        cursor = await db.execute(f"PRAGMA table_info({table})")  # nosemgrep
        rows = await cursor.fetchall()
        existing_columns = {row[1] for row in rows}

        if "session_id" not in existing_columns:
            # table name is from hardcoded tuple, not user input
            await db.execute(f"ALTER TABLE {table} ADD COLUMN session_id TEXT")  # nosemgrep
