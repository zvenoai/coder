"""Tests for orchestrator MCP tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

from orchestrator.config import Config, ReposConfig
from orchestrator.constants import EventType
from orchestrator.event_bus import Event, EventBus
from orchestrator.tracker_tools import ToolState
from orchestrator.workspace_tools import WorkspaceState


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def mock_tracker():
    tracker = MagicMock()
    tracker.add_comment = MagicMock()
    tracker.transition_to_review = MagicMock()
    tracker.create_issue = MagicMock(return_value={"key": "QR-99"})
    return tracker


@pytest.fixture
def mock_pr_monitor():
    monitor = MagicMock()
    monitor.track = MagicMock()
    return monitor


@pytest.fixture
def mock_needs_info_monitor():
    monitor = MagicMock()
    monitor.add = MagicMock()
    return monitor


@pytest.fixture
def mock_recovery():
    recovery = MagicMock()
    recovery.get_state.return_value = MagicMock(
        attempt_count=1,
        attempts=[MagicMock(timestamp=1000.0, category=MagicMock(value="transient"), error_message="test error")],
        should_retry=True,
        backoff_seconds=30.0,
    )
    recovery.record_failure = MagicMock()
    recovery.clear = MagicMock()
    return recovery


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def dispatched():
    return {"QR-42"}


@pytest.fixture
def workspace_state():
    return WorkspaceState(issue_key="QR-42")


@pytest.fixture
def tool_state():
    return ToolState()


@pytest.fixture
def cleanup_worktrees():
    return MagicMock()


@pytest.fixture
def config(tmp_path):
    return Config(
        tracker_token="test-token",
        tracker_org_id="test-org",
        tracker_queue="QR",
        tracker_tag="ai-task",
        tracker_project_id=13,
        tracker_boards=[14],
        workspace_dir=str(tmp_path / "workspace"),
        worktree_base_dir=str(tmp_path / "worktrees"),
        repos_config=ReposConfig(),
        github_token="test-github-token",
    )


@pytest.fixture
def orchestrator_deps(
    event_bus,
    mock_tracker,
    mock_pr_monitor,
    mock_needs_info_monitor,
    mock_recovery,
    mock_session,
    dispatched,
    cleanup_worktrees,
    config,
):
    """Bundle all dependencies needed for orchestrator tools."""
    return {
        "event_bus": event_bus,
        "tracker": mock_tracker,
        "pr_monitor": mock_pr_monitor,
        "needs_info_monitor": mock_needs_info_monitor,
        "recovery": mock_recovery,
        "dispatched_set": dispatched,
        "cleanup_worktrees_callback": cleanup_worktrees,
        "config": config,
    }


@pytest.fixture
def build_tools(orchestrator_deps):
    """Build the orchestrator tools server and return the tool functions."""
    from orchestrator.orchestrator_tools import build_orchestrator_server

    # The server is created via create_sdk_mcp_server which returns "mock_server"
    # We need to access the tool functions directly
    # Since create_sdk_mcp_server is mocked, we can't call tools through it.
    # Instead, test the tool functions by importing and calling them.
    return build_orchestrator_server(**orchestrator_deps)


class TestTrackPrTool:
    """Tests for track_pr terminal tool."""

    @pytest.mark.asyncio
    async def test_track_pr_calls_monitor_and_publishes_event(
        self,
        event_bus,
        mock_tracker,
        mock_pr_monitor,
        mock_recovery,
        dispatched,
        mock_session,
        workspace_state,
        config,
    ):
        from orchestrator.orchestrator_tools import track_pr_impl

        events: list[Event] = []
        q = event_bus.subscribe_task("QR-42")

        await track_pr_impl(
            issue_key="QR-42",
            pr_url="https://github.com/org/repo/pull/123",
            session=mock_session,
            workspace_state=workspace_state,
            event_bus=event_bus,
            tracker=mock_tracker,
            pr_monitor=mock_pr_monitor,
            recovery=mock_recovery,
        )

        mock_tracker.transition_to_review.assert_called_once_with("QR-42")
        mock_pr_monitor.track.assert_called_once_with(
            "QR-42",
            "https://github.com/org/repo/pull/123",
            mock_session,
            workspace_state,
            None,
        )
        mock_recovery.clear.assert_called_once_with("QR-42")

        # Check event published
        event = q.get_nowait()
        assert event.type == EventType.PR_TRACKED
        assert event.data["pr_url"] == "https://github.com/org/repo/pull/123"

    @pytest.mark.asyncio
    async def test_track_pr_includes_cost_duration_resumed(
        self,
        event_bus,
        mock_tracker,
        mock_pr_monitor,
        mock_recovery,
        dispatched,
        mock_session,
        workspace_state,
        config,
    ):
        from orchestrator.orchestrator_tools import track_pr_impl

        q = event_bus.subscribe_task("QR-42")

        await track_pr_impl(
            issue_key="QR-42",
            pr_url="https://github.com/org/repo/pull/123",
            session=mock_session,
            workspace_state=workspace_state,
            event_bus=event_bus,
            tracker=mock_tracker,
            pr_monitor=mock_pr_monitor,
            recovery=mock_recovery,
            cost_usd=1.5,
            duration_seconds=120.0,
            resumed=True,
        )

        event = q.get_nowait()
        assert event.data["cost"] == 1.5
        assert event.data["duration"] == 120.0
        assert event.data["resumed"] is True


class TestCompleteTaskTool:
    """Tests for complete_task terminal tool."""

    @pytest.mark.asyncio
    async def test_complete_task_closes_session_and_publishes(
        self,
        event_bus,
        mock_session,
        workspace_state,
        cleanup_worktrees,
    ):
        from orchestrator.orchestrator_tools import complete_task_impl

        await complete_task_impl(
            issue_key="QR-42",
            summary="Task completed successfully",
            session=mock_session,
            workspace_state=workspace_state,
            event_bus=event_bus,
            cleanup_worktrees_callback=cleanup_worktrees,
        )

        mock_session.close.assert_awaited_once()

        history = event_bus.get_task_history("QR-42")
        assert any(e.type == EventType.TASK_COMPLETED for e in history)


class TestFailTaskTool:
    """Tests for fail_task terminal tool."""

    @pytest.mark.asyncio
    async def test_fail_task_records_failure_and_publishes(
        self,
        event_bus,
        mock_recovery,
        mock_session,
        workspace_state,
        cleanup_worktrees,
    ):
        from orchestrator.orchestrator_tools import fail_task_impl

        await fail_task_impl(
            issue_key="QR-42",
            error="Permanent error: auth failure",
            session=mock_session,
            workspace_state=workspace_state,
            event_bus=event_bus,
            recovery=mock_recovery,
            cleanup_worktrees_callback=cleanup_worktrees,
        )

        mock_recovery.record_failure.assert_called_once_with("QR-42", "Permanent error: auth failure")
        mock_session.close.assert_awaited_once()

        history = event_bus.get_task_history("QR-42")
        assert any(e.type == EventType.TASK_FAILED for e in history)


class TestCreateFollowUpTaskTool:
    """Tests for create_follow_up_task tool."""

    @pytest.mark.asyncio
    async def test_creates_tracker_issue_with_ai_task_tag(
        self,
        mock_tracker,
        config,
    ):
        from orchestrator.orchestrator_tools import create_follow_up_task_impl

        result = await create_follow_up_task_impl(
            summary="Fix remaining tests",
            description="Tests in module X are still failing",
            component="Бекенд",
            assignee="john.doe",
            tracker=mock_tracker,
            config=config,
        )

        mock_tracker.create_issue.assert_called_once()
        call_kwargs = mock_tracker.create_issue.call_args
        # Check ai-task tag included
        assert config.tracker_tag in call_kwargs.kwargs.get("tags", []) or config.tracker_tag in (
            call_kwargs[1].get("tags", []) if len(call_kwargs) > 1 else []
        )
        assert result == "QR-99"


class TestMonitorNeedsInfoTool:
    """Tests for monitor_needs_info terminal tool."""

    @pytest.mark.asyncio
    async def test_monitor_needs_info_adds_to_monitor_and_publishes(
        self,
        event_bus,
        mock_needs_info_monitor,
        mock_tracker,
        mock_session,
        workspace_state,
        tool_state,
    ):
        from orchestrator.orchestrator_tools import monitor_needs_info_impl

        await monitor_needs_info_impl(
            issue_key="QR-42",
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
            event_bus=event_bus,
            needs_info_monitor=mock_needs_info_monitor,
            tracker=mock_tracker,
        )

        mock_needs_info_monitor.add.assert_called_once()

        history = event_bus.get_task_history("QR-42")
        assert any(e.type == EventType.NEEDS_INFO for e in history)


class TestGetTaskHistoryTool:
    """Tests for get_task_history context tool."""

    @pytest.mark.asyncio
    async def test_returns_recovery_state_and_events(
        self,
        event_bus,
        mock_recovery,
    ):
        from orchestrator.orchestrator_tools import get_task_history_impl

        # Publish some events first
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-42", data={"summary": "test"}))

        result = get_task_history_impl(
            issue_key="QR-42",
            event_bus=event_bus,
            recovery=mock_recovery,
        )

        assert "attempt_count" in result
        assert "events" in result
        assert len(result["events"]) >= 1


class TestGetRecentEventsTool:
    """Tests for get_recent_events context tool."""

    @pytest.mark.asyncio
    async def test_returns_recent_events(self, event_bus):
        from orchestrator.orchestrator_tools import get_recent_events_impl

        await event_bus.publish(Event(type=EventType.TASK_COMPLETED, task_key="QR-1", data={}))
        await event_bus.publish(Event(type=EventType.TASK_FAILED, task_key="QR-2", data={}))

        result = get_recent_events_impl(count=10, event_bus=event_bus)
        assert len(result) == 2


class TestCleanupWorktreesInTerminalActions:
    """Verify cleanup_worktrees_callback is called by all terminal tools."""

    @pytest.mark.asyncio
    async def test_complete_calls_cleanup(self, event_bus, mock_session, cleanup_worktrees):
        from orchestrator.orchestrator_tools import complete_task_impl

        ws = WorkspaceState(issue_key="QR-42")
        await complete_task_impl(
            issue_key="QR-42",
            summary="done",
            session=mock_session,
            workspace_state=ws,
            event_bus=event_bus,
            cleanup_worktrees_callback=cleanup_worktrees,
        )
        cleanup_worktrees.assert_called_once_with("QR-42", ws.repo_paths)

    @pytest.mark.asyncio
    async def test_fail_calls_cleanup(self, event_bus, mock_recovery, mock_session, cleanup_worktrees):
        from orchestrator.orchestrator_tools import fail_task_impl

        ws = WorkspaceState(issue_key="QR-42")
        await fail_task_impl(
            issue_key="QR-42",
            error="fatal",
            session=mock_session,
            workspace_state=ws,
            event_bus=event_bus,
            recovery=mock_recovery,
            cleanup_worktrees_callback=cleanup_worktrees,
        )
        cleanup_worktrees.assert_called_once_with("QR-42", ws.repo_paths)

    @pytest.mark.asyncio
    async def test_fail_records_by_default(self, event_bus, mock_recovery, mock_session, cleanup_worktrees):
        """fail_task_impl with skip_record=False (default) calls record_failure."""
        from orchestrator.orchestrator_tools import fail_task_impl

        ws = WorkspaceState(issue_key="QR-42")
        await fail_task_impl(
            issue_key="QR-42",
            error="fatal error",
            session=mock_session,
            workspace_state=ws,
            event_bus=event_bus,
            recovery=mock_recovery,
            cleanup_worktrees_callback=cleanup_worktrees,
        )
        mock_recovery.record_failure.assert_called_once_with("QR-42", "fatal error")

    @pytest.mark.asyncio
    async def test_fail_skip_record(self, event_bus, mock_recovery, mock_session, cleanup_worktrees):
        """fail_task_impl with skip_record=True does not call record_failure."""
        from orchestrator.orchestrator_tools import fail_task_impl

        ws = WorkspaceState(issue_key="QR-42")
        await fail_task_impl(
            issue_key="QR-42",
            error="fatal",
            session=mock_session,
            workspace_state=ws,
            event_bus=event_bus,
            recovery=mock_recovery,
            cleanup_worktrees_callback=cleanup_worktrees,
            skip_record=True,
        )
        mock_recovery.record_failure.assert_not_called()
        mock_session.close.assert_awaited_once()


class TestErrorPaths:
    """Test graceful degradation when tracker calls fail."""

    @pytest.mark.asyncio
    async def test_track_pr_continues_when_transition_fails(
        self, event_bus, mock_tracker, mock_pr_monitor, mock_recovery, mock_session
    ):
        from orchestrator.orchestrator_tools import track_pr_impl

        mock_tracker.transition_to_review.side_effect = requests.ConnectionError("tracker down")
        ws = WorkspaceState(issue_key="QR-42")

        await track_pr_impl(
            issue_key="QR-42",
            pr_url="https://github.com/org/repo/pull/1",
            session=mock_session,
            workspace_state=ws,
            event_bus=event_bus,
            tracker=mock_tracker,
            pr_monitor=mock_pr_monitor,
            recovery=mock_recovery,
        )

        # PR should still be tracked despite transition failure
        mock_pr_monitor.track.assert_called_once()

    @pytest.mark.asyncio
    async def test_monitor_needs_info_defaults_comment_id_on_error(
        self, event_bus, mock_needs_info_monitor, mock_tracker, mock_session, tool_state
    ):
        from orchestrator.orchestrator_tools import monitor_needs_info_impl

        mock_tracker.get_comments.side_effect = requests.ConnectionError("tracker down")
        ws = WorkspaceState(issue_key="QR-42")

        await monitor_needs_info_impl(
            issue_key="QR-42",
            session=mock_session,
            workspace_state=ws,
            tool_state=tool_state,
            event_bus=event_bus,
            needs_info_monitor=mock_needs_info_monitor,
            tracker=mock_tracker,
        )

        # Should still add to monitor with last_seen_comment_id=0
        mock_needs_info_monitor.add.assert_called_once()
        tracked = mock_needs_info_monitor.add.call_args[0][0]
        assert tracked.last_seen_comment_id == 0
