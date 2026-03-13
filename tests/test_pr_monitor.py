"""Tests for PRMonitor — duplicate notification regressions (QR-193)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import requests

from orchestrator.agent_runner import AgentResult, AgentSession
from orchestrator.config import Config, ReposConfig
from orchestrator.constants import EventType, PRState
from orchestrator.github_client import (
    FailedCheck,
    PRFile,
    PRStatus,
    ReviewThread,
    ThreadComment,
)
from orchestrator.pr_monitor import PRMonitor, TrackedPR
from orchestrator.tracker_client import TrackerIssue
from orchestrator.workspace_tools import WorkspaceState


def make_config(**overrides) -> Config:
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(),
        review_check_delay_seconds=0,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_monitor(**overrides) -> tuple[PRMonitor, dict[str, MagicMock]]:
    """Create a PRMonitor with all mocked dependencies."""
    mocks = {
        "tracker": MagicMock(),
        "github": MagicMock(),
        "event_bus": AsyncMock(),
        "proposal_manager": AsyncMock(),
        "cleanup_worktrees": MagicMock(),
    }

    kwargs = dict(
        tracker=mocks["tracker"],
        github=mocks["github"],
        event_bus=mocks["event_bus"],
        proposal_manager=mocks["proposal_manager"],
        config=make_config(),
        semaphore=asyncio.Semaphore(1),
        session_locks={},
        shutdown_event=asyncio.Event(),
        cleanup_worktrees_callback=mocks["cleanup_worktrees"],
        storage=None,
        dispatched_set=None,
    )
    kwargs.update(overrides)

    monitor = PRMonitor(**kwargs)
    return monitor, mocks


def make_tracked_pr(
    issue_key: str = "QR-1",
    pr_url: str = "https://github.com/test/repo/pull/1",
    session: AgentSession | None = None,
) -> TrackedPR:
    """Create a TrackedPR for testing."""
    if session is None:
        session = AsyncMock(spec=AgentSession)
    return TrackedPR(
        issue_key=issue_key,
        pr_url=pr_url,
        owner="test",
        repo="repo",
        pr_number=1,
        session=session,
        workspace_state=WorkspaceState(issue_key=issue_key),
        last_check_at=0.0,
        issue_summary="Test task",
        seen_thread_ids=set(),
        seen_failed_checks=set(),
    )


async def test_failed_checks_not_duplicated_when_send_raises():
    """Test that failed checks are deduplicated even when session.send() raises."""
    monitor, mocks = make_monitor()

    # Create a session mock that raises on send
    session = AsyncMock(spec=AgentSession)
    session.send.side_effect = Exception("session dead")

    pr = make_tracked_pr(issue_key="QR-100", session=session)
    monitor._tracked_prs["QR-100"] = pr

    # Mock GitHub to return two failed checks
    failed_checks = [
        FailedCheck(
            name="lint-and-test",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url="https://github.com/test/repo/runs/1",
            summary="Tests failed",
        ),
        FailedCheck(
            name="frontend",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url="https://github.com/test/repo/runs/2",
            summary="Build failed",
        ),
    ]
    mocks["github"].get_failed_checks = MagicMock(return_value=failed_checks)

    # First call: should catch exception but still update dedup
    await monitor._process_failed_checks("QR-100", pr)

    # Verify PIPELINE_FAILED event was published exactly once
    assert mocks["event_bus"].publish.call_count == 1
    event = mocks["event_bus"].publish.call_args[0][0]
    assert event.type == EventType.PIPELINE_FAILED
    assert event.task_key == "QR-100"
    assert event.data["check_count"] == 2

    # Verify dedup was updated despite send failure
    assert "lint-and-test:FAILURE" in pr.seen_failed_checks
    assert "frontend:FAILURE" in pr.seen_failed_checks

    # Second call with SAME failures: should be deduplicated
    mocks["event_bus"].publish.reset_mock()
    await monitor._process_failed_checks("QR-100", pr)

    # Verify PIPELINE_FAILED event was NOT published a second time
    assert mocks["event_bus"].publish.call_count == 0


async def test_failed_checks_not_duplicated_when_send_returns_success():
    """Test that failed checks are deduplicated when session.send() returns success."""
    monitor, mocks = make_monitor()

    # Create a session mock that returns success
    session = AsyncMock(spec=AgentSession)
    session.send.return_value = AgentResult(success=True, output="Fixed")

    pr = make_tracked_pr(issue_key="QR-101", session=session)
    monitor._tracked_prs["QR-101"] = pr

    # Mock GitHub to return the same failures on every call
    failed_checks = [
        FailedCheck(
            name="lint-and-test",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url="https://github.com/test/repo/runs/1",
            summary="Tests failed",
        ),
    ]
    mocks["github"].get_failed_checks = MagicMock(return_value=failed_checks)

    # First call: should publish event and update dedup
    await monitor._process_failed_checks("QR-101", pr)

    # Verify event was published
    assert mocks["event_bus"].publish.call_count == 1

    # After success, seen_failed_checks is cleared (expected behavior)
    assert len(pr.seen_failed_checks) == 0

    # After success, seen_failed_checks is cleared (expected behavior)
    # So we need to test that the second call with SAME CI results
    # is deduplicated BEFORE success clears the set

    # Reset and test without clearing
    pr.seen_failed_checks.add("lint-and-test:FAILURE")
    mocks["event_bus"].publish.reset_mock()

    # Second call with SAME failures: should be deduplicated
    await monitor._process_failed_checks("QR-101", pr)

    # Verify PIPELINE_FAILED event was NOT published a second time
    assert mocks["event_bus"].publish.call_count == 0


async def test_review_threads_not_duplicated_when_send_raises():
    """Test that review threads are deduplicated even when session.send() raises."""
    monitor, mocks = make_monitor()

    # Create a session mock that raises on send
    session = AsyncMock(spec=AgentSession)
    session.send.side_effect = Exception("session dead")

    pr = make_tracked_pr(issue_key="QR-102", session=session)
    monitor._tracked_prs["QR-102"] = pr

    # Mock GitHub to return unresolved threads
    threads = [
        ReviewThread(
            id="thread-1",
            is_resolved=False,
            path="src/main.py",
            line=42,
            comments=[
                ThreadComment(
                    author="reviewer",
                    body="Fix this",
                    created_at="2024-01-01T00:00:00Z",
                )
            ],
        ),
        ReviewThread(
            id="thread-2",
            is_resolved=False,
            path="src/utils.py",
            line=10,
            comments=[
                ThreadComment(
                    author="reviewer",
                    body="Refactor this",
                    created_at="2024-01-01T00:01:00Z",
                )
            ],
        ),
    ]
    mocks["github"].get_unresolved_threads = MagicMock(return_value=threads)

    # First call: should catch exception but still update dedup
    await monitor._process_review_threads("QR-102", pr)

    # Verify REVIEW_SENT event was published exactly once
    assert mocks["event_bus"].publish.call_count == 1
    event = mocks["event_bus"].publish.call_args[0][0]
    assert event.type == EventType.REVIEW_SENT
    assert event.task_key == "QR-102"
    assert event.data["thread_count"] == 2

    # Verify dedup was updated despite send failure
    assert "thread-1" in pr.seen_thread_ids
    assert "thread-2" in pr.seen_thread_ids

    # Second call with SAME threads: should be deduplicated
    mocks["event_bus"].publish.reset_mock()
    await monitor._process_review_threads("QR-102", pr)

    # Verify REVIEW_SENT event was NOT published a second time
    assert mocks["event_bus"].publish.call_count == 0


async def test_persist_tracking_includes_session_id():
    """_persist_tracking should include session_id from the session."""

    from orchestrator.stats_models import PRTrackingData

    storage = AsyncMock()
    monitor, _mocks = make_monitor(storage=storage)

    # Create a session with a session_id
    session = AsyncMock(spec=AgentSession)
    session.session_id = "ses-persist-test"

    pr = make_tracked_pr(issue_key="QR-SES", session=session)
    monitor._tracked_prs["QR-SES"] = pr

    # Call _persist_tracking and run the scheduled task
    monitor._persist_tracking("QR-SES")

    # Drain background tasks
    await monitor.drain_background_tasks()

    # Verify storage.upsert_pr_tracking was called with session_id
    storage.upsert_pr_tracking.assert_awaited_once()
    data = storage.upsert_pr_tracking.call_args[0][0]
    assert isinstance(data, PRTrackingData)
    assert data.session_id == "ses-persist-test"


async def test_get_persisted_session_id_returns_session_id():
    """get_persisted_session_id should return session_id from loaded data."""
    storage = AsyncMock()
    from orchestrator.stats_models import PRTrackingData

    storage.load_pr_tracking.return_value = [
        PRTrackingData(
            task_key="QR-P1",
            pr_url="https://github.com/org/repo/pull/1",
            issue_summary="Test",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id="ses-loaded",
        )
    ]
    monitor, _ = make_monitor(storage=storage)
    await monitor.load()

    assert monitor.get_persisted_session_id("QR-P1") == "ses-loaded"
    assert monitor.get_persisted_session_id("QR-NONE") is None


async def test_get_persisted_session_id_none_when_no_session():
    """get_persisted_session_id should return None when session_id is not stored."""
    storage = AsyncMock()
    from orchestrator.stats_models import PRTrackingData

    storage.load_pr_tracking.return_value = [
        PRTrackingData(
            task_key="QR-P2",
            pr_url="https://github.com/org/repo/pull/2",
            issue_summary="Test",
            seen_thread_ids=[],
            seen_failed_checks=[],
            session_id=None,
        )
    ]
    monitor, _ = make_monitor(storage=storage)
    await monitor.load()

    assert monitor.get_persisted_session_id("QR-P2") is None


async def test_failed_checks_send_error_logged_as_warning(caplog):
    """Test that session.send() errors are logged as warnings."""
    import logging

    monitor, mocks = make_monitor()

    # Create a session mock that raises on send
    session = AsyncMock(spec=AgentSession)
    session.send.side_effect = Exception("session dead")

    pr = make_tracked_pr(issue_key="QR-103", session=session)
    monitor._tracked_prs["QR-103"] = pr

    # Mock GitHub to return a failed check
    failed_checks = [
        FailedCheck(
            name="test",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url="https://github.com/test/repo/runs/1",
            summary="Failed",
        ),
    ]
    mocks["github"].get_failed_checks = MagicMock(return_value=failed_checks)

    # Call should log warning, not raise
    with caplog.at_level(logging.WARNING):
        await monitor._process_failed_checks("QR-103", pr)

    # Verify warning was logged
    assert any("Failed to send CI failure prompt" in record.message for record in caplog.records)
    assert any("QR-103" in record.message for record in caplog.records)


async def test_pr_merged_closes_orphaned_subtasks():
    """Test that merged PR auto-closes linked subtasks in non-resolved statuses."""
    dispatched_set = {"QR-1", "QR-10", "QR-11"}
    monitor, mocks = make_monitor(dispatched_set=dispatched_set)

    # Setup PR tracking
    pr = make_tracked_pr(issue_key="QR-1", pr_url="https://github.com/test/repo/pull/1")
    monitor._tracked_prs["QR-1"] = pr

    # Mock GitHub to return PR as MERGED
    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    # Mock tracker to return subtask links
    mocks["tracker"].get_links.return_value = [
        {"relationship": "is parent task for", "issue": {"key": "QR-10"}},
        {"relationship": "is parent task for", "issue": {"key": "QR-11"}},
    ]

    # Mock get_issue to return subtasks in active statuses
    def get_issue_side_effect(key):
        if key == "QR-10":
            return TrackerIssue(
                key="QR-10", summary="Subtask 10", description="", components=[], tags=[], status="inProgress"
            )
        if key == "QR-11":
            return TrackerIssue(
                key="QR-11", summary="Subtask 11", description="", components=[], tags=[], status="needInfo"
            )
        return TrackerIssue(key=key, summary="", description="", components=[], tags=[], status="closed")

    mocks["tracker"].get_issue.side_effect = get_issue_side_effect

    # Run the check
    await monitor._check_all()

    # Verify parent task was closed
    assert mocks["tracker"].transition_to_closed.call_count >= 1

    # Verify add_comment was called for both subtasks
    comment_calls = [call[0][1] for call in mocks["tracker"].add_comment.call_args_list]
    assert any("QR-10" in str(call) or "Задача закрыта автоматически" in str(call) for call in comment_calls)
    assert any("QR-11" in str(call) or "Задача закрыта автоматически" in str(call) for call in comment_calls)

    # Verify transition_to_closed was called for both subtasks
    close_calls = [call[0][0] for call in mocks["tracker"].transition_to_closed.call_args_list]
    assert "QR-10" in close_calls
    assert "QR-11" in close_calls

    # Verify subtasks were removed from dispatched_set
    assert "QR-10" not in dispatched_set
    assert "QR-11" not in dispatched_set


async def test_pr_merged_skips_already_resolved_subtasks():
    """Test that already resolved subtasks are not closed again."""
    dispatched_set = {"QR-1", "QR-10", "QR-11"}
    monitor, mocks = make_monitor(dispatched_set=dispatched_set)

    pr = make_tracked_pr(issue_key="QR-1")
    monitor._tracked_prs["QR-1"] = pr

    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    mocks["tracker"].get_links.return_value = [
        {"relationship": "is parent task for", "issue": {"key": "QR-10"}},
        {"relationship": "is parent task for", "issue": {"key": "QR-11"}},
    ]

    # QR-10 is in progress, QR-11 is already closed
    def get_issue_side_effect(key):
        if key == "QR-10":
            return TrackerIssue(
                key="QR-10", summary="Subtask 10", description="", components=[], tags=[], status="inProgress"
            )
        if key == "QR-11":
            return TrackerIssue(
                key="QR-11", summary="Subtask 11", description="", components=[], tags=[], status="closed"
            )
        return TrackerIssue(key=key, summary="", description="", components=[], tags=[], status="closed")

    mocks["tracker"].get_issue.side_effect = get_issue_side_effect

    await monitor._check_all()

    # Verify only QR-10 was closed
    close_calls = [call[0][0] for call in mocks["tracker"].transition_to_closed.call_args_list]
    assert close_calls.count("QR-10") == 1
    assert "QR-11" not in close_calls or close_calls.count("QR-11") == 0


async def test_pr_merged_subtask_closure_failure_does_not_break_flow():
    """Test that failure to close one subtask doesn't prevent closing others."""
    dispatched_set = {"QR-1", "QR-10", "QR-11"}
    monitor, mocks = make_monitor(dispatched_set=dispatched_set)

    pr = make_tracked_pr(issue_key="QR-1")
    monitor._tracked_prs["QR-1"] = pr

    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    mocks["tracker"].get_links.return_value = [
        {"relationship": "is parent task for", "issue": {"key": "QR-10"}},
        {"relationship": "is parent task for", "issue": {"key": "QR-11"}},
    ]

    def get_issue_side_effect(key):
        if key == "QR-10":
            return TrackerIssue(
                key="QR-10", summary="Subtask 10", description="", components=[], tags=[], status="inProgress"
            )
        if key == "QR-11":
            return TrackerIssue(
                key="QR-11", summary="Subtask 11", description="", components=[], tags=[], status="inProgress"
            )
        return TrackerIssue(key=key, summary="", description="", components=[], tags=[], status="closed")

    mocks["tracker"].get_issue.side_effect = get_issue_side_effect

    # Make transition_to_closed fail for QR-10 but succeed for QR-11
    def transition_side_effect(key, **kwargs):
        if key == "QR-10":
            raise requests.ConnectionError("Failed to close QR-10")

    mocks["tracker"].transition_to_closed.side_effect = transition_side_effect

    await monitor._check_all()

    # Verify PR_MERGED event was still published
    published_events = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
    pr_merged_events = [e for e in published_events if e.type == EventType.PR_MERGED]
    assert len(pr_merged_events) == 1

    # Verify session was still closed
    pr.session.close.assert_called_once()


