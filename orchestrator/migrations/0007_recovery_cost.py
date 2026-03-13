"""Idempotent ALTER TABLE: add no_pr_cost to recovery_states."""

from __future__ import annotations

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add no_pr_cost column to recovery_states table if it doesn't exist."""
    cursor = await db.execute("PRAGMA table_info(recovery_states)")
    rows = await cursor.fetchall()
    existing_columns = {row[1] for row in rows}

    if "no_pr_cost" not in existing_columns:
        await db.execute("ALTER TABLE recovery_states ADD COLUMN no_pr_cost REAL NOT NULL DEFAULT 0.0")
