"""Add seen_merge_conflict column to pr_tracking table."""

from __future__ import annotations

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add seen_merge_conflict column to pr_tracking if not present."""
    cursor = await db.execute("PRAGMA table_info(pr_tracking)")
    rows = await cursor.fetchall()
    existing_columns = {row[1] for row in rows}

    if "seen_merge_conflict" not in existing_columns:
        await db.execute("ALTER TABLE pr_tracking ADD COLUMN seen_merge_conflict INTEGER NOT NULL DEFAULT 0")
