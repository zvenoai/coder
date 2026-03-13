"""Idempotent ALTER TABLE: add verified_at to pr_lifecycle."""

from __future__ import annotations

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Add verified_at column to pr_lifecycle if not present."""
    cursor = await db.execute("PRAGMA table_info(pr_lifecycle)")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}

    if "verified_at" not in existing:
        await db.execute("ALTER TABLE pr_lifecycle ADD COLUMN verified_at REAL")
