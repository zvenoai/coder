"""Tests for session lock cleanup to prevent memory leaks."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.config import Config, ReposConfig
from orchestrator.event_bus import EventBus
from orchestrator.needs_info_monitor import NeedsInfoMonitor, TrackedNeedsInfo
from orchestrator.orchestrator_agent import OrchestratorAgent
from orchestrator.pr_monitor import PRMonitor, TrackedPR
from orchestrator.tracker_tools import ToolState
from orchestrator.workspace_tools import WorkspaceState


@pytest.fixture
def session_locks():
    """Shared session locks dict."""
    return {"QR-100": asyncio.Lock(), "QR-200": asyncio.Lock()}


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def mock_tracker():
    tracker = MagicMock()
    tracker.add_comment = MagicMock()
    tracker.transition_to_review = MagicMock()
    tracker.get_comments = MagicMock(return_value=[])
    tracker.get_links = MagicMock(return_value=[])
    tracker.create_issue = MagicMock(return_value={"key": "QR-99"})
    return tracker


@pytest.fixture
def mock_config():
    return Config(
        tracker_token="token",
        tracker_org_id="org",
        tracker_queue="TEST",
        tracker_tag="ai-task",
        tracker_project_id=13,
        tracker_boards=[14],
        workspace_dir="/workspace",
        worktree_base_dir="/workspace/worktrees",
        repos_config=ReposConfig(),
        github_token="gh_token",
        anthropic_api_key="api_key",
        supervisor_enabled=False,
    )


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def mock_workspace_state():
    return WorkspaceState(issue_key="QR-100")


@pytest.fixture
def mock_tool_state():
    return ToolState()


# ================ PRMonitor Tests ================


@pytest.mark.asyncio
async def test_pr_monitor_cleanup_removes_session_lock(session_locks, event_bus, mock_tracker, mock_config):
    """Test that PRMonitor.cleanup() removes the session lock."""
    monitor = PRMonitor(
        tracker=mock_tracker,
        github=MagicMock(),
        event_bus=event_bus,
        proposal_manager=MagicMock(),
        config=mock_config,
        semaphore=asyncio.Semaphore(1),
        session_locks=session_locks,
        shutdown_event=asyncio.Event(),
        cleanup_worktrees_callback=lambda key, repos: None,
        dispatched_set=set(),
        storage=None,
        mailbox=None,
        reviewer=None,
    )

    # Add a tracked PR
    issue_key = "QR-100"
    session = AsyncMock()
    tracked_pr = TrackedPR(
        issue_key=issue_key,
        pr_number=123,
        pr_url="https://github.com/test/api/pull/123",
        owner="test",
        repo="api",
        session=session,
        workspace_state=WorkspaceState(issue_key=issue_key),
        last_check_at=0.0,
    )
    monitor._tracked_prs[issue_key] = tracked_pr

    # Verify lock exists
    assert issue_key in session_locks

    # Call cleanup
    await monitor.cleanup(issue_key)

    # Verify lock is removed
    assert issue_key not in session_locks


@pytest.mark.asyncio
async def test_pr_monitor_close_all_removes_session_locks(session_locks, event_bus, mock_tracker, mock_config):
    """Test that PRMonitor.close_all() removes all tracked session locks."""
    monitor = PRMonitor(
        tracker=mock_tracker,
        github=MagicMock(),
        event_bus=event_bus,
        proposal_manager=MagicMock(),
        config=mock_config,
        semaphore=asyncio.Semaphore(1),
        session_locks=session_locks,
        shutdown_event=asyncio.Event(),
        cleanup_worktrees_callback=lambda key, repos: None,
        dispatched_set=set(),
        storage=None,
        mailbox=None,
        reviewer=None,
    )

    # Add two tracked PRs
    for issue_key in ["QR-100", "QR-200"]:
        tracked_pr = TrackedPR(
            issue_key=issue_key,
            pr_number=123,
            pr_url=f"https://github.com/test/api/pull/{issue_key}",
            owner="test",
            repo="api",
            session=AsyncMock(),
            workspace_state=WorkspaceState(issue_key=issue_key),
            last_check_at=0.0,
        )
        monitor._tracked_prs[issue_key] = tracked_pr

    # Verify locks exist
    assert "QR-100" in session_locks
    assert "QR-200" in session_locks

    # Call close_all
    await monitor.close_all()

    # Verify locks are removed
    assert "QR-100" not in session_locks
    assert "QR-200" not in session_locks


# ================ NeedsInfoMonitor Tests ================


@pytest.mark.asyncio
async def test_needs_info_handle_success_removes_session_lock(
    session_locks, event_bus, mock_tracker, mock_config, mock_session, mock_workspace_state
):
    """Test that NeedsInfoMonitor._handle_success() removes the session lock."""
    monitor = NeedsInfoMonitor(
        tracker=mock_tracker,
        event_bus=event_bus,
        proposal_manager=MagicMock(),
        config=mock_config,
        semaphore=asyncio.Semaphore(1),
        session_locks=session_locks,
        shutdown_event=asyncio.Event(),
        bot_login="bot",
        track_pr_callback=lambda *args: None,
        cleanup_worktrees_callback=lambda key, repos: None,
        get_latest_comment_id_callback=lambda key: 0,
        dispatched_set=set(),
        record_failure_callback=None,
        clear_recovery_callback=None,
        storage=None,
        mailbox=None,
    )

    issue_key = "QR-100"
    tracked = TrackedNeedsInfo(
        issue_key=issue_key,
        session=mock_session,
        workspace_state=mock_workspace_state,
        tool_state=ToolState(),
        last_check_at=0.0,
        last_seen_comment_id=0,
    )
    monitor._tracked[issue_key] = tracked

    # Verify lock exists
    assert issue_key in session_locks

    # Call _handle_success
    await monitor._handle_success(issue_key, tracked, cost=1.0, duration=10.0)

    # Verify lock is removed
    assert issue_key not in session_locks


@pytest.mark.asyncio
async def test_needs_info_handle_failure_removes_session_lock(
    session_locks, event_bus, mock_tracker, mock_config, mock_session, mock_workspace_state
):
    """Test that NeedsInfoMonitor._handle_failure() removes the session lock."""
    monitor = NeedsInfoMonitor(
        tracker=mock_tracker,
        event_bus=event_bus,
        proposal_manager=MagicMock(),
        config=mock_config,
        semaphore=asyncio.Semaphore(1),
        session_locks=session_locks,
        shutdown_event=asyncio.Event(),
        bot_login="bot",
        track_pr_callback=lambda *args: None,
        cleanup_worktrees_callback=lambda key, repos: None,
        get_latest_comment_id_callback=lambda key: 0,
        dispatched_set=set(),
        record_failure_callback=None,
        clear_recovery_callback=None,
        storage=None,
        mailbox=None,
    )

    issue_key = "QR-100"
    tracked = TrackedNeedsInfo(
        issue_key=issue_key,
        session=mock_session,
        workspace_state=mock_workspace_state,
        tool_state=ToolState(),
        last_check_at=0.0,
        last_seen_comment_id=0,
    )
    monitor._tracked[issue_key] = tracked

    # Verify lock exists
    assert issue_key in session_locks

    # Call _handle_failure
    await monitor._handle_failure(issue_key, tracked, error="Test error", cost=1.0, duration=10.0)

    # Verify lock is removed
    assert issue_key not in session_locks


@pytest.mark.asyncio
async def test_needs_info_handle_pr_created_preserves_lock(
    session_locks, event_bus, mock_tracker, mock_config, mock_session, mock_workspace_state
):
    """Test that NeedsInfoMonitor._handle_pr_created() does NOT remove the lock (session transfers to PRMonitor)."""
    monitor = NeedsInfoMonitor(
        tracker=mock_tracker,
        event_bus=event_bus,
        proposal_manager=MagicMock(),
        config=mock_config,
        semaphore=asyncio.Semaphore(1),
        session_locks=session_locks,
        shutdown_event=asyncio.Event(),
        bot_login="bot",
        track_pr_callback=lambda *args: None,
        cleanup_worktrees_callback=lambda key, repos: None,
        get_latest_comment_id_callback=lambda key: 0,
        dispatched_set=set(),
        record_failure_callback=None,
        clear_recovery_callback=None,
        storage=None,
        mailbox=None,
    )

    issue_key = "QR-100"
    tracked = TrackedNeedsInfo(
        issue_key=issue_key,
        session=mock_session,
        workspace_state=mock_workspace_state,
        tool_state=ToolState(),
        last_check_at=0.0,
        last_seen_comment_id=0,
    )
    monitor._tracked[issue_key] = tracked

    # Verify lock exists
    assert issue_key in session_locks

    # Call _handle_pr_created
    await monitor._handle_pr_created(
        issue_key, tracked, pr_url="https://github.com/test/api/pull/123", cost=1.0, duration=10.0
    )

    # Verify lock is NOT removed (session transfers to PR monitor)
    assert issue_key in session_locks


@pytest.mark.asyncio
async def test_needs_info_close_all_removes_session_locks(
    session_locks, event_bus, mock_tracker, mock_config, mock_session, mock_workspace_state
):
    """Test that NeedsInfoMonitor.close_all() removes all tracked session locks."""
    monitor = NeedsInfoMonitor(
        tracker=mock_tracker,
        event_bus=event_bus,
        proposal_manager=MagicMock(),
        config=mock_config,
        semaphore=asyncio.Semaphore(1),
        session_locks=session_locks,
        shutdown_event=asyncio.Event(),
        bot_login="bot",
        track_pr_callback=lambda *args: None,
        cleanup_worktrees_callback=lambda key, repos: None,
        get_latest_comment_id_callback=lambda key: 0,
        dispatched_set=set(),
        record_failure_callback=None,
        clear_recovery_callback=None,
        storage=None,
        mailbox=None,
    )

    # Add two tracked needs-info sessions
    for issue_key in ["QR-100", "QR-200"]:
        tracked = TrackedNeedsInfo(
            issue_key=issue_key,
            session=AsyncMock(),
            workspace_state=mock_workspace_state,
            tool_state=ToolState(),
            last_check_at=0.0,
            last_seen_comment_id=0,
        )
        monitor._tracked[issue_key] = tracked

    # Verify locks exist
    assert "QR-100" in session_locks
    assert "QR-200" in session_locks

    # Call close_all
    await monitor.close_all()

    # Verify locks are removed
    assert "QR-100" not in session_locks
    assert "QR-200" not in session_locks


# ================ OrchestratorAgent Tests ================


@pytest.mark.asyncio
async def test_orchestrator_agent_complete_removes_lock(mock_tracker, event_bus, mock_config, mock_workspace_state):
    """Test that OrchestratorAgent does NOT call cleanup callback on first successful completion without PR."""
    cleanup_called_with = []

    def cleanup_callback(key: str):
        cleanup_called_with.append(key)

    mock_recovery = MagicMock()
    mock_recovery.get_state.return_value = MagicMock(attempt_count=0, should_retry=False)
    mock_recovery.record_success = MagicMock()
    mock_recovery.clear = MagicMock()
    # Mock record_no_pr to return state for first no-PR completion
    mock_state = MagicMock()
    mock_state.no_pr_count = 1
    mock_state.should_retry_no_pr = False
    mock_state.last_output = "Done"
    mock_state.no_pr_cost = 1.0
    mock_recovery.record_no_pr.return_value = mock_state

    mock_pr_monitor = MagicMock()
    mock_needs_info_monitor = MagicMock()

    mock_issue = MagicMock()
    mock_issue.key = "QR-100"
    mock_issue.summary = "Test task"
    mock_issue.description = "Test description"
    mock_issue.priority = "P1"

    mock_mailbox = MagicMock()
    mock_mailbox.unregister_agent = AsyncMock()

    agent = OrchestratorAgent(
        event_bus=event_bus,
        tracker=mock_tracker,
        pr_monitor=mock_pr_monitor,
        needs_info_monitor=mock_needs_info_monitor,
        recovery=mock_recovery,
        dispatched_set=set(),
        cleanup_worktrees_callback=lambda key, repos: None,
        cleanup_session_lock=cleanup_callback,
        mailbox=mock_mailbox,
    )

    mock_session = AsyncMock()
    mock_session.session_id = "test-session"
    mock_result = MagicMock()
    mock_result.output = "Done"
    mock_result.cost_usd = 1.0
    mock_result.duration_seconds = 10.0

    # Mock the complete_task_impl to avoid actual implementation
    with patch("orchestrator.orchestrator_agent.complete_task_impl", new_callable=AsyncMock):
        await agent._handle_success_no_pr(
            issue=mock_issue, result=mock_result, session=mock_session, workspace_state=mock_workspace_state
        )

    # Verify cleanup callback WAS called for first no-PR completion to prevent memory leak
    assert "QR-100" in cleanup_called_with
    # Verify mailbox was cleaned up
    mock_mailbox.unregister_agent.assert_called_once_with("QR-100")


@pytest.mark.asyncio
async def test_orchestrator_agent_failure_removes_lock(mock_tracker, event_bus, mock_config, mock_workspace_state):
    """Test that OrchestratorAgent calls cleanup callback on failure."""
    cleanup_called_with = []

    def cleanup_callback(key: str):
        cleanup_called_with.append(key)

    mock_recovery = MagicMock()
    mock_recovery.get_state.return_value = MagicMock(attempt_count=0, should_retry=False)
    mock_recovery.record_failure = MagicMock(return_value=MagicMock(attempt_count=1, should_retry=False))
    mock_recovery.clear = MagicMock()

    mock_pr_monitor = MagicMock()
    mock_needs_info_monitor = MagicMock()

    mock_issue = MagicMock()
    mock_issue.key = "QR-100"
    mock_issue.summary = "Test task"
    mock_issue.description = "Test description"
    mock_issue.priority = "P1"

    mock_mailbox = MagicMock()
    mock_mailbox.unregister_agent = AsyncMock()

    agent = OrchestratorAgent(
        event_bus=event_bus,
        tracker=mock_tracker,
        pr_monitor=mock_pr_monitor,
        needs_info_monitor=mock_needs_info_monitor,
        recovery=mock_recovery,
        dispatched_set=set(),
        cleanup_worktrees_callback=lambda key, repos: None,
        cleanup_session_lock=cleanup_callback,
        mailbox=mock_mailbox,
    )

    mock_session = AsyncMock()
    mock_session.session_id = "test-session"
    mock_result = MagicMock()
    mock_result.output = "Test error"
    mock_result.cost_usd = 1.0
    mock_result.duration_seconds = 10.0

    # Call _handle_failure
    with patch("orchestrator.orchestrator_agent.fail_task_impl", new_callable=AsyncMock):
        await agent._handle_failure(
            issue=mock_issue, result=mock_result, session=mock_session, workspace_state=mock_workspace_state
        )

    # Verify cleanup callback was called
    assert "QR-100" in cleanup_called_with


@pytest.mark.asyncio
async def test_orchestrator_agent_retryable_failure_removes_lock(
    mock_tracker, event_bus, mock_config, mock_workspace_state
):
    """Test that OrchestratorAgent calls cleanup callback even on retryable failure."""
    cleanup_called_with = []

    def cleanup_callback(key: str):
        cleanup_called_with.append(key)

    mock_recovery = MagicMock()
    mock_recovery.get_state.return_value = MagicMock(attempt_count=1, should_retry=True)
    mock_recovery.record_failure = MagicMock(return_value=MagicMock(attempt_count=1, should_retry=True))
    mock_recovery.clear = MagicMock()

    mock_pr_monitor = MagicMock()
    mock_needs_info_monitor = MagicMock()

    mock_issue = MagicMock()
    mock_issue.key = "QR-100"
    mock_issue.summary = "Test task"
    mock_issue.description = "Test description"
    mock_issue.priority = "P1"

    mock_mailbox = MagicMock()
    mock_mailbox.unregister_agent = AsyncMock()

    agent = OrchestratorAgent(
        event_bus=event_bus,
        tracker=mock_tracker,
        pr_monitor=mock_pr_monitor,
        needs_info_monitor=mock_needs_info_monitor,
        recovery=mock_recovery,
        dispatched_set=set(),
        cleanup_worktrees_callback=lambda key, repos: None,
        cleanup_session_lock=cleanup_callback,
        mailbox=mock_mailbox,
    )

    mock_session = AsyncMock()
    mock_session.session_id = "test-session"
    mock_result = MagicMock()
    mock_result.output = "Retryable error"
    mock_result.cost_usd = 1.0
    mock_result.duration_seconds = 10.0

    # Call _handle_failure with retryable error
    await agent._handle_failure(
        issue=mock_issue, result=mock_result, session=mock_session, workspace_state=mock_workspace_state
    )

    # Verify cleanup callback was called
    assert "QR-100" in cleanup_called_with