async def test_pr_merged_no_subtask_links():
    """Test that PR merge works normally when there are no subtask links."""
    dispatched_set = {"QR-1"}
    monitor, mocks = make_monitor(dispatched_set=dispatched_set)

    pr = make_tracked_pr(issue_key="QR-1")
    monitor._tracked_prs["QR-1"] = pr

    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    # No subtask links
    mocks["tracker"].get_links.return_value = []

    await monitor._check_all()

    # Verify parent was closed and PR_MERGED event was published
    published_events = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
    pr_merged_events = [e for e in published_events if e.type == EventType.PR_MERGED]
    assert len(pr_merged_events) == 1

    # Verify session was closed
    pr.session.close.assert_called_once()

    # Verify the task itself was removed from dispatched set
    assert "QR-1" not in dispatched_set


async def test_pr_merged_get_links_failure_does_not_break_flow():
    """Test that failure to get links doesn't prevent PR merge flow."""
    dispatched_set = {"QR-1"}
    monitor, mocks = make_monitor(dispatched_set=dispatched_set)

    pr = make_tracked_pr(issue_key="QR-1")
    monitor._tracked_prs["QR-1"] = pr

    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    # get_links raises an exception
    mocks["tracker"].get_links.side_effect = requests.ConnectionError("Tracker API error")

    await monitor._check_all()

    # Verify parent was still closed
    close_calls = [call[0][0] for call in mocks["tracker"].transition_to_closed.call_args_list]
    assert "QR-1" in close_calls

    # Verify PR_MERGED event was still published
    published_events = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
    pr_merged_events = [e for e in published_events if e.type == EventType.PR_MERGED]
    assert len(pr_merged_events) == 1

    # Verify session was still closed
    pr.session.close.assert_called_once()


