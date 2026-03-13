"""Add merge_conflict_head_sha column to pr_tracking table."""

from __future__ import annotations

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add merge_conflict_head_sha column if not present."""
    cursor = await db.execute("PRAGMA table_info(pr_tracking)")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}

    if "merge_conflict_head_sha" not in existing:
        await db.execute("ALTER TABLE pr_tracking ADD COLUMN merge_conflict_head_sha TEXT NOT NULL DEFAULT ''")
