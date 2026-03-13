"""SQLite-backed storage implementation (Storage Protocol)."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from orchestrator.stats_models import (
    DeferredTaskRecord,
    EpicChildRecord,
    EpicStateRecord,
    ErrorLogEntry,
    NeedsInfoTrackingRecord,
    ProposalRecord,
    PRTrackingData,
    RecoveryRecord,
    SupervisorRun,
    TaskCostSummary,
    TaskRun,
)

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_PATTERN = re.compile(r"^(\d{4})_.*\.(sql|py)$")

# Regex to detect non-SELECT statements (case-insensitive)
_WRITE_PATTERN = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|REINDEX|VACUUM)",
    re.IGNORECASE,
)

# Reject multiple statements (semicolon followed by non-whitespace)
_MULTI_STMT_PATTERN = re.compile(r";\s*\S")

# Safe read-only PRAGMAs that always accept arguments (e.g. table_info(name))
_SAFE_PRAGMAS_WITH_ARGS = frozenset(
    {
        "table_info",
        "table_list",
        "index_list",
        "integrity_check",
        "quick_check",
    }
)

# Safe read-only PRAGMAs — only when called WITHOUT arguments.
# With arguments they become write operations (e.g. PRAGMA journal_mode(WAL)).
_SAFE_PRAGMAS_NO_ARGS = frozenset(
    {
        "journal_mode",
        "page_count",
        "page_size",
        "database_list",
        "compile_options",
        "freelist_count",
    }
)

_SAFE_PRAGMAS = _SAFE_PRAGMAS_WITH_ARGS | _SAFE_PRAGMAS_NO_ARGS


class SQLiteStorage:
    """Async SQLite database for orchestrator statistics.

    Uses WAL mode for concurrent reads and inline schema migrations.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    def _require_db(self) -> aiosqlite.Connection:
        """Return the open database connection or raise if not open."""
        if self._db is None:
            raise RuntimeError("SQLiteStorage is not open. Call open() first.")
        return self._db

    async def open(self) -> None:
        """Open the database and apply migrations."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._migrate()
        logger.info("SQLiteStorage opened: %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("SQLiteStorage closed")

    async def _migrate(self) -> None:
        """Apply schema migrations using PRAGMA user_version and numbered files."""
        db = self._require_db()

        # Backward compat: migrate from old schema_version table to PRAGMA user_version
        current_version = await self._read_version(db)

        # Discover migration files
        migrations = self._discover_migrations()
        if not migrations:
            return

        # Apply pending migrations
        for version, path in migrations:
            if version <= current_version:
                continue
            if path.suffix == ".sql":
                sql = path.read_text(encoding="utf-8")
                try:
                    await db.executescript(sql)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" in str(exc):
                        logger.warning(
                            "Idempotent skip for %s: %s",
                            path.name,
                            exc,
                        )
                    else:
                        raise
            elif path.suffix == ".py":
                module_name = f"orchestrator.migrations.{path.stem}"
                # Migration modules are from controlled filesystem path, not user input
                mod = importlib.import_module(module_name)  # nosemgrep
                await mod.migrate(db)
            # PRAGMA user_version with integer from controlled source (filesystem enumeration)
            await db.execute(f"PRAGMA user_version = {version}")  # nosemgrep
            await db.commit()
            logger.info("Applied schema migration to version %d (%s)", version, path.name)

    @staticmethod
    async def _read_version(db: aiosqlite.Connection) -> int:
        """Read current schema version, handling legacy schema_version table."""
        # Check for legacy schema_version table
        try:
            cursor = await db.execute("SELECT version FROM schema_version LIMIT 1")
            row = await cursor.fetchone()
            if row:
                legacy_version = row[0]
                # Migrate to PRAGMA user_version and drop legacy table
                # PRAGMA user_version with integer from database query result, not user input
                await db.execute(f"PRAGMA user_version = {legacy_version}")  # nosemgrep
                await db.execute("DROP TABLE schema_version")
                await db.commit()
                logger.info("Migrated schema version %d from legacy table to PRAGMA user_version", legacy_version)
                return legacy_version
        except aiosqlite.OperationalError:
            pass

        # Read PRAGMA user_version (defaults to 0 for new databases)
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        return row[0] if row else 0

    @staticmethod
    def _discover_migrations() -> list[tuple[int, Path]]:
        """Scan migrations/ directory for numbered .sql and .py files."""
        if not _MIGRATIONS_DIR.is_dir():
            return []
        result: list[tuple[int, Path]] = []
        for path in sorted(_MIGRATIONS_DIR.iterdir()):
            match = _MIGRATION_PATTERN.match(path.name)
            if match:
                version = int(match.group(1))
                result.append((version, path))
        return result

    # ---- Insert methods ----

    async def record_task_run(self, run: TaskRun) -> None:
        """Record a completed or failed task run."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO task_runs
               (task_key, model, cost_usd, duration_seconds, success,
                error_category, pr_url, needs_info, resumed, started_at,
                finished_at, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.task_key,
                run.model,
                run.cost_usd,
                run.duration_seconds,
                int(run.success),
                run.error_category,
                run.pr_url,
                int(run.needs_info),
                int(run.resumed),
                run.started_at,
                run.finished_at,
                run.session_id,
            ),
        )
        await db.commit()

    async def record_supervisor_run(self, run: SupervisorRun) -> None:
        """Record a supervisor agent run."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO supervisor_runs
               (trigger_task_keys, cost_usd, duration_seconds, success, tasks_created, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                json.dumps(run.trigger_task_keys),
                run.cost_usd,
                run.duration_seconds,
                int(run.success),
                json.dumps(run.tasks_created),
                run.started_at,
                run.finished_at,
            ),
        )
        await db.commit()

    async def record_pr_tracked(
        self,
        task_key: str,
        pr_url: str,
        tracked_at: float,
    ) -> None:
        """Record that a PR has been created and is being tracked.

        Idempotent: if a row for the same (task_key, pr_url)
        already exists, updates tracked_at and clears
        cancelled_at (PR reopened). Does not clear merged_at
        or verified_at so that re-tracking a merged PR
        preserves its history.
        """
        db = self._require_db()
        await db.execute(
            """INSERT INTO pr_lifecycle
               (task_key, pr_url, tracked_at)
               VALUES (?, ?, ?)
               ON CONFLICT(task_key, pr_url)
               DO UPDATE SET
                   tracked_at = excluded.tracked_at,
                   cancelled_at = NULL
            """,
            (task_key, pr_url, tracked_at),
        )
        await db.commit()

    async def record_pr_merged(self, task_key: str, pr_url: str, merged_at: float) -> None:
        """Update the specific PR for a task with merge timestamp."""
        db = self._require_db()
        await db.execute(
            """UPDATE pr_lifecycle SET merged_at = ?
               WHERE task_key = ?
                 AND pr_url = ?
                 AND merged_at IS NULL
                 AND cancelled_at IS NULL""",
            (merged_at, task_key, pr_url),
        )
        await db.commit()

    async def record_pr_cancelled(self, task_key: str, cancelled_at: float) -> None:
        """Mark the most recent PR as cancelled."""
        db = self._require_db()
        await db.execute(
            """UPDATE pr_lifecycle SET cancelled_at = ?
               WHERE id = (
                   SELECT id FROM pr_lifecycle
                   WHERE task_key = ?
                     AND merged_at IS NULL
                     AND cancelled_at IS NULL
                   ORDER BY tracked_at DESC LIMIT 1
               )""",
            (cancelled_at, task_key),
        )
        await db.commit()

    async def record_pr_verified(
        self,
        task_key: str,
        verified_at: float,
    ) -> None:
        """Update the most recent PR with verification timestamp."""
        db = self._require_db()
        await db.execute(
            """UPDATE pr_lifecycle SET verified_at = ?
               WHERE id = (
                   SELECT id FROM pr_lifecycle
                   WHERE task_key = ?
                     AND cancelled_at IS NULL
                   ORDER BY tracked_at DESC LIMIT 1
               )""",
            (verified_at, task_key),
        )
        await db.commit()

    async def increment_review_iterations(self, task_key: str) -> None:
        """Increment review iteration count for the most recent PR."""
        db = self._require_db()
        await db.execute(
            """UPDATE pr_lifecycle SET review_iterations = review_iterations + 1
               WHERE id = (
                   SELECT id FROM pr_lifecycle
                   WHERE task_key = ?
                     AND merged_at IS NULL
                     AND cancelled_at IS NULL
                   ORDER BY tracked_at DESC LIMIT 1
               )""",
            (task_key,),
        )
        await db.commit()

    async def increment_ci_failures(self, task_key: str) -> None:
        """Increment CI failure count for the most recent PR."""
        db = self._require_db()
        await db.execute(
            """UPDATE pr_lifecycle SET ci_failures = ci_failures + 1
               WHERE id = (
                   SELECT id FROM pr_lifecycle
                   WHERE task_key = ?
                     AND merged_at IS NULL
                     AND cancelled_at IS NULL
                   ORDER BY tracked_at DESC LIMIT 1
               )""",
            (task_key,),
        )
        await db.commit()

    async def record_error(self, entry: ErrorLogEntry) -> None:
        """Record an error occurrence."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO error_log (task_key, error_category, error_message, retryable, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (entry.task_key, entry.error_category, entry.error_message, int(entry.retryable), entry.timestamp),
        )
        await db.commit()

    async def has_successful_task_run(self, task_key: str) -> bool:
        """Check if any successful task run exists for the given key."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT 1 FROM task_runs WHERE task_key = ? AND success = 1 LIMIT 1",
            (task_key,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def has_unmerged_pr(self, task_key: str) -> bool:
        """Check if an actively tracked, unmerged PR exists.

        Requires both a pr_lifecycle row (merged_at IS NULL)
        and a pr_tracking row. Defense-in-depth: a PR is
        considered merged if ANY row for the same
        (task_key, pr_url) has merged_at set, protecting
        against pre-migration duplicate rows.
        """
        db = self._require_db()
        cursor = await db.execute(
            """SELECT 1 FROM pr_lifecycle pl
               JOIN pr_tracking pt
                   ON pl.task_key = pt.task_key
               WHERE pl.task_key = ?
                 AND pl.merged_at IS NULL
                 AND pl.cancelled_at IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM pr_lifecycle pl2
                     WHERE pl2.task_key = pl.task_key
                       AND pl2.pr_url = pl.pr_url
                       AND pl2.merged_at IS NOT NULL
                 )
               LIMIT 1""",
            (task_key,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def get_latest_session_id(self, task_key: str) -> str | None:
        """Get the most recent non-null session_id for a task."""
        db = self._require_db()
        cursor = await db.execute(
            """SELECT session_id FROM task_runs
               WHERE task_key = ? AND session_id IS NOT NULL
               ORDER BY finished_at DESC LIMIT 1""",
            (task_key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ---- Query methods ----

    async def get_task_cost_summary(
        self,
        task_key: str,
    ) -> TaskCostSummary:
        """Get total cost and run count for a task."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM task_runs WHERE task_key = ?",
            (task_key,),
        )
        # Aggregate without GROUP BY always returns one row,
        # but fetchone() is typed as Row | None.
        row = await cursor.fetchone()
        if row is None:
            return {"total_cost_usd": 0.0, "run_count": 0}
        return {
            "total_cost_usd": row[0],
            "run_count": row[1],
        }

    async def get_summary(self, days: int = 7, since: float | None = None) -> dict:
        """Get summary statistics for the given time window."""
        db = self._require_db()
        cutoff = since if since is not None else time.time() - days * 86400

        cursor = await db.execute(
            """SELECT
                 COUNT(*) as total_tasks,
                 COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) as success_count,
                 COALESCE(SUM(cost_usd), 0) as total_cost,
                 COALESCE(AVG(duration_seconds), 0) as avg_duration,
                 COALESCE(AVG(cost_usd), 0) as avg_cost
               FROM task_runs
               WHERE finished_at >= ?""",
            (cutoff,),
        )
        row = await cursor.fetchone()

        total = row[0] if row else 0
        success_count = row[1] if row else 0
        success_rate = round(success_count / total * 100, 2) if total > 0 else 0.0

        return {
            "total_tasks": total,
            "success_count": success_count,
            "success_rate": success_rate,
            "total_cost": row[2] if row else 0.0,
            "avg_duration": row[3] if row else 0.0,
            "avg_cost": row[4] if row else 0.0,
            "days": days,
        }

    async def get_costs(
        self, group_by: str = "model", days: int = 7, limit: int = 50, since: float | None = None
    ) -> list[dict]:
        """Get cost breakdown grouped by model or day."""
        db = self._require_db()
        cutoff = since if since is not None else time.time() - days * 86400

        if group_by == "model":
            cursor = await db.execute(
                """SELECT model as 'group', SUM(cost_usd) as total_cost, COUNT(*) as count
                   FROM task_runs WHERE finished_at >= ?
                   GROUP BY model ORDER BY total_cost DESC LIMIT ?""",
                (cutoff, limit),
            )
        elif group_by == "day":
            cursor = await db.execute(
                """SELECT date(finished_at, 'unixepoch') as 'group',
                          SUM(cost_usd) as total_cost, COUNT(*) as count
                   FROM task_runs WHERE finished_at >= ?
                   GROUP BY date(finished_at, 'unixepoch') ORDER BY 1 DESC LIMIT ?""",
                (cutoff, limit),
            )
        else:
            return []

        rows = await cursor.fetchall()
        return [{"group": r[0], "total_cost": r[1], "count": r[2]} for r in rows]

    async def get_recent_tasks(self, limit: int = 20) -> list[dict]:
        """Get most recent task runs."""
        db = self._require_db()
        cursor = await db.execute(
            """SELECT task_key, model, cost_usd, duration_seconds, success,
                      error_category, pr_url, needs_info, resumed, started_at, finished_at
               FROM task_runs ORDER BY finished_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                **dict(r),
                "success": bool(r["success"]),
                "needs_info": bool(r["needs_info"]),
                "resumed": bool(r["resumed"]),
            }
            for r in rows
        ]

    async def get_error_stats(self, days: int = 7, since: float | None = None) -> list[dict]:
        """Get error statistics aggregated by category."""
        db = self._require_db()
        cutoff = since if since is not None else time.time() - days * 86400

        cursor = await db.execute(
            """SELECT error_category as category,
                      COUNT(*) as count,
                      SUM(CASE WHEN retryable = 1 THEN 1 ELSE 0 END) as retryable_count
               FROM error_log WHERE timestamp >= ?
               GROUP BY error_category ORDER BY count DESC""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [{"category": r[0], "count": r[1], "retryable_count": r[2]} for r in rows]

    async def execute_readonly(self, sql: str, limit: int = 100, timeout_seconds: float = 5.0) -> list[dict]:
        """Execute a read-only SQL query with validation.

        Only SELECT statements are allowed. Results are limited to prevent
        unbounded memory usage.

        Raises:
            ValueError: If the SQL contains non-SELECT statements or multiple statements.
        """
        db = self._require_db()

        # Reject multiple statements
        if _MULTI_STMT_PATTERN.search(sql):
            raise ValueError("Only SELECT statements are allowed (multiple statements detected)")

        # Reject write operations
        stripped = sql.strip()
        if _WRITE_PATTERN.match(stripped):
            raise ValueError("Only SELECT statements are allowed")

        upper = stripped.upper()
        if upper.startswith("PRAGMA"):
            # Extract pragma body after "PRAGMA "
            pragma_body = stripped[6:].strip()
            # Reject write PRAGMAs (contain '=' assignment)
            if "=" in pragma_body:
                raise ValueError("Write PRAGMAs are not allowed (only read-only PRAGMAs permitted)")
            # Extract pragma name and check for arguments: "PRAGMA name" or "PRAGMA name(arg)"
            pragma_name = pragma_body.split("(")[0].strip().lower()
            has_args = "(" in pragma_body
            if pragma_name not in _SAFE_PRAGMAS:
                raise ValueError(f"PRAGMA '{pragma_name}' is not allowed (only read-only PRAGMAs permitted)")
            # Pragmas that become writes when given arguments (e.g. journal_mode(WAL))
            if has_args and pragma_name in _SAFE_PRAGMAS_NO_ARGS:
                raise ValueError("Write PRAGMAs are not allowed (only read-only PRAGMAs permitted)")
        elif upper.startswith("WITH"):
            # CTE: strip parenthesised blocks to find the main statement keyword.
            # CTEs are `WITH name AS (...) [, name AS (...)]* <main_stmt>`.
            # The main statement must be SELECT.
            depth = 0
            main_start = -1
            for i, ch in enumerate(upper):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        main_start = i + 1
            if main_start > 0:
                main_keyword = upper[main_start:].lstrip()
                if not main_keyword.startswith("SELECT"):
                    raise ValueError("Only SELECT statements are allowed")
            else:
                raise ValueError("Only SELECT statements are allowed")
        elif not upper.startswith("SELECT"):
            raise ValueError("Only SELECT statements are allowed")

        async with asyncio.timeout(timeout_seconds):
            cursor = await db.execute(stripped)
            rows = await cursor.fetchmany(limit)
        return [dict(r) for r in rows]

    # ---- Proposal methods ----

    async def resolve_proposal(self, proposal_id: str, status: str, resolved_at: float) -> None:
        """Update a proposal's status to approved or rejected."""
        db = self._require_db()
        await db.execute(
            "UPDATE proposals SET status = ?, resolved_at = ? WHERE proposal_id = ?",
            (status, resolved_at, proposal_id),
        )
        await db.commit()

    async def get_proposal_stats(self, days: int = 7, since: float | None = None) -> dict:
        """Get proposal counts grouped by status."""
        db = self._require_db()
        cutoff = since if since is not None else time.time() - days * 86400

        cursor = await db.execute(
            """SELECT status, COUNT(*) as count
               FROM proposals WHERE created_at >= ?
               GROUP BY status""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        result: dict[str, int] = {}
        for r in rows:
            result[r[0]] = r[1]
        return result

    # ---- Recovery methods ----

    async def upsert_recovery_state(self, record: RecoveryRecord) -> None:
        """Insert or update recovery state for an issue."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO recovery_states (issue_key, attempt_count, no_pr_count, last_output, updated_at, no_pr_cost)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(issue_key) DO UPDATE SET
                   attempt_count = excluded.attempt_count,
                   no_pr_count = excluded.no_pr_count,
                   last_output = excluded.last_output,
                   updated_at = excluded.updated_at,
                   no_pr_cost = excluded.no_pr_cost""",
            (
                record.issue_key,
                record.attempt_count,
                record.no_pr_count,
                record.last_output,
                record.updated_at,
                record.no_pr_cost,
            ),
        )
        await db.commit()

    async def record_recovery_attempt(
        self, issue_key: str, timestamp: float, category: str, error_message: str
    ) -> None:
        """Record a single recovery attempt."""
        db = self._require_db()
        await db.execute(
            "INSERT INTO recovery_attempts (issue_key, timestamp, category, error_message) VALUES (?, ?, ?, ?)",
            (issue_key, timestamp, category, error_message),
        )
        await db.commit()

    async def delete_recovery(self, issue_key: str) -> None:
        """Delete all recovery data for an issue."""
        db = self._require_db()
        await db.execute("DELETE FROM recovery_states WHERE issue_key = ?", (issue_key,))
        await db.execute("DELETE FROM recovery_attempts WHERE issue_key = ?", (issue_key,))
        await db.commit()

    async def load_recovery_states(self) -> list[RecoveryRecord]:
        """Load all recovery states from the database."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT issue_key, attempt_count, no_pr_count, last_output, updated_at, no_pr_cost FROM recovery_states"
        )
        rows = await cursor.fetchall()
        return [
            RecoveryRecord(
                issue_key=r[0],
                attempt_count=r[1],
                no_pr_count=r[2],
                last_output=r[3],
                updated_at=r[4],
                no_pr_cost=r[5] if len(r) > 5 else 0.0,
            )
            for r in rows
        ]

    async def load_recovery_attempts(self, issue_key: str) -> list[tuple[float, str, str]]:
        """Load recovery attempts for an issue. Returns (timestamp, category, error_message) tuples."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT timestamp, category, error_message FROM recovery_attempts WHERE issue_key = ? ORDER BY timestamp",
            (issue_key,),
        )
        rows = await cursor.fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # ---- Epic coordinator methods ----

    async def upsert_epic_state(self, record: EpicStateRecord) -> None:
        """Insert or update an epic state."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO epic_states (epic_key, epic_summary, phase, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(epic_key) DO UPDATE SET
                   epic_summary = excluded.epic_summary,
                   phase = excluded.phase,
                   created_at = excluded.created_at""",
            (record.epic_key, record.epic_summary, record.phase, record.created_at),
        )
        await db.commit()

    async def upsert_epic_child(self, epic_key: str, child: EpicChildRecord) -> None:
        """Insert or update an epic child."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO epic_children (epic_key, child_key, summary, status, depends_on, tracker_status, last_comment_id, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(epic_key, child_key) DO UPDATE SET
                   summary = excluded.summary,
                   status = excluded.status,
                   depends_on = excluded.depends_on,
                   tracker_status = excluded.tracker_status,
                   last_comment_id = excluded.last_comment_id,
                   tags = excluded.tags""",
            (
                epic_key,
                child.child_key,
                child.summary,
                child.status,
                json.dumps(child.depends_on),
                child.tracker_status,
                child.last_comment_id,
                json.dumps(child.tags),
            ),
        )
        await db.commit()

    async def delete_epic(self, epic_key: str) -> None:
        """Delete an epic and all its children."""
        db = self._require_db()
        await db.execute("DELETE FROM epic_children WHERE epic_key = ?", (epic_key,))
        await db.execute("DELETE FROM epic_states WHERE epic_key = ?", (epic_key,))
        await db.commit()

    async def load_epic_states(self) -> list[EpicStateRecord]:
        """Load all epic states from the database."""
        db = self._require_db()
        cursor = await db.execute("SELECT epic_key, epic_summary, phase, created_at FROM epic_states")
        rows = await cursor.fetchall()
        return [
            EpicStateRecord(
                epic_key=r[0],
                epic_summary=r[1],
                phase=r[2],
                created_at=r[3],
            )
            for r in rows
        ]

    async def load_epic_children(self, epic_key: str) -> list[EpicChildRecord]:
        """Load all children for an epic."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT child_key, summary, status, depends_on, tracker_status, last_comment_id, tags FROM epic_children WHERE epic_key = ?",
            (epic_key,),
        )
        rows = await cursor.fetchall()
        return [
            EpicChildRecord(
                child_key=r[0],
                summary=r[1],
                status=r[2],
                depends_on=json.loads(r[3]),
                tracker_status=r[4],
                last_comment_id=r[5],
                tags=json.loads(r[6]),
            )
            for r in rows
        ]

    # ---- PR tracking dedup methods ----

    async def upsert_pr_tracking(self, data: PRTrackingData) -> None:
        """Insert or update PR tracking dedup data."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO pr_tracking
               (task_key, pr_url, issue_summary,
                seen_thread_ids, seen_failed_checks,
                session_id, seen_merge_conflict,
                merge_conflict_retries,
                merge_conflict_head_sha)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_key) DO UPDATE SET
                   pr_url = excluded.pr_url,
                   issue_summary = excluded.issue_summary,
                   seen_thread_ids = excluded.seen_thread_ids,
                   seen_failed_checks = excluded.seen_failed_checks,
                   session_id = excluded.session_id,
                   seen_merge_conflict = excluded.seen_merge_conflict,
                   merge_conflict_retries = excluded.merge_conflict_retries,
                   merge_conflict_head_sha = excluded.merge_conflict_head_sha""",
            (
                data.task_key,
                data.pr_url,
                data.issue_summary,
                json.dumps(data.seen_thread_ids),
                json.dumps(data.seen_failed_checks),
                data.session_id,
                int(data.seen_merge_conflict),
                data.merge_conflict_retries,
                data.merge_conflict_head_sha,
            ),
        )
        await db.commit()

    async def delete_pr_tracking(self, task_key: str) -> None:
        """Delete PR tracking data for a task."""
        db = self._require_db()
        await db.execute("DELETE FROM pr_tracking WHERE task_key = ?", (task_key,))
        await db.commit()

    async def load_pr_tracking(
        self,
        task_key: str | None = None,
    ) -> list[PRTrackingData]:
        """Load PR tracking data, optionally filtered by task_key."""
        db = self._require_db()
        query = (
            "SELECT task_key, pr_url, issue_summary,"
            " seen_thread_ids, seen_failed_checks,"
            " session_id, seen_merge_conflict,"
            " merge_conflict_retries,"
            " merge_conflict_head_sha"
            " FROM pr_tracking"
        )
        params: tuple[str, ...] = ()
        if task_key is not None:
            query += " WHERE task_key = ?"
            params = (task_key,)
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [
            PRTrackingData(
                task_key=r[0],
                pr_url=r[1],
                issue_summary=r[2],
                seen_thread_ids=json.loads(r[3]),
                seen_failed_checks=json.loads(r[4]),
                session_id=r[5],
                seen_merge_conflict=bool(r[6]),
                merge_conflict_retries=r[7],
                merge_conflict_head_sha=r[8] or "",
            )
            for r in rows
        ]

    # ---- Needs-info tracking methods ----

    async def upsert_needs_info_tracking(self, record: NeedsInfoTrackingRecord) -> None:
        """Insert or update needs-info tracking data."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO needs_info_tracking (issue_key, last_seen_comment_id, issue_summary, tracked_at, session_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(issue_key) DO UPDATE SET
                   last_seen_comment_id = excluded.last_seen_comment_id,
                   issue_summary = excluded.issue_summary,
                   tracked_at = excluded.tracked_at,
                   session_id = excluded.session_id""",
            (record.issue_key, record.last_seen_comment_id, record.issue_summary, record.tracked_at, record.session_id),
        )
        await db.commit()

    async def delete_needs_info_tracking(self, issue_key: str) -> None:
        """Delete needs-info tracking data for an issue."""
        db = self._require_db()
        await db.execute("DELETE FROM needs_info_tracking WHERE issue_key = ?", (issue_key,))
        await db.commit()

    async def load_needs_info_tracking(self) -> list[NeedsInfoTrackingRecord]:
        """Load all needs-info tracking data."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT issue_key, last_seen_comment_id, issue_summary, tracked_at, session_id FROM needs_info_tracking"
        )
        rows = await cursor.fetchall()
        return [
            NeedsInfoTrackingRecord(
                issue_key=r[0],
                last_seen_comment_id=r[1],
                issue_summary=r[2],
                tracked_at=r[3],
                session_id=r[4],
            )
            for r in rows
        ]

    # ---- Deferred tasks (dependency manager) methods ----

    async def upsert_deferred_task(self, record: DeferredTaskRecord) -> None:
        """Insert or update a deferred task."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO deferred_tasks (issue_key, issue_summary, blockers, deferred_at, manual)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(issue_key) DO UPDATE SET
                   issue_summary = excluded.issue_summary,
                   blockers = excluded.blockers,
                   deferred_at = excluded.deferred_at,
                   manual = excluded.manual""",
            (
                record.issue_key,
                record.issue_summary,
                json.dumps(record.blockers),
                record.deferred_at,
                int(record.manual),
            ),
        )
        await db.commit()

    async def delete_deferred_task(self, issue_key: str) -> None:
        """Delete a deferred task record."""
        db = self._require_db()
        await db.execute("DELETE FROM deferred_tasks WHERE issue_key = ?", (issue_key,))
        await db.commit()

    async def load_deferred_tasks(self) -> list[DeferredTaskRecord]:
        """Load all deferred tasks from the database."""
        db = self._require_db()
        cursor = await db.execute("SELECT issue_key, issue_summary, blockers, deferred_at, manual FROM deferred_tasks")
        rows = await cursor.fetchall()
        return [
            DeferredTaskRecord(
                issue_key=r[0],
                issue_summary=r[1],
                blockers=json.loads(r[2]),
                deferred_at=r[3],
                manual=bool(r[4]),
            )
            for r in rows
        ]

    # ---- Proposal extended methods ----

    async def upsert_proposal(self, record: ProposalRecord) -> None:
        """Insert or update a proposal with all fields."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO proposals
               (proposal_id, source_task_key, summary, category, status, created_at, resolved_at, description, component, tracker_issue_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(proposal_id) DO UPDATE SET
                   summary = excluded.summary,
                   category = excluded.category,
                   status = excluded.status,
                   resolved_at = excluded.resolved_at,
                   description = excluded.description,
                   component = excluded.component,
                   tracker_issue_key = excluded.tracker_issue_key""",
            (
                record.proposal_id,
                record.source_task_key,
                record.summary,
                record.category,
                record.status,
                record.created_at,
                record.resolved_at,
                record.description,
                record.component,
                record.tracker_issue_key,
            ),
        )
        await db.commit()

    async def load_proposals(self) -> list[dict]:
        """Load all proposals with extended fields."""
        db = self._require_db()
        cursor = await db.execute(
            """SELECT proposal_id, source_task_key, summary, category, status, created_at,
                      resolved_at, description, component, tracker_issue_key
               FROM proposals"""
        )
        rows = await cursor.fetchall()
        return [
            {
                "proposal_id": r[0],
                "source_task_key": r[1],
                "summary": r[2],
                "category": r[3],
                "status": r[4],
                "created_at": r[5],
                "resolved_at": r[6],
                "description": r[7] or "",
                "component": r[8] or "",
                "tracker_issue_key": r[9],
            }
            for r in rows
        ]

    # ---- Event persistence methods ----

    async def save_event(self, event_type: str, task_key: str, data: str, timestamp: float) -> None:
        """Save a single event."""
        db = self._require_db()
        await db.execute(
            "INSERT INTO events (type, task_key, data, timestamp) VALUES (?, ?, ?, ?)",
            (event_type, task_key, data, timestamp),
        )
        await db.commit()

    async def save_events_batch(self, events: list[tuple[str, str, str, float]]) -> None:
        """Save a batch of events using executemany."""
        if not events:
            return
        db = self._require_db()
        await db.executemany(
            "INSERT INTO events (type, task_key, data, timestamp) VALUES (?, ?, ?, ?)",
            events,
        )
        await db.commit()

    async def load_events_for_task(self, task_key: str, limit: int = 500) -> list[dict]:
        """Load events for a specific task, oldest first."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT type, task_key, data, timestamp FROM events WHERE task_key = ? ORDER BY timestamp ASC LIMIT ?",
            (task_key, limit),
        )
        rows = await cursor.fetchall()
        return [{"type": r[0], "task_key": r[1], "data": r[2], "timestamp": r[3]} for r in rows]

    async def load_recent_events(self, limit: int = 5000) -> list[dict]:
        """Load most recent events across all tasks, oldest first."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT type, task_key, data, timestamp FROM events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        # Reverse to get oldest-first order
        result = [{"type": r[0], "task_key": r[1], "data": r[2], "timestamp": r[3]} for r in rows]
        result.reverse()
        return result

    async def delete_old_events(self, before_timestamp: float) -> int:
        """Delete events older than the given timestamp. Returns count of deleted rows."""
        db = self._require_db()
        cursor = await db.execute(
            "DELETE FROM events WHERE timestamp < ?",
            (before_timestamp,),
        )
        await db.commit()
        return cursor.rowcount

    # ---- Environment config methods ----

    async def get_environment(self, name: str) -> dict | None:
        """Get environment config by name.

        Returns a dict with name, config, updated_at,
        updated_by or None if not found.
        """
        db = self._require_db()
        cursor = await db.execute(
            "SELECT name, config, updated_at, updated_by FROM environment_config WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "name": row[0],
            "config": json.loads(row[1]),
            "updated_at": row[2],
            "updated_by": row[3],
        }

    async def set_environment(
        self,
        name: str,
        config: dict,
        updated_by: str,
    ) -> None:
        """Insert or update an environment config."""
        db = self._require_db()
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        await db.execute(
            """INSERT INTO environment_config
               (name, config, updated_at, updated_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   config = excluded.config,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            (name, json.dumps(config), now_iso, updated_by),
        )
        await db.commit()

    async def list_environments(self) -> list[dict]:
        """List all environment configs (name, updated_at, updated_by)."""
        db = self._require_db()
        cursor = await db.execute("SELECT name, updated_at, updated_by FROM environment_config ORDER BY name")
        rows = await cursor.fetchall()
        return [
            {
                "name": r[0],
                "updated_at": r[1],
                "updated_by": r[2],
            }
            for r in rows
        ]

    async def delete_environment(self, name: str) -> None:
        """Delete an environment config by name."""
        db = self._require_db()
        await db.execute(
            "DELETE FROM environment_config WHERE name = ?",
            (name,),
        )
        await db.commit()