async def test_pr_monitor_preserves_empty_dispatched_set_reference():
    """Test that PRMonitor preserves reference to empty dispatched_set."""
    # Create an empty set (simulating orchestrator startup before any tasks dispatched)
    orchestrator_dispatched: set[str] = set()

    # Pass the empty set to PRMonitor
    monitor, mocks = make_monitor(dispatched_set=orchestrator_dispatched)

    # Verify that the monitor's internal _dispatched is the SAME object
    # (not a new set created by `or set()`)
    assert monitor._dispatched is orchestrator_dispatched

    # Setup PR tracking
    pr = make_tracked_pr(issue_key="QR-1", pr_url="https://github.com/test/repo/pull/1")
    monitor._tracked_prs["QR-1"] = pr

    # Add a subtask to the orchestrator's dispatched set
    orchestrator_dispatched.add("QR-10")

    # Mock GitHub to return PR as MERGED
    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    # Mock tracker to return subtask link
    mocks["tracker"].get_links.return_value = [
        {"relationship": "is parent task for", "issue": {"key": "QR-10"}},
    ]

    # Mock get_issue to return subtask in active status
    mocks["tracker"].get_issue.return_value = TrackerIssue(
        key="QR-10", summary="Subtask 10", description="", components=[], tags=[], status="inProgress"
    )

    # Run the check
    await monitor._check_all()

    # Verify subtask was removed from the SHARED dispatched set
    # (not just from monitor's internal copy)
    assert "QR-10" not in orchestrator_dispatched


async def test_pr_merged_closes_subtask_with_object_field():
    """Test that subtasks in 'object' field (not 'issue') are also closed."""
    dispatched_set = {"QR-1", "QR-10"}
    monitor, mocks = make_monitor(dispatched_set=dispatched_set)

    pr = make_tracked_pr(issue_key="QR-1")
    monitor._tracked_prs["QR-1"] = pr

    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    # Mock tracker to return link with 'object' field instead of 'issue'
    mocks["tracker"].get_links.return_value = [
        {"relationship": "is parent task for", "object": {"key": "QR-10"}},
    ]

    mocks["tracker"].get_issue.return_value = TrackerIssue(
        key="QR-10", summary="Subtask 10", description="", components=[], tags=[], status="inProgress"
    )

    await monitor._check_all()

    # Verify subtask was closed (using object field)
    close_calls = [call[0][0] for call in mocks["tracker"].transition_to_closed.call_args_list]
    assert "QR-10" in close_calls

    # Verify subtask was removed from dispatched_set
    assert "QR-10" not in dispatched_set


async def test_pr_merged_does_not_close_unrelated_tasks():
    """Test that tasks with only 'relates to' links are NOT auto-closed."""
    dispatched_set = {"QR-1", "QR-20", "QR-21"}
    monitor, mocks = make_monitor(dispatched_set=dispatched_set)

    pr = make_tracked_pr(issue_key="QR-1")
    monitor._tracked_prs["QR-1"] = pr

    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    # QR-1 has only "relates to" links (NOT subtask/parent/epic relationships)
    # These should NOT be auto-closed
    mocks["tracker"].get_links.return_value = [
        {"relationship": "relates to", "issue": {"key": "QR-20"}},
        {"relationship": "depends on", "issue": {"key": "QR-21"}},
    ]

    await monitor._check_all()

    # Verify only parent QR-1 was closed, NOT the related tasks
    close_calls = [call[0][0] for call in mocks["tracker"].transition_to_closed.call_args_list]
    assert "QR-1" in close_calls
    assert "QR-20" not in close_calls  # Should NOT close unrelated task
    assert "QR-21" not in close_calls  # Should NOT close unrelated task


async def test_pr_merged_skips_cancelled_subtasks():
    """Test that cancelled subtasks are skipped (not auto-closed with FIXED resolution)."""
    dispatched_set = {"QR-1", "QR-10", "QR-11"}
    monitor, mocks = make_monitor(dispatched_set=dispatched_set)

    pr = make_tracked_pr(issue_key="QR-1")
    monitor._tracked_prs["QR-1"] = pr

    mocks["github"].get_pr_status = MagicMock(return_value=MagicMock(state=PRState.MERGED))

    mocks["tracker"].get_links.return_value = [
        {"relationship": "is parent task for", "issue": {"key": "QR-10"}},
        {"relationship": "is parent task for", "issue": {"key": "QR-11"}},
    ]

    def get_issue_side_effect(key):
        if key == "QR-10":
            return TrackerIssue(
                key="QR-10", summary="Cancelled subtask", description="", components=[], tags=[], status="cancelled"
            )
        if key == "QR-11":
            return TrackerIssue(
                key="QR-11", summary="Active subtask", description="", components=[], tags=[], status="inProgress"
            )
        return TrackerIssue(key=key, summary="", description="", components=[], tags=[], status="open")

    mocks["tracker"].get_issue.side_effect = get_issue_side_effect

    await monitor._check_all()

    # Verify cancelled subtask QR-10 was NOT closed
    close_calls = [call[0][0] for call in mocks["tracker"].transition_to_closed.call_args_list]
    assert "QR-10" not in close_calls  # Cancelled — should be skipped

    # Verify active subtask QR-11 WAS closed
    assert "QR-11" in close_calls

    # Verify QR-10 was NOT removed from dispatched_set (still cancelled, not auto-fixed)
    assert "QR-10" in dispatched_set

    # Verify QR-11 was removed from dispatched_set
    assert "QR-11" not in dispatched_set


