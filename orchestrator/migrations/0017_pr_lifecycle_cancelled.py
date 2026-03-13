import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(pr_lifecycle)")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}
    if "cancelled_at" not in existing:
        await db.execute("ALTER TABLE pr_lifecycle ADD COLUMN cancelled_at REAL")
