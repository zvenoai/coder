"""Idempotent ALTER TABLE: add description, component, tracker_issue_key to proposals."""

from __future__ import annotations

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add columns to proposals table if they don't exist."""
    cursor = await db.execute("PRAGMA table_info(proposals)")
    rows = await cursor.fetchall()
    existing_columns = {row[1] for row in rows}

    if "description" not in existing_columns:
        await db.execute("ALTER TABLE proposals ADD COLUMN description TEXT DEFAULT ''")
    if "component" not in existing_columns:
        await db.execute("ALTER TABLE proposals ADD COLUMN component TEXT DEFAULT ''")
    if "tracker_issue_key" not in existing_columns:
        await db.execute("ALTER TABLE proposals ADD COLUMN tracker_issue_key TEXT")