class TestPRMonitorMailboxLifecycle:
    """Test that PRMonitor unregisters agents from mailbox on cleanup."""

    async def test_cleanup_unregisters_agent_from_mailbox(self):
        """When PR is cleaned up, agent should be unregistered from mailbox."""
        from orchestrator.agent_mailbox import AgentMailbox

        mailbox = MagicMock(spec=AgentMailbox)
        mailbox.unregister_agent = AsyncMock()
        monitor, _mocks = make_monitor(mailbox=mailbox)

        pr = make_tracked_pr(issue_key="QR-1")
        monitor._tracked_prs["QR-1"] = pr

        await monitor.cleanup("QR-1")

        mailbox.unregister_agent.assert_called_once_with("QR-1")

    async def test_close_all_unregisters_all_agents_from_mailbox(self):
        """close_all should unregister all tracked agents from mailbox."""
        from orchestrator.agent_mailbox import AgentMailbox

        mailbox = MagicMock(spec=AgentMailbox)
        mailbox.unregister_agent = AsyncMock()
        monitor, _mocks = make_monitor(mailbox=mailbox)

        monitor._tracked_prs["QR-1"] = make_tracked_pr(issue_key="QR-1")
        monitor._tracked_prs["QR-2"] = make_tracked_pr(
            issue_key="QR-2",
            pr_url="https://github.com/test/repo/pull/2",
        )

        await monitor.close_all()

        assert mailbox.unregister_agent.call_count == 2
        unregistered_keys = {call[0][0] for call in mailbox.unregister_agent.call_args_list}
        assert unregistered_keys == {"QR-1", "QR-2"}

    async def test_no_mailbox_cleanup_does_not_crash(self):
        """cleanup should work without mailbox (backwards compatibility)."""
        monitor, _mocks = make_monitor()  # no mailbox

        pr = make_tracked_pr(issue_key="QR-1")
        monitor._tracked_prs["QR-1"] = pr

        # Should not raise
        await monitor.cleanup("QR-1")


