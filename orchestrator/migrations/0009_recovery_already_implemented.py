"""Idempotent ALTER TABLE: add already_implemented to recovery_states."""

from __future__ import annotations

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add already_implemented column to recovery_states if it doesn't exist."""
    cursor = await db.execute("PRAGMA table_info(recovery_states)")
    rows = await cursor.fetchall()
    existing_columns = {row[1] for row in rows}

    if "already_implemented" not in existing_columns:
        await db.execute("ALTER TABLE recovery_states ADD COLUMN already_implemented INTEGER NOT NULL DEFAULT 0")
