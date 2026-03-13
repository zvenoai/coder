"""Tests for NeedsInfoMonitor — PR review comment regressions."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from orchestrator.config import Config, ReposConfig
from orchestrator.needs_info_monitor import NeedsInfoMonitor, TrackedNeedsInfo
from orchestrator.tracker_tools import ToolState
from orchestrator.workspace_tools import WorkspaceState


def make_config(**overrides) -> Config:
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(),
        needs_info_check_delay_seconds=0,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_monitor(**overrides) -> tuple[NeedsInfoMonitor, dict[str, MagicMock]]:
    """Create a NeedsInfoMonitor with all mocked dependencies."""
    mocks = {
        "tracker": MagicMock(),
        "event_bus": AsyncMock(),
        "proposal_manager": AsyncMock(),
        "track_pr": MagicMock(),
        "cleanup_worktrees": MagicMock(),
        "get_latest_comment_id": MagicMock(return_value=0),
    }

    kwargs = dict(
        tracker=mocks["tracker"],
        event_bus=mocks["event_bus"],
        proposal_manager=mocks["proposal_manager"],
        config=make_config(),
        semaphore=asyncio.Semaphore(1),
        session_locks={},
        shutdown_event=asyncio.Event(),
        bot_login="bot",
        track_pr_callback=mocks["track_pr"],
        cleanup_worktrees_callback=mocks["cleanup_worktrees"],
        get_latest_comment_id_callback=mocks["get_latest_comment_id"],
        dispatched_set=set(),
    )
    kwargs.update(overrides)

    monitor = NeedsInfoMonitor(**kwargs)
    return monitor, mocks


def make_tracked_ni(issue_key: str = "QR-1") -> TrackedNeedsInfo:
    return TrackedNeedsInfo(
        issue_key=issue_key,
        session=AsyncMock(),
        workspace_state=WorkspaceState(issue_key=issue_key),
        tool_state=ToolState(),
        last_check_at=0,
        last_seen_comment_id=0,
        issue_summary="Test task",
    )


class TestSessionIdPersistence:
    """session_id persistence in NeedsInfoMonitor."""

    async def test_persist_ni_tracking_includes_session_id(self) -> None:
        """_persist_tracking should include session_id from the session."""
        from orchestrator.stats_models import NeedsInfoTrackingRecord

        storage = AsyncMock()
        monitor, _ = make_monitor(storage=storage)

        ni = make_tracked_ni("QR-NI-SES")
        ni.session.session_id = "ses-ni-persist"
        monitor.add(ni)

        # Drain background tasks
        await monitor.drain_background_tasks()

        # Verify storage.upsert_needs_info_tracking was called with session_id
        storage.upsert_needs_info_tracking.assert_awaited_once()
        record = storage.upsert_needs_info_tracking.call_args[0][0]
        assert isinstance(record, NeedsInfoTrackingRecord)
        assert record.session_id == "ses-ni-persist"

    async def test_get_persisted_session_id_returns_session_id(self) -> None:
        """get_persisted_session_id returns session_id from loaded data."""
        from orchestrator.stats_models import NeedsInfoTrackingRecord

        storage = AsyncMock()
        storage.load_needs_info_tracking.return_value = [
            NeedsInfoTrackingRecord(
                issue_key="QR-NI-1",
                last_seen_comment_id=10,
                issue_summary="Test",
                tracked_at=1.0,
                session_id="ses-ni-loaded",
            )
        ]
        monitor, _ = make_monitor(storage=storage)
        await monitor.load()

        assert monitor.get_persisted_session_id("QR-NI-1") == "ses-ni-loaded"
        assert monitor.get_persisted_session_id("QR-NONE") is None

    async def test_get_persisted_session_id_none_when_no_session(self) -> None:
        """get_persisted_session_id returns None when session_id is not stored."""
        from orchestrator.stats_models import NeedsInfoTrackingRecord

        storage = AsyncMock()
        storage.load_needs_info_tracking.return_value = [
            NeedsInfoTrackingRecord(
                issue_key="QR-NI-2",
                last_seen_comment_id=5,
                issue_summary="Test",
                tracked_at=1.0,
                session_id=None,
            )
        ]
        monitor, _ = make_monitor(storage=storage)
        await monitor.load()

        assert monitor.get_persisted_session_id("QR-NI-2") is None


class TestFailureRecording:
    """PR review: _handle_failure must record failure to prevent infinite retry loop.

    Without record_failure, discarded tasks get re-dispatched every poll cycle
    with no backoff — same bug as empty repos early return in task_dispatcher.
    """

    async def test_handle_failure_records_failure(self) -> None:
        record_failure = MagicMock()
        monitor, _mocks = make_monitor(record_failure_callback=record_failure)
        ni = make_tracked_ni("QR-1")
        monitor.add(ni)

        await monitor._handle_failure("QR-1", ni, error="agent crashed")

        record_failure.assert_called_once_with("QR-1", "agent crashed")


class TestFailureRetryability:
    """PR review: _handle_failure ignores recovery state retryability check.

    Reference: main.py:_handle_agent_result uses state.should_retry to decide:
    - should_retry=True → discard from dispatched (allow re-dispatch)
    - should_retry=False → permanent failure, do NOT discard
    Currently _handle_failure unconditionally does BOTH, which is wrong.
    """

    async def test_retryable_failure_discards_from_dispatched(self) -> None:
        """When should_retry=True, task must be discarded to allow re-dispatch."""
        state = MagicMock(should_retry=True)
        record_failure = MagicMock(return_value=state)
        dispatched = {"QR-1"}
        monitor, _mocks = make_monitor(
            record_failure_callback=record_failure,
            dispatched_set=dispatched,
        )
        ni = make_tracked_ni("QR-1")
        monitor.add(ni)

        await monitor._handle_failure("QR-1", ni, error="transient error")

        assert "QR-1" not in dispatched

    async def test_permanent_failure_does_not_discard_from_dispatched(self) -> None:
        """When should_retry=False (permanent), task must NOT be discarded."""
        state = MagicMock(should_retry=False)
        record_failure = MagicMock(return_value=state)
        dispatched = {"QR-1"}
        monitor, _mocks = make_monitor(
            record_failure_callback=record_failure,
            dispatched_set=dispatched,
        )
        ni = make_tracked_ni("QR-1")
        monitor.add(ni)

        await monitor._handle_failure("QR-1", ni, error="permanent error")

        assert "QR-1" in dispatched


class TestPrCreatedClearsRecovery:
    """Needs-info → PR path must clear recovery state to avoid stale no-PR counts."""

    async def test_handle_pr_created_clears_recovery(self) -> None:
        clear_recovery = MagicMock()
        monitor, _mocks = make_monitor(clear_recovery_callback=clear_recovery)
        ni = make_tracked_ni("QR-1")
        monitor.add(ni)

        await monitor._handle_pr_created("QR-1", ni, pr_url="https://github.com/o/r/pull/1", cost=1.0, duration=60.0)

        clear_recovery.assert_called_once_with("QR-1")


class TestSuccessCompletesTask:
    """Needs-info → success without PR completes the task (agent-driven completion)."""

    async def test_handle_success_closes_session_and_publishes(self) -> None:
        """_handle_success should close session, clean up, and publish TASK_COMPLETED."""
        monitor, _mocks = make_monitor()
        ni = make_tracked_ni("QR-1")
        monitor.add(ni)

        await monitor._handle_success("QR-1", ni, cost=0.5, duration=30.0, output="Task done")

        ni.session.close.assert_awaited_once()
        _mocks["cleanup_worktrees"].assert_called_once()


class TestPrTrackedEventIncludesCostDuration:
    """PR_TRACKED event from needs-info path must include cost and duration.

    Bug: _handle_pr_created receives cost/duration parameters but publishes
    PR_TRACKED with only {"pr_url": pr_url}, losing cost/duration data.
    StatsCollector._on_pr_tracked reads data.get("cost") and data.get("duration"),
    so needs-info tasks are recorded with cost_usd=0.0 and duration_seconds=0.0.
    """

    async def test_pr_tracked_event_includes_cost_and_duration(self) -> None:
        monitor, mocks = make_monitor()
        ni = make_tracked_ni("QR-1")
        monitor.add(ni)

        await monitor._handle_pr_created("QR-1", ni, pr_url="https://github.com/o/r/pull/5", cost=2.5, duration=180.0)

        # Find the PR_TRACKED event in published events
        from orchestrator.constants import EventType

        published = mocks["event_bus"].publish.call_args_list
        pr_events = [c for c in published if c[0][0].type == EventType.PR_TRACKED]
        assert len(pr_events) == 1

        data = pr_events[0][0][0].data
        assert data["pr_url"] == "https://github.com/o/r/pull/5"
        assert data["cost"] == 2.5
        assert data["duration"] == 180.0


class TestSuccessDiscardsFromDispatched:
    """Success without PR must discard from dispatched set."""

    async def test_handle_success_discards_from_dispatched(self) -> None:
        """Dispatched set is cleared on success."""
        dispatched = {"QR-1"}
        monitor, _mocks = make_monitor(dispatched_set=dispatched)
        ni = make_tracked_ni("QR-1")
        monitor.add(ni)

        await monitor._handle_success("QR-1", ni, cost=0.5, duration=30.0, output="Done")

        assert "QR-1" not in dispatched


class TestRemovedFlag:
    """F3: removed flag prevents stale operations after remove()."""

    async def test_remove_closes_session_and_cleans_up(self) -> None:
        """remove() closes session and cleans up worktrees."""
        monitor, _mocks = make_monitor()
        ni = make_tracked_ni("QR-RM1")
        monitor.add(ni)

        await monitor.remove("QR-RM1")

        assert ni.removed is True
        ni.session.close.assert_awaited_once()
        assert "QR-RM1" not in monitor._tracked
        _mocks["cleanup_worktrees"].assert_called_once()

    async def test_remove_nonexistent_is_noop(self) -> None:
        """remove() on unknown key should not raise."""
        monitor, _mocks = make_monitor()
        await monitor.remove("QR-MISSING")

    async def test_removed_flag_guards_handle_pr_created(self) -> None:
        """_handle_pr_created should bail early when removed=True."""
        monitor, _mocks = make_monitor()
        ni = make_tracked_ni("QR-RM2")
        ni.removed = True
        monitor._tracked["QR-RM2"] = ni

        await monitor._handle_pr_created(
            "QR-RM2",
            ni,
            pr_url="https://github.com/o/r/pull/1",
            cost=1.0,
            duration=60.0,
        )

        # track_pr should NOT be called
        _mocks["track_pr"].assert_not_called()

    async def test_removed_flag_guards_handle_success(self) -> None:
        """_handle_success should bail early when removed=True."""
        monitor, _mocks = make_monitor()
        ni = make_tracked_ni("QR-RM3")
        ni.removed = True
        monitor._tracked["QR-RM3"] = ni

        await monitor._handle_success(
            "QR-RM3",
            ni,
            cost=0.5,
            duration=30.0,
            output="Done",
        )

        # Session close should NOT be called (already handled by remove)
        ni.session.close.assert_not_awaited()