class TestMergeConflictDetection:
    """Tests for merge conflict detection in PRMonitor."""

    async def test_merge_conflict_sends_prompt_to_agent(self) -> None:
        """When PR has merge conflict, agent should receive merge conflict prompt."""
        monitor, mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed conflicts")

        pr = make_tracked_pr(issue_key="QR-200", session=session)
        monitor._tracked_prs["QR-200"] = pr

        # Mock GitHub: PR is OPEN with CONFLICTING mergeable
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(state="OPEN", review_decision="", mergeable="CONFLICTING")
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()

        # Verify merge conflict prompt was sent
        session.send.assert_called_once()
        sent_prompt = session.send.call_args[0][0]
        assert "merge conflicts" in sent_prompt.lower()
        assert pr.seen_merge_conflict is True

    async def test_merge_conflict_deduplication(self) -> None:
        """Merge conflict prompt should only be sent once (dedup via seen_merge_conflict)."""
        monitor, mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-201", session=session)
        monitor._tracked_prs["QR-201"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(state="OPEN", review_decision="", mergeable="CONFLICTING")
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # First check
        await monitor._check_all()
        assert session.send.call_count == 1

        # Reset check time so second pass runs
        pr.last_check_at = 0.0

        # Second check — should NOT send again
        await monitor._check_all()
        assert session.send.call_count == 1

    async def test_merge_conflict_reset_on_resolution(self) -> None:
        """When PR becomes MERGEABLE after conflict, seen_merge_conflict should reset."""
        monitor, mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-202", session=session)
        pr.seen_merge_conflict = True  # Was in conflict before
        monitor._tracked_prs["QR-202"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(state="OPEN", review_decision="", mergeable="MERGEABLE")
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()

        assert pr.seen_merge_conflict is False
        # No prompt should be sent for MERGEABLE state
        session.send.assert_not_called()

    async def test_merge_conflict_unknown_ignored(self) -> None:
        """UNKNOWN mergeable state should not trigger any action."""
        monitor, mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)

        pr = make_tracked_pr(issue_key="QR-203", session=session)
        monitor._tracked_prs["QR-203"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(state="OPEN", review_decision="", mergeable="UNKNOWN")
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()

        assert pr.seen_merge_conflict is False
        session.send.assert_not_called()

    async def test_merge_conflict_publishes_event(self) -> None:
        """Merge conflict should publish MERGE_CONFLICT event."""
        monitor, mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-204", session=session)
        monitor._tracked_prs["QR-204"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(state="OPEN", review_decision="", mergeable="CONFLICTING")
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()

        # Verify MERGE_CONFLICT event was published
        published_events = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
        merge_events = [e for e in published_events if e.type == EventType.MERGE_CONFLICT]
        assert len(merge_events) == 1
        assert merge_events[0].task_key == "QR-204"

    async def test_check_all_skips_cleaned_up_pr(self) -> None:
        """After cleanup(), _check_all() should not poll or send prompts for that PR."""
        monitor, mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-300", session=session)
        monitor._tracked_prs["QR-300"] = pr

        # Set up GitHub to return CONFLICTING for this PR
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
            )
        )

        # Clean up the PR (simulating TASK_SKIPPED/COMPLETED)
        await monitor.cleanup("QR-300")

        # Verify PR is removed
        assert "QR-300" not in monitor._tracked_prs

        # Run _check_all() — should NOT attempt to check GitHub or send prompt
        await monitor._check_all()
        mocks["github"].get_pr_status.assert_not_called()
        session.send.assert_not_called()


class TestMergeConflictRetry:
    """Tests for merge conflict retry with SHA gating."""

    async def test_conflict_same_sha_no_retry(self) -> None:
        """Conflict + same SHA on next poll -> NO retry."""
        monitor, mocks = make_monitor(config=make_config(merge_conflict_max_retries=2))

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-R1", session=session)
        monitor._tracked_prs["QR-R1"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-aaa",
            )
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # First poll — sends prompt
        await monitor._check_all()
        assert session.send.call_count == 1
        assert pr.merge_conflict_head_sha == "sha-aaa"

        # Second poll — same SHA, no retry
        pr.last_check_at = 0.0
        await monitor._check_all()
        assert session.send.call_count == 1

    async def test_conflict_new_sha_retries(self) -> None:
        """Conflict + new SHA on next poll -> retry."""
        monitor, mocks = make_monitor(config=make_config(merge_conflict_max_retries=2))

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-R2", session=session)
        monitor._tracked_prs["QR-R2"] = pr

        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # First poll — sha-aaa
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-aaa",
            )
        )
        await monitor._check_all()
        assert session.send.call_count == 1
        assert pr.merge_conflict_retries == 0

        # Second poll — new SHA, still conflicting -> retry
        pr.last_check_at = 0.0
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-bbb",
            )
        )
        await monitor._check_all()
        assert session.send.call_count == 2
        assert pr.merge_conflict_retries == 1

    async def test_counter_resets_on_mergeable(self) -> None:
        """Counter resets when PR becomes MERGEABLE."""
        monitor, mocks = make_monitor(config=make_config(merge_conflict_max_retries=2))

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-R3", session=session)
        pr.seen_merge_conflict = True
        pr.merge_conflict_retries = 1
        pr.merge_conflict_head_sha = "sha-old"
        monitor._tracked_prs["QR-R3"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="MERGEABLE",
            )
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()

        assert pr.seen_merge_conflict is False
        assert pr.merge_conflict_retries == 0
        assert pr.merge_conflict_head_sha == ""

    async def test_exhaustion_no_more_prompts(self) -> None:
        """Retries >= max + new SHA -> no more prompts."""
        monitor, mocks = make_monitor(config=make_config(merge_conflict_max_retries=2))

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-R4", session=session)
        pr.seen_merge_conflict = True
        pr.merge_conflict_retries = 2  # exhausted
        pr.merge_conflict_head_sha = "sha-old"
        monitor._tracked_prs["QR-R4"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-new",
            )
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()
        session.send.assert_not_called()

    async def test_persist_load_cycle(self) -> None:
        """Merge conflict retry state persists across save/load."""
        storage = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(merge_conflict_max_retries=2),
            storage=storage,
        )

        session = AsyncMock(spec=AgentSession)
        session.session_id = "sess-123"
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-R5", session=session)
        monitor._tracked_prs["QR-R5"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-aaa",
            )
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()
        # Wait for background persistence tasks
        await asyncio.sleep(0.05)

        # Verify upsert was called with retries data
        upsert_calls = storage.upsert_pr_tracking.call_args_list
        assert len(upsert_calls) >= 1
        data = upsert_calls[-1][0][0]
        assert data.merge_conflict_retries == 0
        assert data.seen_merge_conflict is True
        assert data.merge_conflict_head_sha == "sha-aaa"

    async def test_after_restart_conflict_sends_prompt(
        self,
    ) -> None:
        """After restart (head_sha == '') + conflict -> one prompt."""
        monitor, mocks = make_monitor(config=make_config(merge_conflict_max_retries=2))

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-R6", session=session)
        # Simulate restart: seen_merge_conflict + head_sha
        # restored from DB
        pr.seen_merge_conflict = True
        pr.merge_conflict_retries = 0
        pr.merge_conflict_head_sha = "sha-old"
        monitor._tracked_prs["QR-R6"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-new",
            )
        )
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()

        # After restart with empty head_sha, a new SHA
        # should trigger retry
        assert session.send.call_count == 1
        assert pr.merge_conflict_retries == 1
        assert pr.merge_conflict_head_sha == "sha-new"

    async def test_head_sha_restored_from_persistence(
        self,
    ) -> None:
        """merge_conflict_head_sha restored from DB prevents spurious retry."""
        from orchestrator.stats_models import PRTrackingData

        storage = AsyncMock()
        storage.load_pr_tracking.return_value = [
            PRTrackingData(
                task_key="QR-SHA",
                pr_url="https://github.com/o/r/pull/1",
                issue_summary="test",
                seen_thread_ids=[],
                seen_failed_checks=[],
                session_id="sess-1",
                seen_merge_conflict=True,
                merge_conflict_retries=0,
                merge_conflict_head_sha="sha-persisted",
            ),
        ]

        monitor, mocks = make_monitor(
            config=make_config(merge_conflict_max_retries=2),
            storage=storage,
        )
        await monitor.load()

        session = AsyncMock(spec=AgentSession)
        session.session_id = "sess-1"
        session.send.return_value = AgentResult(
            success=True,
            output="ok",
        )

        monitor.track(
            issue_key="QR-SHA",
            pr_url="https://github.com/o/r/pull/1",
            session=session,
            workspace_state=MagicMock(),
        )

        pr = monitor._tracked_prs["QR-SHA"]
        assert pr.merge_conflict_head_sha == "sha-persisted"

        # Same SHA as persisted — should NOT retry
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-persisted",
            ),
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        await monitor._check_all()

        # Same SHA — no retry prompt sent
        session.send.assert_not_called()
        assert pr.merge_conflict_retries == 0

    async def test_full_lifecycle_retry_then_resolve(
        self,
    ) -> None:
        """Full lifecycle: conflict -> retry -> resolve -> new conflict."""
        monitor, mocks = make_monitor(config=make_config(merge_conflict_max_retries=2))

        session = AsyncMock(spec=AgentSession)
        session.send.return_value = AgentResult(success=True, output="Fixed")

        pr = make_tracked_pr(issue_key="QR-LIFE", session=session)
        monitor._tracked_prs["QR-LIFE"] = pr

        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # 1. Initial conflict
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-1",
            )
        )
        await monitor._check_all()
        assert session.send.call_count == 1
        assert pr.merge_conflict_retries == 0

        # 2. Agent pushes fix but still conflicting -> retry
        pr.last_check_at = 0.0
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-2",
            )
        )
        await monitor._check_all()
        assert session.send.call_count == 2
        assert pr.merge_conflict_retries == 1

        # 3. PR becomes MERGEABLE — reset
        pr.last_check_at = 0.0
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="MERGEABLE",
            )
        )
        await monitor._check_all()
        assert pr.merge_conflict_retries == 0
        assert pr.merge_conflict_head_sha == ""
        assert session.send.call_count == 2

        # 4. New conflict — counter fresh again
        pr.last_check_at = 0.0
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="sha-3",
            )
        )
        await monitor._check_all()
        assert session.send.call_count == 3
        assert pr.merge_conflict_retries == 0


