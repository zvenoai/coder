"""Tests for SQLiteStorage — SQLite persistence for orchestrator."""

from __future__ import annotations

import time

import pytest


def _all_migration_versions() -> list[int]:
    """Discover all migration versions for parametrized tests."""
    from orchestrator.sqlite_storage import SQLiteStorage

    return [v for v, _ in SQLiteStorage._discover_migrations()]


@pytest.fixture
async def storage(tmp_path):
    """Create a SQLiteStorage with a temporary database."""
    from orchestrator.sqlite_storage import SQLiteStorage

    db = SQLiteStorage(str(tmp_path / "test_coder.db"))
    await db.open()
    yield db
    await db.close()


class TestRequireDb:
    async def test_raises_runtime_error_when_not_open(self, tmp_path) -> None:
        """Calling methods without open() should raise RuntimeError, not AssertionError."""
        from orchestrator.sqlite_storage import SQLiteStorage
        from orchestrator.stats_models import TaskRun

        db = SQLiteStorage(str(tmp_path / "not_opened.db"))
        with pytest.raises(RuntimeError, match="SQLiteStorage is not open"):
            await db.record_task_run(
                TaskRun(
                    task_key="QR-1",
                    model="sonnet",
                    cost_usd=0.1,
                    duration_seconds=10.0,
                    success=True,
                    error_category=None,
                    pr_url=None,
                    needs_info=False,
                    resumed=False,
                    started_at=0.0,
                    finished_at=0.0,
                )
            )

    async def test_raises_on_get_summary_when_not_open(self, tmp_path) -> None:
        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(str(tmp_path / "not_opened.db"))
        with pytest.raises(RuntimeError, match="SQLiteStorage is not open"):
            await db.get_summary()

    async def test_raises_on_execute_readonly_when_not_open(self, tmp_path) -> None:
        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(str(tmp_path / "not_opened.db"))
        with pytest.raises(RuntimeError, match="SQLiteStorage is not open"):
            await db.execute_readonly("SELECT 1")


