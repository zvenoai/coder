"""Tests for PRMonitor SQLite persistence (dedup data)."""

from __future__ import annotations

import pytest

from orchestrator.stats_models import PRTrackingData


@pytest.fixture
async def storage(tmp_path):
    """Create a SQLiteStorage with a temporary database."""
    from orchestrator.sqlite_storage import SQLiteStorage

    db = SQLiteStorage(str(tmp_path / "test_pr.db"))
    await db.open()
    yield db
    await db.close()


class TestPRTrackingStorageCRUD:
    """Low-level storage methods for PR tracking dedup data."""

    async def test_seen_threads_survive_restart(self, storage) -> None:
        """seen_thread_ids persist through write → load cycle."""
        data = PRTrackingData(
            task_key="QR-10",
            pr_url="https://github.com/org/repo/pull/1",
            issue_summary="Fix login",
            seen_thread_ids=["thread-1", "thread-2"],
            seen_failed_checks=[],
        )
        await storage.upsert_pr_tracking(data)

        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert set(loaded[0].seen_thread_ids) == {"thread-1", "thread-2"}

    async def test_seen_checks_survive_restart(self, storage) -> None:
        """seen_failed_checks persist through write → load cycle."""
        data = PRTrackingData(
            task_key="QR-11",
            pr_url="https://github.com/org/repo/pull/2",
            issue_summary="Fix CI",
            seen_thread_ids=[],
            seen_failed_checks=["lint:failure", "test:failure"],
        )
        await storage.upsert_pr_tracking(data)

        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert set(loaded[0].seen_failed_checks) == {"lint:failure", "test:failure"}

    async def test_upsert_updates_existing(self, storage) -> None:
        """Upserting same task_key updates the record."""
        data1 = PRTrackingData(
            task_key="QR-12",
            pr_url="https://github.com/org/repo/pull/3",
            issue_summary="Task",
            seen_thread_ids=["t1"],
            seen_failed_checks=[],
        )
        await storage.upsert_pr_tracking(data1)

        data2 = PRTrackingData(
            task_key="QR-12",
            pr_url="https://github.com/org/repo/pull/3",
            issue_summary="Task",
            seen_thread_ids=["t1", "t2"],
            seen_failed_checks=["check:fail"],
        )
        await storage.upsert_pr_tracking(data2)

        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert set(loaded[0].seen_thread_ids) == {"t1", "t2"}

    async def test_delete_removes_tracking(self, storage) -> None:
        """Deleting removes tracking data."""
        data = PRTrackingData(
            task_key="QR-13",
            pr_url="https://github.com/org/repo/pull/4",
            issue_summary="Task",
            seen_thread_ids=["t1"],
            seen_failed_checks=[],
        )
        await storage.upsert_pr_tracking(data)
        await storage.delete_pr_tracking("QR-13")

        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 0

    async def test_no_duplicate_reviews_after_load(self, storage) -> None:
        """Dedup state loaded from DB prevents re-processing of same thread."""
        # Persist initial state with seen threads
        data = PRTrackingData(
            task_key="QR-14",
            pr_url="https://github.com/org/repo/pull/5",
            issue_summary="Task",
            seen_thread_ids=["thread-A", "thread-B"],
            seen_failed_checks=["check-X:failure"],
        )
        await storage.upsert_pr_tracking(data)

        # Simulate restart: load from DB
        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        restored = loaded[0]

        # These IDs should be recognized as "seen"
        seen_threads = set(restored.seen_thread_ids)
        assert "thread-A" in seen_threads
        assert "thread-B" in seen_threads
        assert "thread-C" not in seen_threads  # new thread would be processed

        seen_checks = set(restored.seen_failed_checks)
        assert "check-X:failure" in seen_checks


class TestPRTrackingSessionId:
    """session_id field roundtrip through SQLite."""

    async def test_pr_tracking_session_id_roundtrip(self, storage) -> None:
        """session_id persists through write → load cycle."""
        data = PRTrackingData(
            task_key="QR-SES-1",
            pr_url="https://github.com/org/repo/pull/10",
            issue_summary="Resume test",
            seen_thread_ids=["t1"],
            seen_failed_checks=[],
            session_id="ses-abc-123",
        )
        await storage.upsert_pr_tracking(data)

        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].session_id == "ses-abc-123"

    async def test_pr_tracking_session_id_null(self, storage) -> None:
        """session_id defaults to None for records without it."""
        data = PRTrackingData(
            task_key="QR-SES-2",
            pr_url="https://github.com/org/repo/pull/11",
            issue_summary="No session",
            seen_thread_ids=[],
            seen_failed_checks=[],
        )
        await storage.upsert_pr_tracking(data)

        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].session_id is None

    async def test_pr_tracking_session_id_updated_on_upsert(self, storage) -> None:
        """session_id updates when upserting existing record."""
        data = PRTrackingData(
            task_key="QR-SES-3",
            pr_url="https://github.com/org/repo/pull/12",
            issue_summary="Update test",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id="ses-old",
        )
        await storage.upsert_pr_tracking(data)

        data.session_id = "ses-new"
        await storage.upsert_pr_tracking(data)

        loaded = await storage.load_pr_tracking()
        assert len(loaded) == 1
        assert loaded[0].session_id == "ses-new"


class TestSessionCloseFailure:
    """PRMonitor must cleanup even when session.close() raises."""

    async def test_cleanup_called_when_session_close_raises(self) -> None:
        """If session.close() raises, cleanup() must still be called and issue removed."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from orchestrator.constants import PRState
        from orchestrator.pr_monitor import PRMonitor, TrackedPR

        # Minimal PRMonitor setup with mocks
        tracker = MagicMock()
        github = MagicMock()
        event_bus = AsyncMock()
        proposal_manager = AsyncMock()
        config = MagicMock()
        config.review_check_delay_seconds = 0
        semaphore = MagicMock()
        session_locks: dict = {}
        shutdown = MagicMock()
        cleanup_called = []

        def track_cleanup(key, paths):
            cleanup_called.append(key)

        monitor = PRMonitor(
            tracker=tracker,
            github=github,
            event_bus=event_bus,
            proposal_manager=proposal_manager,
            config=config,
            semaphore=semaphore,
            session_locks=session_locks,
            shutdown_event=shutdown,
            cleanup_worktrees_callback=track_cleanup,
        )

        # Create a tracked PR with a failing session
        session = AsyncMock()
        session.close.side_effect = RuntimeError("session close failed")
        workspace = MagicMock()
        workspace.repo_paths = []

        pr = TrackedPR(
            issue_key="QR-CLOSE",
            pr_url="https://github.com/org/repo/pull/99",
            owner="org",
            repo="repo",
            pr_number=99,
            session=session,
            workspace_state=workspace,
            last_check_at=0.0,
        )
        monitor._tracked_prs["QR-CLOSE"] = pr

        # Simulate merged PR — session.close() raises but cleanup must still happen
        with pytest.raises(RuntimeError, match="session close failed"):
            with patch.object(monitor, "_event_bus", event_bus):
                await monitor._handle_pr_closed_or_merged("QR-CLOSE", pr, PRState.MERGED)

        # Session.close() was called
        session.close.assert_awaited_once()

        # Cleanup must have been called despite the exception
        assert "QR-CLOSE" in cleanup_called

        # PR must be removed from tracked
        assert "QR-CLOSE" not in monitor._tracked_prs