class TestHumanGate:
    """Tests for human gate — blocking auto-merge for large/sensitive PRs."""

    def _make_pr_files(
        self,
        files: list[tuple[str, int, int]],
    ) -> list[PRFile]:
        """Create PRFile list from (filename, additions, deletions)."""
        return [
            PRFile(
                filename=f,
                status="modified",
                additions=a,
                deletions=d,
                patch=None,
            )
            for f, a, d in files
        ]

    async def test_human_gate_blocks_large_diff(self) -> None:
        """PRs exceeding diff line threshold should be blocked."""
        config = make_config(
            auto_merge_enabled=True,
            human_gate_max_diff_lines=100,
        )
        monitor, mocks = make_monitor(config=config)

        pr = make_tracked_pr(issue_key="QR-HG1")
        monitor._tracked_prs["QR-HG1"] = pr

        # 200 additions + 50 deletions = 250 > 100
        files = self._make_pr_files(
            [
                ("src/big_file.py", 200, 50),
            ]
        )
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )

        blocked, reason = await monitor._check_human_gate(pr)

        assert blocked is True
        assert "250" in reason or "diff" in reason.lower()

    async def test_human_gate_blocks_sensitive_path(self) -> None:
        """PRs touching sensitive paths should be blocked."""
        config = make_config(
            auto_merge_enabled=True,
            human_gate_max_diff_lines=1000,
            human_gate_sensitive_paths="**/auth/**,**/*.sql",
        )
        monitor, mocks = make_monitor(config=config)

        pr = make_tracked_pr(issue_key="QR-HG2")
        monitor._tracked_prs["QR-HG2"] = pr

        # Small diff but touches auth path
        files = self._make_pr_files(
            [
                ("src/auth/login.py", 5, 2),
            ]
        )
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )

        blocked, reason = await monitor._check_human_gate(pr)

        assert blocked is True
        assert "auth" in reason.lower() or "sensitive" in reason.lower()

    async def test_human_gate_blocks_sql_files(self) -> None:
        """PRs touching .sql files should be blocked by glob."""
        config = make_config(
            auto_merge_enabled=True,
            human_gate_max_diff_lines=1000,
            human_gate_sensitive_paths="**/*.sql",
        )
        monitor, mocks = make_monitor(config=config)

        pr = make_tracked_pr(issue_key="QR-HG2B")
        monitor._tracked_prs["QR-HG2B"] = pr

        files = self._make_pr_files(
            [
                ("db/migrations/001_create_users.sql", 10, 0),
            ]
        )
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )

        blocked, _reason = await monitor._check_human_gate(pr)

        assert blocked is True

    async def test_human_gate_passes_normal_pr(self) -> None:
        """Normal PRs within threshold and no sensitive paths pass."""
        config = make_config(
            auto_merge_enabled=True,
            human_gate_max_diff_lines=500,
            human_gate_sensitive_paths="**/auth/**",
        )
        monitor, mocks = make_monitor(config=config)

        pr = make_tracked_pr(issue_key="QR-HG3")
        monitor._tracked_prs["QR-HG3"] = pr

        files = self._make_pr_files(
            [
                ("src/utils.py", 10, 5),
                ("tests/test_utils.py", 20, 3),
            ]
        )
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )

        blocked, reason = await monitor._check_human_gate(pr)

        assert blocked is False
        assert reason == ""

    async def test_human_gate_posts_comment(self) -> None:
        """When gate triggers with notify enabled, a PR comment is posted."""
        config = make_config(
            auto_merge_enabled=True,
            human_gate_max_diff_lines=50,
            human_gate_notify_comment=True,
        )
        monitor, mocks = make_monitor(config=config)

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-HG4", session=session)
        monitor._tracked_prs["QR-HG4"] = pr

        files = self._make_pr_files(
            [
                ("src/main.py", 100, 100),
            ]
        )
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )
        mocks["github"].post_review = MagicMock(
            return_value=True,
        )
        # Provide merge readiness so _process_auto_merge
        # proceeds past the readiness check — but gate blocks first.
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="APPROVED",
                mergeable="MERGEABLE",
                head_sha="abc123",
            ),
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        await monitor._check_all()

        # Verify comment was posted on the PR
        mocks["github"].post_review.assert_called_once()
        call_kwargs = mocks["github"].post_review.call_args
        body = call_kwargs[1].get("body") or call_kwargs[0][3]
        assert "human" in body.lower() or "gate" in body.lower()

    async def test_human_gate_publishes_event(self) -> None:
        """When gate triggers, HUMAN_GATE_TRIGGERED event is published."""
        config = make_config(
            auto_merge_enabled=True,
            human_gate_max_diff_lines=50,
        )
        monitor, mocks = make_monitor(config=config)

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-HG5", session=session)
        monitor._tracked_prs["QR-HG5"] = pr

        files = self._make_pr_files(
            [
                ("src/main.py", 100, 0),
            ]
        )
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )
        mocks["github"].post_review = MagicMock(
            return_value=True,
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="APPROVED",
                mergeable="MERGEABLE",
                head_sha="abc123",
            ),
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        await monitor._check_all()

        published = [c[0][0] for c in mocks["event_bus"].publish.call_args_list]
        gate_events = [e for e in published if e.type == EventType.HUMAN_GATE_TRIGGERED]
        assert len(gate_events) == 1
        assert gate_events[0].task_key == "QR-HG5"


class TestPreMergeReviewFailOpen:
    """Bug: timeout/exception handlers hardcode 'approve' ignoring fail_open."""

    async def test_timeout_rejects_when_fail_closed(self) -> None:
        """When fail_open=False, timeout should set verdict to 'reject'."""
        config = make_config(
            auto_merge_enabled=True,
            pre_merge_review_enabled=True,
            pre_merge_review_fail_open=False,
            pre_merge_review_timeout_seconds=0,
        )
        monitor, _mocks = make_monitor(config=config)

        pr = make_tracked_pr(issue_key="QR-FO1")
        monitor._tracked_prs["QR-FO1"] = pr

        # Simulate reviewer that times out
        async def slow_review(*_a, **_kw):
            raise TimeoutError("review timed out")

        reviewer = AsyncMock()
        reviewer.review.side_effect = slow_review
        monitor._reviewer = reviewer

        await monitor._request_pre_merge_review("QR-FO1", pr)

        # Wait for the background task to finish
        task = monitor._review_tasks.get("QR-FO1")
        if task:
            await task

        assert pr.pre_merge_review_verdict == "reject"

    async def test_timeout_approves_when_fail_open(self) -> None:
        """When fail_open=True, timeout should set verdict to 'approve'."""
        config = make_config(
            auto_merge_enabled=True,
            pre_merge_review_enabled=True,
            pre_merge_review_fail_open=True,
            pre_merge_review_timeout_seconds=0,
        )
        monitor, _mocks = make_monitor(config=config)

        pr = make_tracked_pr(issue_key="QR-FO2")
        monitor._tracked_prs["QR-FO2"] = pr

        async def slow_review(*_a, **_kw):
            raise TimeoutError("review timed out")

        reviewer = AsyncMock()
        reviewer.review.side_effect = slow_review
        monitor._reviewer = reviewer

        await monitor._request_pre_merge_review("QR-FO2", pr)

        task = monitor._review_tasks.get("QR-FO2")
        if task:
            await task

        assert pr.pre_merge_review_verdict == "approve"

    async def test_exception_rejects_when_fail_closed(self) -> None:
        """When fail_open=False, exception should set verdict to 'reject'."""
        config = make_config(
            auto_merge_enabled=True,
            pre_merge_review_enabled=True,
            pre_merge_review_fail_open=False,
        )
        monitor, _mocks = make_monitor(config=config)

        pr = make_tracked_pr(issue_key="QR-FO3")
        monitor._tracked_prs["QR-FO3"] = pr

        async def failing_review(*_a, **_kw):
            raise RuntimeError("something broke")

        reviewer = AsyncMock()
        reviewer.review.side_effect = failing_review
        monitor._reviewer = reviewer

        await monitor._request_pre_merge_review("QR-FO3", pr)

        task = monitor._review_tasks.get("QR-FO3")
        if task:
            await task

        assert pr.pre_merge_review_verdict == "reject"


class TestMergeShaPassedToVerification:
    """Bug: post-merge verification receives head SHA instead of merge SHA."""

    async def test_merge_sha_from_github_api(self) -> None:
        """After merge, verification should get merge_commit_sha from GitHub."""
        from orchestrator.post_merge_verifier import PostMergeVerifier

        config = make_config(
            auto_merge_enabled=True,
            post_merge_verification_enabled=True,
        )
        monitor, mocks = make_monitor(config=config)

        verifier = AsyncMock(spec=PostMergeVerifier)
        verifier.verify = AsyncMock()
        monitor._verifier = verifier

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-MS1", session=session)
        pr.last_seen_head_sha = "head-sha-111"
        monitor._tracked_prs["QR-MS1"] = pr

        # GitHub returns merge_commit_sha via REST
        mocks["github"].get_merge_commit_sha = MagicMock(
            return_value="merge-sha-222",
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="MERGED",
                review_decision="APPROVED",
            ),
        )

        # Suppress transition call
        mocks["tracker"].transition_to_closed = MagicMock()
        mocks["tracker"].get_children = MagicMock(return_value=[])

        await monitor._handle_pr_closed_or_merged(
            "QR-MS1",
            pr,
            PRState.MERGED,
        )

        # Wait for scheduled tasks
        tasks = list(monitor._background_tasks)
        for t in tasks:
            try:
                await t
            except Exception:
                pass

        # Verify merge_sha passed is from GitHub, not head SHA
        verifier.verify.assert_called_once()
        call_kwargs = verifier.verify.call_args
        merge_sha_arg = (
            call_kwargs[1].get(
                "merge_sha",
            )
            or call_kwargs[0][4]
        )
        assert merge_sha_arg == "merge-sha-222"


