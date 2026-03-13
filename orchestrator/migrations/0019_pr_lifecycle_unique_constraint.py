"""Add UNIQUE(task_key, pr_url) to pr_lifecycle.

Merges duplicate rows first: for each (task_key, pr_url) group
computes aggregated values (MAX for timestamps, SUM for
counters), picks the "best" row (prefer merged, then latest),
updates it with aggregated values, then deletes the rest.

Mutual exclusion: merged_at wins over cancelled_at.
"""

import aiosqlite


async def migrate(db: aiosqlite.Connection) -> None:
    """Deduplicate pr_lifecycle and add UNIQUE index."""
    # Idempotency: skip if index already exists
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_pr_lifecycle_task_pr'",
    )
    if await cursor.fetchone():
        return

    # Step 1: Find groups with duplicates and compute
    # aggregated values.
    cursor = await db.execute(
        """SELECT task_key, pr_url,
               MAX(merged_at) AS agg_merged,
               MAX(verified_at) AS agg_verified,
               MAX(cancelled_at) AS agg_cancelled,
               SUM(review_iterations) AS agg_iters,
               SUM(ci_failures) AS agg_fails
           FROM pr_lifecycle
           GROUP BY task_key, pr_url
           HAVING COUNT(*) > 1""",
    )
    groups = await cursor.fetchall()

    for grp in groups:
        tk = grp[0]
        pu = grp[1]
        agg_merged = grp[2]
        agg_verified = grp[3]
        agg_cancelled = grp[4]
        agg_iters = grp[5] or 0
        agg_fails = grp[6] or 0

        # merged wins over cancelled
        if agg_merged is not None:
            agg_cancelled = None

        # Pick the "best" row id
        cur2 = await db.execute(
            """SELECT id FROM pr_lifecycle
               WHERE task_key = ? AND pr_url = ?
               ORDER BY
                   CASE WHEN merged_at IS NOT NULL
                        THEN 0 ELSE 1 END,
                   tracked_at DESC,
                   id DESC
               LIMIT 1""",
            (tk, pu),
        )
        best = await cur2.fetchone()
        if best is None:
            continue
        best_id = best[0]

        # Update best row with aggregated values
        await db.execute(
            """UPDATE pr_lifecycle SET
                   merged_at = ?,
                   verified_at = ?,
                   cancelled_at = ?,
                   review_iterations = ?,
                   ci_failures = ?
               WHERE id = ?""",
            (
                agg_merged,
                agg_verified,
                agg_cancelled,
                agg_iters,
                agg_fails,
                best_id,
            ),
        )

        # Delete other rows
        await db.execute(
            """DELETE FROM pr_lifecycle
               WHERE task_key = ?
                 AND pr_url = ?
                 AND id != ?""",
            (tk, pu, best_id),
        )

    # Step 2: Add UNIQUE constraint
    await db.execute(
        "CREATE UNIQUE INDEX uq_pr_lifecycle_task_pr ON pr_lifecycle(task_key, pr_url)",
    )