class TestMigrations:
    async def test_open_creates_tables(self, storage) -> None:
        rows = await storage.execute_readonly("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = {r["name"] for r in rows}
        assert "task_runs" in names
        assert "supervisor_runs" in names
        assert "pr_lifecycle" in names
        assert "error_log" in names

    async def test_open_idempotent(self, tmp_path) -> None:
        """Opening the same DB twice should not fail."""
        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(str(tmp_path / "idempotent.db"))
        await db.open()
        await db.close()

        db2 = SQLiteStorage(str(tmp_path / "idempotent.db"))
        await db2.open()
        # Verify tables exist (proving migrations ran successfully)
        rows = await db2.execute_readonly("SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'")
        assert len(rows) == 1
        await db2.close()

    async def test_wal_mode_enabled(self, storage) -> None:
        rows = await storage.execute_readonly("PRAGMA journal_mode")
        assert rows[0]["journal_mode"] == "wal"

    async def test_user_version_matches_latest_migration(self, storage) -> None:
        """PRAGMA user_version should equal the highest migration number."""
        db = storage._require_db()
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        latest = max(_all_migration_versions())
        assert row["user_version"] == latest

    @pytest.mark.parametrize(
        "rollback_version",
        _all_migration_versions(),
        ids=lambda v: f"rollback_to_v{v}",
    )
    async def test_migration_idempotent_rerun(
        self,
        tmp_path,
        rollback_version: int,
    ) -> None:
        """Every migration must be idempotent: re-applying after crash must not fail."""
        import aiosqlite

        from orchestrator.sqlite_storage import SQLiteStorage

        db_path = str(tmp_path / f"v{rollback_version}_idem.db")

        # First open — applies all migrations
        db = SQLiteStorage(db_path)
        await db.open()
        await db.close()

        # Simulate crash: roll back user_version so migrations re-run
        async with aiosqlite.connect(db_path) as raw:
            await raw.execute(f"PRAGMA user_version = {rollback_version}")
            await raw.commit()

        # Re-open must succeed — all replayed migrations are idempotent
        db2 = SQLiteStorage(db_path)
        await db2.open()
        conn = db2._require_db()
        cursor = await conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        latest = max(_all_migration_versions())
        assert row["user_version"] == latest
        await db2.close()

    async def test_backward_compat_legacy_schema_version_table(self, tmp_path) -> None:
        """DB with old schema_version table should migrate to PRAGMA user_version."""
        import aiosqlite

        from orchestrator.sqlite_storage import SQLiteStorage

        db_path = str(tmp_path / "legacy.db")

        # Simulate a legacy DB: create schema_version table with version=6
        async with aiosqlite.connect(db_path) as raw:
            await raw.execute("PRAGMA journal_mode=WAL")
            # Create just enough schema for the migration to detect legacy table
            await raw.executescript("""
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (6);
            """)
            # Pre-create all tables so migrations don't fail
            from orchestrator.sqlite_storage import _MIGRATIONS_DIR

            for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                await raw.executescript(sql_file.read_text())
            # Apply V6 columns manually
            await raw.execute("ALTER TABLE proposals ADD COLUMN description TEXT DEFAULT ''")
            await raw.execute("ALTER TABLE proposals ADD COLUMN component TEXT DEFAULT ''")
            await raw.execute("ALTER TABLE proposals ADD COLUMN tracker_issue_key TEXT")
            await raw.commit()

        # Open with new migration system — should migrate from legacy table
        db = SQLiteStorage(db_path)
        await db.open()

        # schema_version table should be gone
        rows = await db.execute_readonly("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
        assert len(rows) == 0

        # PRAGMA user_version should be set (all migrations including V9 applied)
        conn = db._require_db()
        cursor = await conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row["user_version"] == 19

        await db.close()


class TestRecordTaskRun:
    async def test_insert_and_query(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        run = TaskRun(
            task_key="QR-1",
            model="claude-sonnet-4-5-20250929",
            cost_usd=0.5,
            duration_seconds=120.0,
            success=True,
            error_category=None,
            pr_url="https://github.com/org/repo/pull/1",
            needs_info=False,
            resumed=False,
            started_at=now - 120,
            finished_at=now,
        )
        await storage.record_task_run(run)

        rows = await storage.execute_readonly("SELECT * FROM task_runs WHERE task_key = 'QR-1'")
        assert len(rows) == 1
        assert rows[0]["model"] == "claude-sonnet-4-5-20250929"
        assert rows[0]["cost_usd"] == 0.5
        assert rows[0]["success"] == 1

    async def test_insert_failed_run(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        run = TaskRun(
            task_key="QR-2",
            model="claude-opus-4-6",
            cost_usd=1.2,
            duration_seconds=60.0,
            success=False,
            error_category="timeout",
            pr_url=None,
            needs_info=False,
            resumed=False,
            started_at=now - 60,
            finished_at=now,
        )
        await storage.record_task_run(run)

        rows = await storage.execute_readonly("SELECT * FROM task_runs WHERE task_key = 'QR-2'")
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert rows[0]["error_category"] == "timeout"


class TestGetTaskCostSummary:
    async def test_empty_task(self, storage) -> None:
        summary = await storage.get_task_cost_summary("QR-99")
        assert summary["total_cost_usd"] == 0.0
        assert summary["run_count"] == 0

    async def test_single_run(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        await storage.record_task_run(
            TaskRun(
                task_key="QR-1",
                model="sonnet",
                cost_usd=2.5,
                duration_seconds=120.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 120,
                finished_at=now,
            )
        )
        summary = await storage.get_task_cost_summary("QR-1")
        assert summary["total_cost_usd"] == 2.5
        assert summary["run_count"] == 1

    async def test_multiple_runs_summed(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        for i in range(3):
            await storage.record_task_run(
                TaskRun(
                    task_key="QR-X",
                    model="sonnet",
                    cost_usd=1.0 + i * 0.5,
                    duration_seconds=60.0,
                    success=i < 2,
                    error_category=None,
                    pr_url=None,
                    needs_info=False,
                    resumed=False,
                    started_at=now - 180 + i * 60,
                    finished_at=now - 120 + i * 60,
                )
            )
        summary = await storage.get_task_cost_summary("QR-X")
        assert summary["total_cost_usd"] == 4.5  # 1.0 + 1.5 + 2.0
        assert summary["run_count"] == 3


class TestRecordSupervisorRun:
    async def test_insert_and_query(self, storage) -> None:
        from orchestrator.stats_models import SupervisorRun

        now = time.time()
        run = SupervisorRun(
            trigger_task_keys=["QR-1", "QR-2"],
            cost_usd=0.3,
            duration_seconds=45.0,
            success=True,
            tasks_created=["QR-10"],
            started_at=now - 45,
            finished_at=now,
        )
        await storage.record_supervisor_run(run)

        rows = await storage.execute_readonly("SELECT * FROM supervisor_runs")
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == 0.3
        # JSON-encoded lists
        assert "QR-1" in rows[0]["trigger_task_keys"]
        assert "QR-10" in rows[0]["tasks_created"]


class TestRecordPRLifecycle:
    async def test_track_and_merge(self, storage) -> None:
        now = time.time()
        await storage.record_pr_tracked("QR-1", "https://github.com/org/repo/pull/1", now)

        rows = await storage.execute_readonly("SELECT * FROM pr_lifecycle WHERE task_key = 'QR-1'")
        assert len(rows) == 1
        assert rows[0]["merged_at"] is None

        await storage.record_pr_merged("QR-1", "https://github.com/org/repo/pull/1", now + 3600)
        rows = await storage.execute_readonly("SELECT * FROM pr_lifecycle WHERE task_key = 'QR-1'")
        assert rows[0]["merged_at"] is not None

    async def test_increment_review_iterations(self, storage) -> None:
        now = time.time()
        await storage.record_pr_tracked("QR-1", "https://github.com/org/repo/pull/1", now)
        await storage.increment_review_iterations("QR-1")
        await storage.increment_review_iterations("QR-1")

        rows = await storage.execute_readonly("SELECT review_iterations FROM pr_lifecycle WHERE task_key = 'QR-1'")
        assert rows[0]["review_iterations"] == 2

    async def test_increment_ci_failures(self, storage) -> None:
        now = time.time()
        await storage.record_pr_tracked("QR-1", "https://github.com/org/repo/pull/1", now)
        await storage.increment_ci_failures("QR-1")

        rows = await storage.execute_readonly("SELECT ci_failures FROM pr_lifecycle WHERE task_key = 'QR-1'")
        assert rows[0]["ci_failures"] == 1


class TestRecordError:
    async def test_insert_and_query(self, storage) -> None:
        from orchestrator.stats_models import ErrorLogEntry

        entry = ErrorLogEntry(
            task_key="QR-3",
            error_category="api_error",
            error_message="Connection timeout",
            retryable=True,
            timestamp=time.time(),
        )
        await storage.record_error(entry)

        rows = await storage.execute_readonly("SELECT * FROM error_log WHERE task_key = 'QR-3'")
        assert len(rows) == 1
        assert rows[0]["error_category"] == "api_error"
        assert rows[0]["retryable"] == 1


class TestGetSummary:
    async def test_empty_db(self, storage) -> None:
        summary = await storage.get_summary()
        assert summary["total_tasks"] == 0
        assert summary["success_rate"] == 0.0
        assert summary["total_cost"] == 0.0

    async def test_empty_db_success_count_is_int(self, storage) -> None:
        """success_count must be 0 (int), not None, on an empty database."""
        summary = await storage.get_summary()
        assert summary["success_count"] is not None, "success_count should not be None"
        assert summary["success_count"] == 0
        assert isinstance(summary["success_count"], int)

    async def test_with_data(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        for i in range(3):
            await storage.record_task_run(
                TaskRun(
                    task_key=f"QR-{i}",
                    model="sonnet",
                    cost_usd=1.0,
                    duration_seconds=60.0,
                    success=i < 2,  # 2 success, 1 failure
                    error_category="error" if i >= 2 else None,
                    pr_url=None,
                    needs_info=False,
                    resumed=False,
                    started_at=now - 60,
                    finished_at=now,
                )
            )

        summary = await storage.get_summary(days=7)
        assert summary["total_tasks"] == 3
        assert abs(summary["success_rate"] - 66.67) < 0.1
        assert summary["total_cost"] == 3.0
        assert summary["avg_duration"] == 60.0

    async def test_respects_time_window(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        # Old task (30 days ago)
        await storage.record_task_run(
            TaskRun(
                task_key="QR-old",
                model="sonnet",
                cost_usd=10.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 86400 * 30,
                finished_at=now - 86400 * 30 + 60,
            )
        )
        # Recent task
        await storage.record_task_run(
            TaskRun(
                task_key="QR-new",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=30.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 60,
                finished_at=now,
            )
        )

        summary = await storage.get_summary(days=7)
        assert summary["total_tasks"] == 1
        assert summary["total_cost"] == 1.0


class TestSinceParameter:
    async def test_get_summary_with_since(self, storage) -> None:
        """since parameter overrides days-based cutoff for deterministic testing."""
        from orchestrator.stats_models import TaskRun

        base = 1_000_000.0
        # Task at base + 100
        await storage.record_task_run(
            TaskRun(
                task_key="QR-S1",
                model="sonnet",
                cost_usd=2.0,
                duration_seconds=30.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=base + 70,
                finished_at=base + 100,
            )
        )
        # Task at base + 200
        await storage.record_task_run(
            TaskRun(
                task_key="QR-S2",
                model="sonnet",
                cost_usd=3.0,
                duration_seconds=50.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=base + 150,
                finished_at=base + 200,
            )
        )

        # since before both → both included
        summary = await storage.get_summary(since=base)
        assert summary["total_tasks"] == 2
        assert summary["total_cost"] == 5.0

        # since between them → only second included
        summary = await storage.get_summary(since=base + 150)
        assert summary["total_tasks"] == 1
        assert summary["total_cost"] == 3.0

    async def test_get_costs_with_since(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        base = 1_000_000.0
        await storage.record_task_run(
            TaskRun(
                task_key="QR-C1",
                model="opus",
                cost_usd=5.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=base,
                finished_at=base + 60,
            )
        )

        costs = await storage.get_costs(group_by="model", since=base)
        assert len(costs) == 1
        assert costs[0]["group"] == "opus"

        costs = await storage.get_costs(group_by="model", since=base + 100)
        assert len(costs) == 0

    async def test_get_error_stats_with_since(self, storage) -> None:
        from orchestrator.stats_models import ErrorLogEntry

        base = 1_000_000.0
        await storage.record_error(
            ErrorLogEntry(
                task_key="QR-E1",
                error_category="timeout",
                error_message="test",
                retryable=True,
                timestamp=base + 10,
            )
        )

        errors = await storage.get_error_stats(since=base)
        assert len(errors) == 1
        assert errors[0]["category"] == "timeout"

        errors = await storage.get_error_stats(since=base + 100)
        assert len(errors) == 0


class TestGetCosts:
    async def test_group_by_model(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        for model, cost in [("sonnet", 1.0), ("sonnet", 2.0), ("opus", 5.0)]:
            await storage.record_task_run(
                TaskRun(
                    task_key=f"QR-{model}-{cost}",
                    model=model,
                    cost_usd=cost,
                    duration_seconds=60.0,
                    success=True,
                    error_category=None,
                    pr_url=None,
                    needs_info=False,
                    resumed=False,
                    started_at=now - 60,
                    finished_at=now,
                )
            )

        costs = await storage.get_costs(group_by="model", days=7)
        by_model = {r["group"]: r for r in costs}
        assert by_model["sonnet"]["total_cost"] == 3.0
        assert by_model["sonnet"]["count"] == 2
        assert by_model["opus"]["total_cost"] == 5.0

    async def test_group_by_day(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        await storage.record_task_run(
            TaskRun(
                task_key="QR-1",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 60,
                finished_at=now,
            )
        )

        costs = await storage.get_costs(group_by="day", days=7)
        assert len(costs) >= 1
        assert costs[0]["total_cost"] == 1.0

    async def test_group_by_day_ordered_desc(self, storage) -> None:
        """Days should be ordered DESC (most recent first)."""
        from orchestrator.stats_models import TaskRun

        # Insert newer day FIRST so insertion order is opposite of expected DESC
        day1_ts = 1735689600.0  # 2025-01-01 00:00:00 UTC
        day2_ts = 1735776000.0  # 2025-01-02 00:00:00 UTC
        day3_ts = 1735862400.0  # 2025-01-03 00:00:00 UTC
        # Insert in chronological order (ASC) — GROUP BY returns them in this order
        # so DESC ordering must reverse them
        for day_ts, key, cost in [
            (day1_ts, "QR-D1", 1.0),
            (day2_ts, "QR-D2", 2.0),
            (day3_ts, "QR-D3", 3.0),
        ]:
            await storage.record_task_run(
                TaskRun(
                    task_key=key,
                    model="sonnet",
                    cost_usd=cost,
                    duration_seconds=60.0,
                    success=True,
                    error_category=None,
                    pr_url=None,
                    needs_info=False,
                    resumed=False,
                    started_at=day_ts,
                    finished_at=day_ts + 60,
                )
            )

        costs = await storage.get_costs(group_by="day", since=day1_ts)
        assert len(costs) == 3
        # Most recent day first (DESC)
        assert costs[0]["group"] == "2025-01-03"
        assert costs[1]["group"] == "2025-01-02"
        assert costs[2]["group"] == "2025-01-01"


class TestGetRecentTasks:
    async def test_returns_recent(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        for i in range(5):
            await storage.record_task_run(
                TaskRun(
                    task_key=f"QR-{i}",
                    model="sonnet",
                    cost_usd=float(i),
                    duration_seconds=60.0,
                    success=True,
                    error_category=None,
                    pr_url=None,
                    needs_info=False,
                    resumed=False,
                    started_at=now - 60 + i,
                    finished_at=now + i,
                )
            )

        recent = await storage.get_recent_tasks(limit=3)
        assert len(recent) == 3
        # Should be ordered by finished_at DESC
        assert recent[0]["task_key"] == "QR-4"


class TestGetErrorStats:
    async def test_aggregates_errors(self, storage) -> None:
        from orchestrator.stats_models import ErrorLogEntry

        now = time.time()
        for cat, retryable in [("timeout", True), ("timeout", True), ("api_error", False)]:
            await storage.record_error(
                ErrorLogEntry(
                    task_key="QR-1",
                    error_category=cat,
                    error_message="test",
                    retryable=retryable,
                    timestamp=now,
                )
            )

        errors = await storage.get_error_stats(days=7)
        by_cat = {r["category"]: r for r in errors}
        assert by_cat["timeout"]["count"] == 2
        assert by_cat["timeout"]["retryable_count"] == 2
        assert by_cat["api_error"]["count"] == 1
        assert by_cat["api_error"]["retryable_count"] == 0


class TestExecuteReadonly:
    async def test_select_works(self, storage) -> None:
        rows = await storage.execute_readonly("SELECT 1 AS val")
        assert rows[0]["val"] == 1

    async def test_rejects_write_statements(self, storage) -> None:
        with pytest.raises(ValueError, match="Only SELECT"):
            await storage.execute_readonly("DROP TABLE task_runs")

    async def test_rejects_delete(self, storage) -> None:
        with pytest.raises(ValueError, match="Only SELECT"):
            await storage.execute_readonly("DELETE FROM task_runs")

    async def test_rejects_insert(self, storage) -> None:
        with pytest.raises(ValueError, match="Only SELECT"):
            await storage.execute_readonly("INSERT INTO task_runs VALUES (1)")

    async def test_rejects_update(self, storage) -> None:
        with pytest.raises(ValueError, match="Only SELECT"):
            await storage.execute_readonly("UPDATE task_runs SET cost_usd = 0")

    async def test_respects_limit(self, storage) -> None:
        from orchestrator.stats_models import TaskRun

        now = time.time()
        for i in range(10):
            await storage.record_task_run(
                TaskRun(
                    task_key=f"QR-{i}",
                    model="sonnet",
                    cost_usd=1.0,
                    duration_seconds=60.0,
                    success=True,
                    error_category=None,
                    pr_url=None,
                    needs_info=False,
                    resumed=False,
                    started_at=now,
                    finished_at=now,
                )
            )

        rows = await storage.execute_readonly("SELECT * FROM task_runs", limit=3)
        assert len(rows) == 3

    async def test_semicolon_injection(self, storage) -> None:
        with pytest.raises(ValueError, match="Only SELECT"):
            await storage.execute_readonly("SELECT 1; DROP TABLE task_runs")

    async def test_case_insensitive_rejection(self, storage) -> None:
        with pytest.raises(ValueError, match="Only SELECT"):
            await storage.execute_readonly("drop TABLE task_runs")

    async def test_safe_pragma_allowed(self, storage) -> None:
        rows = await storage.execute_readonly("PRAGMA journal_mode")
        assert rows[0]["journal_mode"] == "wal"

    async def test_safe_pragma_table_info_allowed(self, storage) -> None:
        rows = await storage.execute_readonly("PRAGMA table_info(task_runs)")
        assert len(rows) > 0

    async def test_dangerous_pragma_rejected(self, storage) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            await storage.execute_readonly("PRAGMA writable_schema = ON")

    async def test_dangerous_pragma_journal_delete_rejected(self, storage) -> None:
        with pytest.raises(ValueError, match="Write PRAGMAs are not allowed"):
            await storage.execute_readonly("PRAGMA journal_mode = DELETE")

    async def test_wal_checkpoint_rejected(self, storage) -> None:
        """wal_checkpoint performs writes — must be rejected."""
        with pytest.raises(ValueError, match="not allowed"):
            await storage.execute_readonly("PRAGMA wal_checkpoint")

    async def test_pragma_parenthesis_write_rejected(self, storage) -> None:
        """PRAGMA name(value) is equivalent to PRAGMA name = value — must be rejected."""
        with pytest.raises(ValueError, match="not allowed"):
            await storage.execute_readonly("PRAGMA journal_mode(DELETE)")

    async def test_cte_with_select_allowed(self, storage) -> None:
        """WITH ... SELECT (CTE) is a valid read-only query."""
        rows = await storage.execute_readonly(
            "WITH daily AS (SELECT date(finished_at, 'unixepoch') as d, SUM(cost_usd) as cost FROM task_runs GROUP BY d) SELECT * FROM daily"
        )
        assert isinstance(rows, list)

    async def test_cte_with_write_rejected(self, storage) -> None:
        """WITH ... INSERT/DELETE must still be rejected."""
        with pytest.raises(ValueError, match="Only SELECT"):
            await storage.execute_readonly(
                "WITH ids AS (SELECT id FROM task_runs) DELETE FROM task_runs WHERE id IN (SELECT id FROM ids)"
            )


class TestV2MigrationTables:
    async def test_proposals_table_exists(self, storage) -> None:
        rows = await storage.execute_readonly("SELECT name FROM sqlite_master WHERE type='table' AND name='proposals'")
        assert len(rows) == 1

    async def test_recovery_tables_exist(self, storage) -> None:
        rows = await storage.execute_readonly(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('recovery_states', 'recovery_attempts') ORDER BY name"
        )
        names = {r["name"] for r in rows}
        assert "recovery_states" in names
        assert "recovery_attempts" in names


class TestProposalsCRUD:
    async def test_record_and_query(self, storage) -> None:
        from orchestrator.stats_models import ProposalRecord

        record = ProposalRecord(
            proposal_id="prop-1",
            source_task_key="QR-10",
            summary="Add caching layer",
            category="performance",
            status="pending",
            created_at=1_000_000.0,
        )
        await storage.upsert_proposal(record)

        rows = await storage.execute_readonly("SELECT * FROM proposals WHERE proposal_id = 'prop-1'")
        assert len(rows) == 1
        assert rows[0]["summary"] == "Add caching layer"
        assert rows[0]["status"] == "pending"

    async def test_resolve_proposal(self, storage) -> None:
        from orchestrator.stats_models import ProposalRecord

        await storage.upsert_proposal(
            ProposalRecord(
                proposal_id="prop-2",
                source_task_key="QR-11",
                summary="Add tests",
                category="quality",
                status="pending",
                created_at=1_000_000.0,
            )
        )
        await storage.resolve_proposal("prop-2", "approved", 1_000_100.0)

        rows = await storage.execute_readonly("SELECT status, resolved_at FROM proposals WHERE proposal_id = 'prop-2'")
        assert rows[0]["status"] == "approved"
        assert rows[0]["resolved_at"] == 1_000_100.0

    async def test_get_proposal_stats(self, storage) -> None:
        from orchestrator.stats_models import ProposalRecord

        base = 1_000_000.0
        for i, status in enumerate(["pending", "approved", "approved", "rejected"]):
            await storage.upsert_proposal(
                ProposalRecord(
                    proposal_id=f"prop-s{i}",
                    source_task_key=f"QR-{i}",
                    summary=f"Proposal {i}",
                    category="improvement",
                    status=status,
                    created_at=base + i,
                )
            )

        result = await storage.get_proposal_stats(since=base)
        assert result["pending"] == 1
        assert result["approved"] == 2
        assert result["rejected"] == 1


class TestCloseIdempotent:
    async def test_double_close_does_not_raise(self, tmp_path) -> None:
        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(str(tmp_path / "double_close.db"))
        await db.open()
        await db.close()
        await db.close()  # should not raise


class TestCloseAndReopen:
    async def test_data_persists(self, tmp_path) -> None:
        from orchestrator.sqlite_storage import SQLiteStorage
        from orchestrator.stats_models import TaskRun

        db_path = str(tmp_path / "persist.db")
        db = SQLiteStorage(db_path)
        await db.open()

        now = time.time()
        await db.record_task_run(
            TaskRun(
                task_key="QR-persist",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now,
                finished_at=now,
            )
        )
        await db.close()

        db2 = SQLiteStorage(db_path)
        await db2.open()
        rows = await db2.execute_readonly("SELECT * FROM task_runs WHERE task_key = 'QR-persist'")
        assert len(rows) == 1
        await db2.close()


class TestEventPersistence:
    async def test_save_and_load_event(self, storage):
        await storage.save_event("task_started", "QR-1", '{"model": "sonnet"}', 1000.0)
        events = await storage.load_events_for_task("QR-1")
        assert len(events) == 1
        assert events[0]["type"] == "task_started"
        assert events[0]["task_key"] == "QR-1"
        assert events[0]["data"] == '{"model": "sonnet"}'
        assert events[0]["timestamp"] == 1000.0

    async def test_save_events_batch(self, storage):
        batch = [
            ("task_started", "QR-1", "{}", 100.0),
            ("task_completed", "QR-1", '{"cost": 0.5}', 200.0),
            ("task_started", "QR-2", "{}", 150.0),
        ]
        await storage.save_events_batch(batch)
        events_1 = await storage.load_events_for_task("QR-1")
        assert len(events_1) == 2
        events_2 = await storage.load_events_for_task("QR-2")
        assert len(events_2) == 1

    async def test_load_recent_events_limit(self, storage):
        batch = [("task_started", f"QR-{i}", "{}", float(i)) for i in range(20)]
        await storage.save_events_batch(batch)
        events = await storage.load_recent_events(limit=5)
        assert len(events) == 5
        # Oldest first within the 5 most recent
        assert events[0]["timestamp"] == 15.0
        assert events[-1]["timestamp"] == 19.0

    async def test_delete_old_events(self, storage):
        await storage.save_events_batch(
            [
                ("task_started", "QR-1", "{}", 100.0),
                ("task_started", "QR-2", "{}", 200.0),
                ("task_started", "QR-3", "{}", 300.0),
            ]
        )
        deleted = await storage.delete_old_events(before_timestamp=250.0)
        assert deleted == 2
        remaining = await storage.load_recent_events()
        assert len(remaining) == 1
        assert remaining[0]["task_key"] == "QR-3"

    async def test_events_table_created(self, storage):
        rows = await storage.execute_readonly("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
        assert len(rows) == 1


class TestHasSuccessfulTaskRun:
    """Tests for has_successful_task_run — preflight check source of truth."""

    async def test_returns_true_when_successful_run_exists(self, storage):
        from orchestrator.stats_models import TaskRun

        now = time.time()
        await storage.record_task_run(
            TaskRun(
                task_key="QR-100",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url="https://github.com/org/repo/pull/1",
                needs_info=False,
                resumed=False,
                started_at=now - 60,
                finished_at=now,
            )
        )

        result = await storage.has_successful_task_run("QR-100")
        assert result is True

    async def test_returns_false_when_no_runs_exist(self, storage):
        result = await storage.has_successful_task_run("QR-999")
        assert result is False

    async def test_returns_false_when_only_failed_runs(self, storage):
        from orchestrator.stats_models import TaskRun

        now = time.time()
        await storage.record_task_run(
            TaskRun(
                task_key="QR-101",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=False,
                error_category="timeout",
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 60,
                finished_at=now,
            )
        )

        result = await storage.has_successful_task_run("QR-101")
        assert result is False

    async def test_returns_true_with_mixed_runs(self, storage):
        """One failed + one successful → True."""
        from orchestrator.stats_models import TaskRun

        now = time.time()
        # Failed run
        await storage.record_task_run(
            TaskRun(
                task_key="QR-102",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=False,
                error_category="timeout",
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 120,
                finished_at=now - 60,
            )
        )
        # Successful run
        await storage.record_task_run(
            TaskRun(
                task_key="QR-102",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 60,
                finished_at=now,
            )
        )

        result = await storage.has_successful_task_run("QR-102")
        assert result is True


class TestHasUnmergedPR:
    """Tests for has_unmerged_pr — preflight unmerged PR check."""

    async def test_returns_true_when_unmerged_pr_exists(self, storage):
        from orchestrator.stats_models import PRTrackingData

        now = time.time()
        await storage.record_pr_tracked("QR-200", "https://github.com/org/repo/pull/1", now)
        # PR is still actively tracked (pr_tracking row exists)
        await storage.upsert_pr_tracking(
            PRTrackingData(
                task_key="QR-200",
                pr_url="https://github.com/org/repo/pull/1",
                issue_summary="Test",
                seen_thread_ids=[],
                seen_failed_checks=[],
            )
        )

        result = await storage.has_unmerged_pr("QR-200")
        assert result is True

    async def test_returns_false_when_pr_is_merged(self, storage):
        now = time.time()
        await storage.record_pr_tracked("QR-201", "https://github.com/org/repo/pull/2", now)
        await storage.record_pr_merged("QR-201", "https://github.com/org/repo/pull/2", now + 3600)

        result = await storage.has_unmerged_pr("QR-201")
        assert result is False

    async def test_returns_false_when_no_pr_exists(self, storage):
        result = await storage.has_unmerged_pr("QR-999")
        assert result is False

    async def test_returns_false_when_pr_closed_without_merge(self, storage):
        """A PR tracked but then closed (pr_tracking deleted, merged_at NULL)
        should return False — the PR is no longer active."""
        now = time.time()
        await storage.record_pr_tracked("QR-202", "https://github.com/org/repo/pull/3", now)
        # Simulate PR close: pr_tracking row deleted (pr_monitor.cleanup)
        await storage.delete_pr_tracking("QR-202")

        result = await storage.has_unmerged_pr("QR-202")
        assert result is False


class TestGetLatestSessionId:
    """Tests for get_latest_session_id — session resumption lookup."""

    async def test_returns_none_when_no_runs(self, storage):
        result = await storage.get_latest_session_id("QR-999")
        assert result is None

    async def test_returns_none_when_no_session_id(self, storage):
        from orchestrator.stats_models import TaskRun

        now = time.time()
        await storage.record_task_run(
            TaskRun(
                task_key="QR-300",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 60,
                finished_at=now,
            )
        )

        result = await storage.get_latest_session_id("QR-300")
        assert result is None

    async def test_returns_most_recent_session_id(self, storage):
        from orchestrator.stats_models import TaskRun

        now = time.time()
        # Older run with session
        await storage.record_task_run(
            TaskRun(
                task_key="QR-301",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 120,
                finished_at=now - 60,
                session_id="old-session",
            )
        )
        # Newer run with session
        await storage.record_task_run(
            TaskRun(
                task_key="QR-301",
                model="sonnet",
                cost_usd=1.0,
                duration_seconds=60.0,
                success=True,
                error_category=None,
                pr_url=None,
                needs_info=False,
                resumed=False,
                started_at=now - 60,
                finished_at=now,
                session_id="new-session",
            )
        )

        result = await storage.get_latest_session_id("QR-301")
        assert result == "new-session"


class TestPRTrackingSeenMergeConflict:
    """seen_merge_conflict must round-trip through upsert/load."""

    async def test_seen_merge_conflict_persisted(self, storage):
        from orchestrator.stats_models import PRTrackingData

        data = PRTrackingData(
            task_key="QR-1",
            pr_url="https://github.com/org/repo/pull/1",
            issue_summary="Fix bug",
            seen_thread_ids=["t1"],
            seen_failed_checks=["c1"],
            session_id="sess-1",
            seen_merge_conflict=True,
        )
        await storage.upsert_pr_tracking(data)
        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].seen_merge_conflict is True

    async def test_seen_merge_conflict_default_false(self, storage):
        from orchestrator.stats_models import PRTrackingData

        data = PRTrackingData(
            task_key="QR-2",
            pr_url="https://github.com/org/repo/pull/2",
            issue_summary="Add feature",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
            seen_merge_conflict=False,
        )
        await storage.upsert_pr_tracking(data)
        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].seen_merge_conflict is False

    async def test_seen_merge_conflict_update(self, storage):
        from orchestrator.stats_models import PRTrackingData

        data = PRTrackingData(
            task_key="QR-3",
            pr_url="https://github.com/org/repo/pull/3",
            issue_summary="Refactor",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
            seen_merge_conflict=False,
        )
        await storage.upsert_pr_tracking(data)
        # Update to True
        data.seen_merge_conflict = True
        await storage.upsert_pr_tracking(data)
        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].seen_merge_conflict is True

    async def test_merge_conflict_retries_persisted(self, storage):
        from orchestrator.stats_models import PRTrackingData

        data = PRTrackingData(
            task_key="QR-retry-1",
            pr_url="https://github.com/org/repo/pull/10",
            issue_summary="Retry test",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
            seen_merge_conflict=True,
            merge_conflict_retries=2,
        )
        await storage.upsert_pr_tracking(data)
        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].merge_conflict_retries == 2

    async def test_merge_conflict_retries_default_zero(self, storage):
        from orchestrator.stats_models import PRTrackingData

        data = PRTrackingData(
            task_key="QR-retry-2",
            pr_url="https://github.com/org/repo/pull/11",
            issue_summary="Default test",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
        )
        await storage.upsert_pr_tracking(data)
        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].merge_conflict_retries == 0

    async def test_merge_conflict_retries_update(self, storage):
        from orchestrator.stats_models import PRTrackingData

        data = PRTrackingData(
            task_key="QR-retry-3",
            pr_url="https://github.com/org/repo/pull/12",
            issue_summary="Update test",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
            merge_conflict_retries=0,
        )
        await storage.upsert_pr_tracking(data)
        data.merge_conflict_retries = 3
        await storage.upsert_pr_tracking(data)
        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].merge_conflict_retries == 3


class TestPRTrackingFiltering:
    """Tests for task_key filtering in load_pr_tracking."""

    async def test_load_pr_tracking_with_task_key_filter(self, storage):
        """load_pr_tracking returns only matching task_key when filtered."""
        from orchestrator.stats_models import PRTrackingData

        # Create multiple PR tracking records (each task has one PR)
        data1 = PRTrackingData(
            task_key="QR-100",
            pr_url="https://github.com/org/repo/pull/100",
            issue_summary="Task 100",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
        )
        data2 = PRTrackingData(
            task_key="QR-101",
            pr_url="https://github.com/org/repo/pull/101",
            issue_summary="Task 101",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
        )
        data3 = PRTrackingData(
            task_key="QR-102",
            pr_url="https://github.com/org/repo/pull/102",
            issue_summary="Task 102",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
        )
        await storage.upsert_pr_tracking(data1)
        await storage.upsert_pr_tracking(data2)
        await storage.upsert_pr_tracking(data3)

        # Load all
        all_prs = await storage.load_pr_tracking()
        assert len(all_prs) == 3

        # Load filtered by QR-100
        filtered = await storage.load_pr_tracking(task_key="QR-100")
        assert len(filtered) == 1
        assert filtered[0].task_key == "QR-100"

        # Load filtered by QR-101
        filtered = await storage.load_pr_tracking(task_key="QR-101")
        assert len(filtered) == 1
        assert filtered[0].task_key == "QR-101"

        # Load filtered by non-existent task
        filtered = await storage.load_pr_tracking(task_key="QR-999")
        assert len(filtered) == 0


class TestCancelledPRExclusion:
    """F3: cancelled PRs excluded from active queries."""

    async def test_record_pr_cancelled(self, storage) -> None:
        """record_pr_cancelled sets cancelled_at on latest unmerged PR."""
        now = time.time()
        await storage.record_pr_tracked("QR-C1", "https://pr/1", now)

        await storage.record_pr_cancelled("QR-C1", now + 100)

        rows = await storage.execute_readonly("SELECT cancelled_at FROM pr_lifecycle WHERE task_key = 'QR-C1'")
        assert len(rows) == 1
        assert rows[0]["cancelled_at"] == now + 100

    async def test_has_unmerged_pr_excludes_cancelled(self, storage) -> None:
        """has_unmerged_pr returns False for cancelled PRs."""
        from orchestrator.stats_models import PRTrackingData

        now = time.time()
        await storage.record_pr_tracked("QR-C2", "https://pr/2", now)
        await storage.upsert_pr_tracking(
            PRTrackingData(
                task_key="QR-C2",
                pr_url="https://pr/2",
                issue_summary="Test",
                seen_thread_ids=[],
                seen_failed_checks=[],
            )
        )

        # Before cancel
        assert await storage.has_unmerged_pr("QR-C2") is True

        # After cancel
        await storage.record_pr_cancelled("QR-C2", now + 100)
        assert await storage.has_unmerged_pr("QR-C2") is False

    async def test_record_pr_merged_skips_cancelled(self, storage) -> None:
        """record_pr_merged does not update a cancelled PR."""
        now = time.time()
        await storage.record_pr_tracked("QR-C3", "https://pr/3", now)
        await storage.record_pr_cancelled("QR-C3", now + 50)

        # Try to merge the cancelled PR — should not update
        await storage.record_pr_merged("QR-C3", "https://pr/3", now + 100)

        rows = await storage.execute_readonly(
            "SELECT merged_at, cancelled_at FROM pr_lifecycle WHERE task_key = 'QR-C3'"
        )
        assert len(rows) == 1
        assert rows[0]["cancelled_at"] == now + 50
        assert rows[0]["merged_at"] is None

    async def test_record_pr_merged_marks_wrong_pr_when_multiple_prs(self, storage) -> None:
        """Bug: record_pr_merged marks the newest PR instead of the specific one.

        Scenario:
        - Task has an older merged PR (tracked at T1)
        - Task has a newer conflicting PR (tracked at T2 > T1)
        - When checking the older PR and finding it merged on GitHub
        - Calling record_pr_merged(task_key, timestamp) incorrectly marks
          the newer PR as merged (because it's the most recent by tracked_at)
        """
        now = time.time()
        older_pr = "https://github.com/org/repo/pull/1"
        newer_pr = "https://github.com/org/repo/pull/2"

        # Track older PR first
        await storage.record_pr_tracked("QR-MULTI", older_pr, now)
        # Track newer PR later
        await storage.record_pr_tracked("QR-MULTI", newer_pr, now + 100)

        # Verify both PRs exist and are unmerged
        rows = await storage.execute_readonly(
            "SELECT pr_url, merged_at, tracked_at FROM pr_lifecycle WHERE task_key = 'QR-MULTI' ORDER BY tracked_at"
        )
        assert len(rows) == 2
        assert rows[0]["pr_url"] == older_pr
        assert rows[0]["merged_at"] is None
        assert rows[1]["pr_url"] == newer_pr
        assert rows[1]["merged_at"] is None

        # Check the older PR on GitHub and find it's merged
        # Call record_pr_merged with the specific PR URL
        await storage.record_pr_merged("QR-MULTI", older_pr, now + 200)

        # Verify the bug: the newer PR is marked as merged instead of the older one
        rows = await storage.execute_readonly(
            "SELECT pr_url, merged_at FROM pr_lifecycle WHERE task_key = 'QR-MULTI' ORDER BY tracked_at"
        )
        assert len(rows) == 2
        # BUG: older PR should be marked but isn't
        assert rows[0]["pr_url"] == older_pr
        # This assertion will fail with the current buggy implementation
        # because the function marks the most recent PR (newer_pr), not the specific one
        assert rows[0]["merged_at"] is not None, "Older PR should be marked as merged"
        # BUG: newer PR is incorrectly marked
        assert rows[1]["pr_url"] == newer_pr
        assert rows[1]["merged_at"] is None, "Newer PR should remain unmerged"


class TestPRLifecycleDedupBug:
    """Bug: duplicate pr_lifecycle rows cause has_unmerged_pr
    to return True even after the PR was merged.

    Root cause: record_pr_tracked() uses plain INSERT,
    allowing multiple rows per (task_key, pr_url).
    record_pr_merged() only updates the latest row,
    leaving older duplicates with merged_at IS NULL.
    """

    async def test_record_pr_tracked_dedup_same_url(
        self,
        storage,
    ) -> None:
        """Two inserts with same (task_key, pr_url) produce
        one row with the latest tracked_at."""
        t1 = 1000.0
        t2 = 2000.0
        await storage.record_pr_tracked(
            "QR-DUP",
            "https://pr/1",
            t1,
        )
        await storage.record_pr_tracked(
            "QR-DUP",
            "https://pr/1",
            t2,
        )

        rows = await storage.execute_readonly(
            "SELECT tracked_at FROM pr_lifecycle WHERE task_key = 'QR-DUP' AND pr_url = 'https://pr/1'",
        )
        assert len(rows) == 1
        assert rows[0]["tracked_at"] == t2

    async def test_has_unmerged_pr_false_after_merge_retrack(
        self,
        storage,
    ) -> None:
        """Bug scenario: track → merge → retrack → should
        still be considered merged."""
        from orchestrator.stats_models import PRTrackingData

        t1 = 1000.0
        t2 = 2000.0
        t3 = 3000.0
        pr_url = "https://pr/84"

        # 1. Track PR
        await storage.record_pr_tracked(
            "QR-BUG",
            pr_url,
            t1,
        )
        # 2. Merge PR
        await storage.record_pr_merged("QR-BUG", pr_url, t2)

        # 3. Re-dispatch creates pr_tracking + re-track
        await storage.upsert_pr_tracking(
            PRTrackingData(
                task_key="QR-BUG",
                pr_url=pr_url,
                issue_summary="Test",
                seen_thread_ids=[],
                seen_failed_checks=[],
            ),
        )
        await storage.record_pr_tracked(
            "QR-BUG",
            pr_url,
            t3,
        )

        # Must be False — PR was already merged
        assert (
            await storage.has_unmerged_pr(
                "QR-BUG",
            )
            is False
        )

    async def test_upsert_preserves_merged_at(
        self,
        storage,
    ) -> None:
        """Re-tracking a merged PR must not clear merged_at."""
        t1 = 1000.0
        t2 = 2000.0
        t3 = 3000.0
        pr_url = "https://pr/99"

        await storage.record_pr_tracked(
            "QR-M",
            pr_url,
            t1,
        )
        await storage.record_pr_merged("QR-M", pr_url, t2)
        await storage.record_pr_tracked(
            "QR-M",
            pr_url,
            t3,
        )

        rows = await storage.execute_readonly(
            "SELECT merged_at, tracked_at FROM pr_lifecycle WHERE task_key = 'QR-M'",
        )
        assert len(rows) == 1
        assert rows[0]["merged_at"] == t2
        assert rows[0]["tracked_at"] == t3

    async def test_different_pr_urls_allowed(
        self,
        storage,
    ) -> None:
        """Different PR URLs for the same task are legitimate
        — two rows must be kept."""
        t1 = 1000.0
        t2 = 2000.0

        await storage.record_pr_tracked(
            "QR-MULTI",
            "https://pr/1",
            t1,
        )
        await storage.record_pr_tracked(
            "QR-MULTI",
            "https://pr/2",
            t2,
        )

        rows = await storage.execute_readonly(
            "SELECT pr_url FROM pr_lifecycle WHERE task_key = 'QR-MULTI'",
        )
        assert len(rows) == 2

    async def test_retrack_clears_cancelled_at(
        self,
        storage,
    ) -> None:
        """Re-tracking a cancelled PR clears cancelled_at
        so the PR becomes visible again."""
        from orchestrator.stats_models import PRTrackingData

        pr_url = "https://pr/reopen"

        await storage.record_pr_tracked(
            "QR-REOPEN",
            pr_url,
            1000.0,
        )
        await storage.record_pr_cancelled(
            "QR-REOPEN",
            2000.0,
        )

        # Re-track same URL (PR reopened)
        await storage.record_pr_tracked(
            "QR-REOPEN",
            pr_url,
            3000.0,
        )
        await storage.upsert_pr_tracking(
            PRTrackingData(
                task_key="QR-REOPEN",
                pr_url=pr_url,
                issue_summary="Test",
                seen_thread_ids=[],
                seen_failed_checks=[],
            ),
        )

        # cancelled_at must be cleared
        rows = await storage.execute_readonly(
            "SELECT cancelled_at, tracked_at FROM pr_lifecycle WHERE task_key = 'QR-REOPEN'",
        )
        assert len(rows) == 1
        assert rows[0]["cancelled_at"] is None
        assert rows[0]["tracked_at"] == 3000.0

        # PR must be visible as unmerged
        assert (
            await storage.has_unmerged_pr(
                "QR-REOPEN",
            )
            is True
        )

    async def _create_db_with_dupes(
        self,
        db_path: str,
        rows: list[tuple[str, ...]],
    ) -> None:
        """Helper: create DB at v18, insert duplicate rows."""
        import aiosqlite

        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(db_path)
        await db.open()
        await db.close()

        async with aiosqlite.connect(db_path) as raw:
            raw.row_factory = aiosqlite.Row
            await raw.execute(
                "DROP INDEX IF EXISTS uq_pr_lifecycle_task_pr",
            )
            await raw.execute("PRAGMA user_version = 18")
            for sql in rows:
                await raw.execute(sql[0], sql[1:] if len(sql) > 1 else ())
            await raw.commit()

    async def test_migration_aggregates_scattered_fields(
        self,
        tmp_path,
    ) -> None:
        """Migration 0019 must merge columns from duplicate
        rows before deleting them.

        Scenario: Row A has merged_at (2 iters, 1 fail),
        Row B has verified_at (3 iters, 2 fails). After
        migration the kept row must have all fields with
        SUM'd counters.
        """
        from orchestrator.sqlite_storage import SQLiteStorage

        db_path = str(tmp_path / "agg.db")
        await self._create_db_with_dupes(
            db_path,
            [
                (
                    "INSERT INTO pr_lifecycle"
                    " (task_key, pr_url, tracked_at,"
                    "  merged_at, verified_at,"
                    "  review_iterations, ci_failures)"
                    " VALUES"
                    " ('QR-AGG', 'https://pr/1', 1000,"
                    "  1500, NULL, 2, 1)",
                ),
                (
                    "INSERT INTO pr_lifecycle"
                    " (task_key, pr_url, tracked_at,"
                    "  merged_at, verified_at,"
                    "  review_iterations, ci_failures)"
                    " VALUES"
                    " ('QR-AGG', 'https://pr/1', 2000,"
                    "  NULL, 2500, 3, 2)",
                ),
            ],
        )

        db2 = SQLiteStorage(db_path)
        await db2.open()

        rows = await db2.execute_readonly(
            "SELECT merged_at, verified_at, review_iterations, ci_failures FROM pr_lifecycle WHERE task_key = 'QR-AGG'",
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["merged_at"] == 1500
        assert row["verified_at"] == 2500
        # SUM of counters: 2+3=5, 1+2=3
        assert row["review_iterations"] == 5
        assert row["ci_failures"] == 3

        await db2.close()

    async def test_migration_merged_wins_over_cancelled(
        self,
        tmp_path,
    ) -> None:
        """merged_at and cancelled_at are mutually exclusive.
        If any duplicate has merged_at, cancelled_at must be
        cleared — merge is a stronger terminal state.
        """
        from orchestrator.sqlite_storage import SQLiteStorage

        db_path = str(tmp_path / "merge_cancel.db")
        await self._create_db_with_dupes(
            db_path,
            [
                (
                    "INSERT INTO pr_lifecycle"
                    " (task_key, pr_url, tracked_at,"
                    "  merged_at, cancelled_at)"
                    " VALUES"
                    " ('QR-MC', 'https://pr/1', 1000,"
                    "  NULL, 900)",
                ),
                (
                    "INSERT INTO pr_lifecycle"
                    " (task_key, pr_url, tracked_at,"
                    "  merged_at, cancelled_at)"
                    " VALUES"
                    " ('QR-MC', 'https://pr/1', 2000,"
                    "  1500, NULL)",
                ),
            ],
        )

        db2 = SQLiteStorage(db_path)
        await db2.open()

        rows = await db2.execute_readonly(
            "SELECT merged_at, cancelled_at FROM pr_lifecycle WHERE task_key = 'QR-MC'",
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["merged_at"] == 1500
        # cancelled_at cleared — merge wins
        assert row["cancelled_at"] is None

        await db2.close()