class TestHumanGateExtended:
    """Extended human gate tests (continued from TestHumanGate)."""

    def _make_pr_files(
        self,
        files: list[tuple[str, int, int]],
    ) -> list[PRFile]:
        return [
            PRFile(
                filename=f,
                status="modified",
                additions=a,
                deletions=d,
                patch=None,
            )
            for f, a, d in files
        ]

    async def test_human_gate_disabled_when_threshold_zero(
        self,
    ) -> None:
        """When threshold is 0, human gate is disabled (no blocking)."""
        config = make_config(
            auto_merge_enabled=True,
            human_gate_max_diff_lines=0,
        )
        monitor, mocks = make_monitor(config=config)

        pr = make_tracked_pr(issue_key="QR-HG6")
        monitor._tracked_prs["QR-HG6"] = pr

        # Even a huge diff should pass when threshold is 0
        files = self._make_pr_files(
            [
                ("src/huge.py", 10000, 5000),
            ]
        )
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )

        blocked, _reason = await monitor._check_human_gate(pr)

        assert blocked is False

    async def test_human_gate_no_comment_when_disabled(
        self,
    ) -> None:
        """When notify_comment is False, no comment is posted."""
        config = make_config(
            auto_merge_enabled=True,
            human_gate_max_diff_lines=50,
            human_gate_notify_comment=False,
        )
        monitor, mocks = make_monitor(config=config)

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(
            issue_key="QR-HG7",
            session=session,
        )
        monitor._tracked_prs["QR-HG7"] = pr

        files = self._make_pr_files(
            [
                ("src/main.py", 100, 100),
            ]
        )
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )
        mocks["github"].post_review = MagicMock(
            return_value=True,
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="APPROVED",
                mergeable="MERGEABLE",
                head_sha="abc123",
            ),
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        await monitor._check_all()

        # post_review should NOT be called
        mocks["github"].post_review.assert_not_called()

        # But event should still be published
        published = [c[0][0] for c in mocks["event_bus"].publish.call_args_list]
        gate_events = [e for e in published if e.type == EventType.HUMAN_GATE_TRIGGERED]
        assert len(gate_events) == 1

    async def test_human_gate_skips_when_auto_merge_disabled(
        self,
    ) -> None:
        """Human gate is only relevant when auto_merge is enabled."""
        config = make_config(
            auto_merge_enabled=False,
            human_gate_max_diff_lines=10,
        )
        monitor, mocks = make_monitor(config=config)

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(
            issue_key="QR-HG8",
            session=session,
        )
        monitor._tracked_prs["QR-HG8"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="APPROVED",
                mergeable="MERGEABLE",
                head_sha="abc123",
            ),
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        await monitor._check_all()

        # get_pr_files should NOT be called (gate not checked)
        mocks["github"].get_pr_files.assert_not_called()

        # No HUMAN_GATE_TRIGGERED events
        published = [c[0][0] for c in mocks["event_bus"].publish.call_args_list]
        gate_events = [e for e in published if e.type == EventType.HUMAN_GATE_TRIGGERED]
        assert len(gate_events) == 0


class TestHumanGateRedundantApiCall:
    """Bug: human gate calls get_pr_files every cycle while review pending."""

    async def test_gate_not_rechecked_after_passing(self) -> None:
        """After human gate passes, get_pr_files must not be
        called again on subsequent poll cycles."""
        config = make_config(
            auto_merge_enabled=True,
            pre_merge_review_enabled=True,
            human_gate_max_diff_lines=1000,
        )
        monitor, mocks = make_monitor(config=config)
        monitor._reviewer = AsyncMock()

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(
            issue_key="QR-HG-R",
            session=session,
        )
        monitor._tracked_prs["QR-HG-R"] = pr

        files = [
            PRFile(
                filename="small.py",
                status="modified",
                additions=10,
                deletions=5,
                patch=None,
            )
        ]
        mocks["github"].get_pr_files = MagicMock(
            return_value=files,
        )
        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
            head_sha="sha1",
        )

        # First cycle — gate passes, review requested
        await monitor._process_auto_merge(
            "QR-HG-R",
            pr,
            status,
        )
        assert mocks["github"].get_pr_files.call_count == 1

        # Second cycle — gate should NOT be re-checked
        await monitor._process_auto_merge(
            "QR-HG-R",
            pr,
            status,
        )
        assert mocks["github"].get_pr_files.call_count == 1


class TestHumanGateResetOnNewCommit:
    """Bug claim: human gate permanently blocks after new commits."""

    async def test_gate_resets_on_new_commit(self) -> None:
        """After human gate blocks, a new commit must reset the
        block so the gate re-evaluates."""
        pr = make_tracked_pr(issue_key="QR-HG-RST")
        pr.auto_merge_attempted = True

        # Simulate new commit via reset_review_flags
        pr.reset_review_flags()

        assert pr.auto_merge_attempted is False


class TestHumanGateDefaultDisabled:
    """Bug: human gate active by default with threshold=500."""

    async def test_default_config_disables_gate(self) -> None:
        """Default config should have gate disabled (threshold=0)."""
        from orchestrator.config import Config, ReposConfig

        config = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        assert config.human_gate_max_diff_lines == 0

    def test_load_config_default_gate_disabled(
        self,
        tmp_path,
    ) -> None:
        """load_config() must also default to 0 (disabled)."""
        import os
        from unittest.mock import patch

        from orchestrator.config import load_config

        env = {
            "YANDEX_TRACKER_TOKEN": "t",
            "YANDEX_TRACKER_ORG_ID": "o",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.human_gate_max_diff_lines == 0


# ---- Post-merge verification wiring ----


class TestPostMergeVerification:
    """Tests for post-merge verification wiring in PRMonitor."""

    async def test_merge_triggers_verification_when_enabled(
        self,
    ) -> None:
        """Verify _run_post_merge_verification is called on merge."""
        monitor, _mocks = make_monitor(
            config=make_config(
                post_merge_verification_enabled=True,
            ),
        )
        _mocks["tracker"].get_links.return_value = []
        _mocks["github"].get_merge_commit_sha = MagicMock(
            return_value="merge-sha-xyz",
        )
        verifier = AsyncMock()
        monitor.set_verifier(verifier)

        pr = make_tracked_pr(issue_key="QR-10")
        pr.last_seen_head_sha = "abc123"
        monitor._tracked_prs["QR-10"] = pr

        await monitor._handle_pr_closed_or_merged(
            "QR-10",
            pr,
            PRState.MERGED,
        )

        # Give the fire-and-forget task time to run
        await monitor.drain_background_tasks()

        verifier.verify.assert_awaited_once()
        call_kwargs = verifier.verify.call_args
        assert call_kwargs[1]["issue_key"] == "QR-10"
        # Should use merge_commit_sha from GitHub, not head SHA
        assert call_kwargs[1]["merge_sha"] == "merge-sha-xyz"

    async def test_merge_skips_verification_when_disabled(
        self,
    ) -> None:
        """Verify no call when config disabled."""
        monitor, _mocks = make_monitor(
            config=make_config(
                post_merge_verification_enabled=False,
            ),
        )
        _mocks["tracker"].get_links.return_value = []
        verifier = AsyncMock()
        monitor.set_verifier(verifier)

        pr = make_tracked_pr(issue_key="QR-11")
        pr.last_seen_head_sha = "abc123"
        monitor._tracked_prs["QR-11"] = pr

        await monitor._handle_pr_closed_or_merged(
            "QR-11",
            pr,
            PRState.MERGED,
        )
        await monitor.drain_background_tasks()

        verifier.verify.assert_not_awaited()

    async def test_merge_skips_verification_when_no_verifier(
        self,
    ) -> None:
        """Verify no crash when verifier not set."""
        monitor, _mocks = make_monitor(
            config=make_config(
                post_merge_verification_enabled=True,
            ),
        )
        _mocks["tracker"].get_links.return_value = []
        # No verifier set — should not crash
        pr = make_tracked_pr(issue_key="QR-12")
        pr.last_seen_head_sha = "abc123"
        monitor._tracked_prs["QR-12"] = pr

        await monitor._handle_pr_closed_or_merged(
            "QR-12",
            pr,
            PRState.MERGED,
        )
        await monitor.drain_background_tasks()
        # No exception means success

    async def test_verification_error_doesnt_crash_monitor(
        self,
    ) -> None:
        """Exception in verifier doesn't propagate."""
        monitor, _mocks = make_monitor(
            config=make_config(
                post_merge_verification_enabled=True,
            ),
        )
        _mocks["tracker"].get_links.return_value = []
        verifier = AsyncMock()
        verifier.verify.side_effect = RuntimeError("boom")
        monitor.set_verifier(verifier)

        pr = make_tracked_pr(issue_key="QR-13")
        pr.last_seen_head_sha = "abc123"
        monitor._tracked_prs["QR-13"] = pr

        await monitor._handle_pr_closed_or_merged(
            "QR-13",
            pr,
            PRState.MERGED,
        )
        await monitor.drain_background_tasks()

        # Verifier was called, error was swallowed
        verifier.verify.assert_awaited_once()

    async def test_closed_pr_skips_verification(
        self,
    ) -> None:
        """Verify no verification on closed (not merged) PR."""
        monitor, _mocks = make_monitor(
            config=make_config(
                post_merge_verification_enabled=True,
            ),
        )
        _mocks["tracker"].get_links.return_value = []
        verifier = AsyncMock()
        monitor.set_verifier(verifier)

        pr = make_tracked_pr(issue_key="QR-14")
        pr.last_seen_head_sha = "abc123"
        monitor._tracked_prs["QR-14"] = pr

        await monitor._handle_pr_closed_or_merged(
            "QR-14",
            pr,
            PRState.CLOSED,
        )
        await monitor.drain_background_tasks()

        verifier.verify.assert_not_awaited()


class TestCleanupOnMergeError:
    """Bug: exception in merge branch skips session cleanup."""

    async def test_cleanup_runs_when_merge_sha_fetch_fails(
        self,
    ) -> None:
        """Session close + cleanup must run even if
        get_merge_commit_sha raises an unexpected error."""
        from orchestrator.post_merge_verifier import (
            PostMergeVerifier,
        )

        verifier = AsyncMock(spec=PostMergeVerifier)
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                post_merge_verification_enabled=True,
            ),
            verifier=verifier,
        )

        session = AsyncMock()
        pr = make_tracked_pr("QR-ERR", session=session)

        mocks["tracker"].transition_to_closed = MagicMock()
        mocks["tracker"].get_children = MagicMock(
            return_value=[],
        )
        # get_merge_commit_sha raises unexpected error
        mocks["github"].get_merge_commit_sha = MagicMock(
            side_effect=RuntimeError("unexpected"),
        )

        monitor._tracked_prs["QR-ERR"] = pr
        monitor.cleanup = AsyncMock()

        try:
            await monitor._handle_pr_closed_or_merged(
                "QR-ERR",
                pr,
                PRState.MERGED,
            )
        except RuntimeError:
            pass  # Expected — exception propagates after finally

        # Session close may or may not be called (depends on
        # exception point), but cleanup MUST run via finally
        monitor.cleanup.assert_awaited_once_with("QR-ERR")


class TestRemovedFlag:
    """F3: removed flag prevents stale operations after remove()."""

    async def test_check_all_skips_removed_pr(self) -> None:
        """_check_all should not process a PR with removed=True."""
        monitor, mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-RM1", session=session)
        pr.removed = True
        monitor._tracked_prs["QR-RM1"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
            )
        )

        await monitor._check_all()

        # Removed PRs should NOT trigger GitHub calls
        session.send.assert_not_called()

    async def test_get_tracked_excludes_removed(self) -> None:
        """get_tracked() filters out removed entries."""
        monitor, _mocks = make_monitor()

        pr_active = make_tracked_pr(issue_key="QR-A")
        pr_removed = make_tracked_pr(issue_key="QR-R")
        pr_removed.removed = True

        monitor._tracked_prs["QR-A"] = pr_active
        monitor._tracked_prs["QR-R"] = pr_removed

        tracked = monitor.get_tracked()
        assert "QR-A" in tracked
        assert "QR-R" not in tracked

    async def test_handle_pr_closed_sets_removed(self) -> None:
        """_handle_pr_closed_or_merged sets removed=True."""
        monitor, mocks = make_monitor()
        mocks["tracker"].get_links = MagicMock(return_value=[])

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-RM2", session=session)
        monitor._tracked_prs["QR-RM2"] = pr

        await monitor._handle_pr_closed_or_merged(
            "QR-RM2",
            pr,
            PRState.CLOSED,
        )

        assert pr.removed is True

    async def test_cleanup_sets_removed(self) -> None:
        """cleanup() sets removed=True."""
        monitor, _mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-RM3", session=session)
        monitor._tracked_prs["QR-RM3"] = pr

        await monitor.cleanup("QR-RM3")

        assert pr.removed is True

    async def test_removed_pr_not_auto_merged(self) -> None:
        """Auto-merge must not proceed on a removed PR."""
        config = make_config(auto_merge_enabled=True)
        monitor, mocks = make_monitor(config=config)

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-RM4", session=session)
        pr.removed = True
        monitor._tracked_prs["QR-RM4"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="APPROVED",
                mergeable="MERGEABLE",
                head_sha="abc",
            ),
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=True,
        )

        await monitor._check_all()

        mocks["github"].enable_auto_merge.assert_not_called()

    async def test_removed_pr_not_sent_review_prompt(self) -> None:
        """Review prompts must not be sent to a removed PR."""
        monitor, mocks = make_monitor()

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-RM5", session=session)
        pr.removed = True
        monitor._tracked_prs["QR-RM5"] = pr

        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="CHANGES_REQUESTED",
                mergeable="MERGEABLE",
            ),
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[
                ReviewThread(
                    id="t1",
                    is_resolved=False,
                    path="src/main.py",
                    line=10,
                    comments=[
                        ThreadComment(
                            body="Fix this",
                            author="reviewer",
                            created_at="2025-01-01T00:00:00Z",
                        )
                    ],
                )
            ],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        await monitor._check_all()

        session.send.assert_not_called()


class TestRemove:
    """F3: PR monitor remove() method."""

    async def test_remove_closes_session_and_cleans_up(self) -> None:
        """remove() closes session, cleans up worktrees, deletes tracking."""
        storage = AsyncMock()
        monitor, _mocks = make_monitor(storage=storage)

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(
            issue_key="QR-DEL",
            session=session,
        )
        monitor._tracked_prs["QR-DEL"] = pr

        await monitor.remove("QR-DEL")

        assert pr.removed is True
        session.close.assert_awaited_once()
        assert "QR-DEL" not in monitor._tracked_prs
        _mocks["cleanup_worktrees"].assert_called_once()

    async def test_remove_nonexistent_key_is_noop(self) -> None:
        """remove() on unknown key should not raise."""
        monitor, _mocks = make_monitor()

        # Should not raise
        await monitor.remove("QR-MISSING")
