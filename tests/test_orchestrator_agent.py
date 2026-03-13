"""Tests for OrchestratorAgent — Opus agent that replaces hardcoded decision tree."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.agent_runner import AgentResult
from orchestrator.config import Config, ReposConfig
from orchestrator.event_bus import EventBus
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
    tracker.get_comments = MagicMock(return_value=[])
    tracker.get_links = MagicMock(return_value=[])
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
        attempt_count=0,
        attempts=[],
        should_retry=True,
        backoff_seconds=0.0,
    )
    recovery.record_failure = MagicMock()
    recovery.record_no_pr = MagicMock()
    recovery.clear = MagicMock()
    return recovery


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.close = AsyncMock()
    return session


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
        supervisor_enabled=False,
    )


@pytest.fixture
def dispatched():
    return {"QR-42"}


@pytest.fixture
def cleanup_worktrees():
    return MagicMock()


@pytest.fixture
def mock_issue():
    issue = MagicMock()
    issue.key = "QR-42"
    issue.summary = "Fix the login bug"
    issue.description = "Login page throws error"
    issue.status = "In Progress"
    issue.components = ["Бекенд"]
    issue.tags = ["ai-task"]
    return issue


@pytest.fixture
def workspace_state():
    return WorkspaceState(issue_key="QR-42")


@pytest.fixture
def tool_state():
    return ToolState()


@pytest.fixture
def mock_mailbox():
    from orchestrator.agent_mailbox import AgentMailbox

    mb = MagicMock(spec=AgentMailbox)
    mb.unregister_agent = AsyncMock()
    return mb


@pytest.fixture
def orchestrator_agent(
    event_bus,
    mock_tracker,
    mock_pr_monitor,
    mock_needs_info_monitor,
    mock_recovery,
    dispatched,
    cleanup_worktrees,
    mock_mailbox,
):
    from orchestrator.orchestrator_agent import OrchestratorAgent

    return OrchestratorAgent(
        event_bus=event_bus,
        tracker=mock_tracker,
        pr_monitor=mock_pr_monitor,
        needs_info_monitor=mock_needs_info_monitor,
        recovery=mock_recovery,
        dispatched_set=dispatched,
        cleanup_worktrees_callback=cleanup_worktrees,
        mailbox=mock_mailbox,
    )


class TestHandleResult:
    """Test handle_result: Opus agent decides what to do with worker results."""

    @pytest.mark.asyncio
    async def test_success_with_pr_calls_track_pr(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        mock_pr_monitor,
        mock_recovery,
        event_bus,
    ):
        """When agent succeeds with PR → track_pr is called."""
        result = AgentResult(
            success=True,
            output="Created PR",
            pr_url="https://github.com/org/repo/pull/123",
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_pr_monitor.track.assert_called_once()
        mock_recovery.clear.assert_called_once_with("QR-42")

    @pytest.mark.asyncio
    async def test_needs_info_calls_monitor(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        mock_needs_info_monitor,
        event_bus,
    ):
        """When agent requests info → needs_info_monitor is engaged."""
        tool_state.needs_info_requested = True
        tool_state.needs_info_text = "What is the login endpoint?"
        result = AgentResult(
            success=True,
            output="Need more info",
            needs_info=True,
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_needs_info_monitor.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_success_no_pr_calls_record_no_pr(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        dispatched,
        event_bus,
        mock_recovery,
        cleanup_worktrees,
    ):
        """When agent succeeds without PR → record_no_pr is called."""
        from orchestrator.recovery import RecoveryState

        state = RecoveryState(issue_key="QR-42")
        state.no_pr_count = 1
        state.last_output = "Made changes but no PR"
        state.no_pr_cost = 0.50
        mock_recovery.record_no_pr.return_value = state

        result = AgentResult(
            success=True,
            output="Made changes but no PR",
            cost_usd=0.50,
            duration_seconds=60.0,
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Should call record_no_pr with output and cost
        mock_recovery.record_no_pr.assert_called_once_with("QR-42", "Made changes but no PR", 0.50)

        # First no-PR should complete successfully (not retry)
        from orchestrator.constants import EventType

        events = event_bus.get_task_history("QR-42")
        completed_events = [e for e in events if e.type == EventType.TASK_COMPLETED]
        assert len(completed_events) == 1

    @pytest.mark.asyncio
    async def test_success_no_pr_retries_when_under_limit(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        dispatched,
        event_bus,
        mock_recovery,
        cleanup_worktrees,
    ):
        """When no-PR retry is allowed (2nd attempt) → task is unblocked for re-dispatch."""
        from orchestrator.recovery import RecoveryState

        # Simulate 2nd no-PR attempt
        state = RecoveryState(issue_key="QR-42")
        state.no_pr_count = 2  # Second attempt
        state.last_output = "Made changes but no PR again"
        state.no_pr_cost = 1.00
        mock_recovery.record_no_pr.return_value = state

        result = AgentResult(
            success=True,
            output="Made changes but no PR again",
            cost_usd=0.50,
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Should unblock from dispatched for retry
        assert "QR-42" not in dispatched
        mock_session.close.assert_awaited_once()
        cleanup_worktrees.assert_called_once()

        # TASK_FAILED event with retryable=True
        from orchestrator.constants import EventType

        events = event_bus.get_task_history("QR-42")
        failed_events = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed_events) == 1
        assert failed_events[0].data["retryable"] is True

    @pytest.mark.asyncio
    async def test_success_no_pr_retry_includes_session_id(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        event_bus,
        mock_recovery,
    ):
        """Retryable no-PR TASK_FAILED event should include session_id for analytics."""
        from orchestrator.recovery import RecoveryState

        # Simulate 2nd no-PR attempt (retryable)
        state = RecoveryState(issue_key="QR-42")
        state.no_pr_count = 2
        state.last_output = "Made changes but no PR again"
        state.no_pr_cost = 1.00
        mock_recovery.record_no_pr.return_value = state

        # Mock session with a specific session_id
        mock_session.session_id = "test-session-123"

        result = AgentResult(
            success=True,
            output="Made changes but no PR again",
            cost_usd=0.50,
            duration_seconds=120.0,
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Verify TASK_FAILED event includes session_id
        from orchestrator.constants import EventType

        events = event_bus.get_task_history("QR-42")
        failed_events = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed_events) == 1
        assert failed_events[0].data["session_id"] == "test-session-123"

    @pytest.mark.asyncio
    async def test_success_no_pr_fails_permanently_when_over_limit(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        dispatched,
        event_bus,
        mock_recovery,
        mock_tracker,
    ):
        """When no-PR limit reached → task fails permanently."""
        mock_recovery.record_no_pr.return_value = MagicMock(
            no_pr_count=3,
            should_retry_no_pr=False,
            should_retry=False,
            no_pr_cost=1.50,
            last_output=None,
        )
        result = AgentResult(
            success=True,
            output="Made changes but no PR",
            cost_usd=0.50,
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Should post comment and fail permanently
        mock_tracker.add_comment.assert_called()
        comment_text = mock_tracker.add_comment.call_args[0][1]
        assert "3 попытки без PR" in comment_text or "без создания PR после 3" in comment_text

        # TASK_FAILED event with retryable=False
        from orchestrator.constants import EventType

        events = event_bus.get_task_history("QR-42")
        failed_events = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed_events) == 1
        assert failed_events[0].data["retryable"] is False

    @pytest.mark.asyncio
    async def test_success_no_pr_with_rate_limit_fails_immediately(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        dispatched,
        event_bus,
        mock_recovery,
        mock_tracker,
    ):
        """When output contains rate-limit pattern → fail immediately."""
        mock_recovery.record_no_pr.return_value = MagicMock(
            no_pr_count=1,
            should_retry_no_pr=False,
            should_retry=False,
            last_output="You've hit your limit · resets 2pm (UTC)",
            no_pr_cost=2.83,
        )
        result = AgentResult(
            success=True,
            output="You've hit your limit · resets 2pm (UTC)",
            cost_usd=2.83,
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Should post comment and fail permanently
        mock_tracker.add_comment.assert_called()
        comment_text = mock_tracker.add_comment.call_args[0][1]
        assert "лимит" in comment_text or "API провайдера" in comment_text

        # TASK_FAILED event with retryable=False
        from orchestrator.constants import EventType

        events = event_bus.get_task_history("QR-42")
        failed_events = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed_events) == 1
        assert failed_events[0].data["retryable"] is False

    @pytest.mark.asyncio
    async def test_failure_retryable(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        dispatched,
        event_bus,
        mock_recovery,
    ):
        """When agent fails with retryable error → allow retry."""
        mock_recovery.record_failure.return_value = MagicMock(should_retry=True, attempt_count=1)
        result = AgentResult(
            success=False,
            output="Connection timeout",
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_recovery.record_failure.assert_called_once()
        assert "QR-42" not in dispatched
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_non_retryable(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        dispatched,
        event_bus,
        mock_recovery,
        mock_tracker,
    ):
        """When agent fails with non-retryable error → fail permanently."""
        mock_recovery.record_failure.return_value = MagicMock(should_retry=False, attempt_count=3)
        result = AgentResult(
            success=False,
            output="Auth error: unauthorized",
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_recovery.record_failure.assert_called_once()
        mock_tracker.add_comment.assert_called()
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_proposals_forwarded(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        event_bus,
        mock_recovery,
    ):
        """Proposals from the agent are processed."""
        result = AgentResult(
            success=True,
            output="Done",
            pr_url="https://github.com/org/repo/pull/1",
            proposals=[{"summary": "Add tests", "description": "Need more tests"}],
        )

        # Mock proposal_manager on the orchestrator_agent
        orchestrator_agent._proposal_manager = AsyncMock()
        orchestrator_agent._proposal_manager.process_proposals = AsyncMock()

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        orchestrator_agent._proposal_manager.process_proposals.assert_awaited_once_with(
            "QR-42", [{"summary": "Add tests", "description": "Need more tests"}]
        )


class TestHandleEpicChildEvent:
    """Test handle_epic_child_event: agent processes epic child outcomes."""

    @pytest.fixture
    def mock_store(self):
        store = MagicMock()
        store.is_epic_child.return_value = True
        store.on_task_completed = AsyncMock()
        store.on_task_failed = AsyncMock()
        store.on_task_cancelled = AsyncMock()
        return store

    @pytest.mark.asyncio
    async def test_child_completed_updates_state(self, orchestrator_agent, mock_store):
        """Child completion is forwarded to epic state store."""
        orchestrator_agent._epic_state_store = mock_store

        await orchestrator_agent.handle_epic_child_event(child_key="QR-11", event_type="completed")

        mock_store.is_epic_child.assert_called_with("QR-11")
        mock_store.on_task_completed.assert_awaited_once_with("QR-11")

    @pytest.mark.asyncio
    async def test_child_failed_updates_state(self, orchestrator_agent, mock_store):
        """Child failure is forwarded to epic state store."""
        orchestrator_agent._epic_state_store = mock_store

        await orchestrator_agent.handle_epic_child_event(child_key="QR-11", event_type="failed")

        mock_store.on_task_failed.assert_awaited_once_with("QR-11")

    @pytest.mark.asyncio
    async def test_child_cancelled_updates_state(self, orchestrator_agent, mock_store):
        """Child cancellation is forwarded to epic state store."""
        orchestrator_agent._epic_state_store = mock_store

        await orchestrator_agent.handle_epic_child_event(child_key="QR-11", event_type="cancelled")

        mock_store.on_task_cancelled.assert_awaited_once_with("QR-11")

    @pytest.mark.asyncio
    async def test_non_epic_child_is_ignored(self, orchestrator_agent, mock_store):
        """Non-epic-child key is silently ignored."""
        mock_store.is_epic_child.return_value = False
        orchestrator_agent._epic_state_store = mock_store

        await orchestrator_agent.handle_epic_child_event(child_key="QR-99", event_type="completed")

        mock_store.on_task_completed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_epic_store_is_noop(self, orchestrator_agent):
        """When no epic state store is configured, events are silently ignored."""
        orchestrator_agent._epic_state_store = None

        # Should not raise
        await orchestrator_agent.handle_epic_child_event(child_key="QR-11", event_type="completed")


class TestHandleSuccessNoPR:
    """Test _handle_success_no_pr: legitimate completions vs retries."""

    @pytest.mark.asyncio
    async def test_first_no_pr_completion_succeeds(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        event_bus,
        mock_recovery,
        cleanup_worktrees,
    ):
        """First no-PR completion without rate limit should complete successfully."""
        from orchestrator.constants import EventType
        from orchestrator.recovery import RecoveryState

        # Simulate first no-PR completion
        state = RecoveryState(issue_key="QR-42")
        state.no_pr_count = 1  # After record_no_pr increments
        state.last_output = "Task complete, no PR needed"
        state.no_pr_cost = 0.5
        mock_recovery.record_no_pr.return_value = state

        result = AgentResult(
            success=True,
            output="Task complete, no PR needed",
            pr_url=None,
            cost_usd=0.5,
            duration_seconds=60.0,
        )

        await orchestrator_agent._handle_success_no_pr(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
        )

        # Should emit TASK_COMPLETED, not TASK_FAILED
        events = event_bus.get_task_history("QR-42")
        completed_events = [e for e in events if e.type == EventType.TASK_COMPLETED]
        failed_events = [e for e in events if e.type == EventType.TASK_FAILED]

        assert len(completed_events) == 1, "Should emit TASK_COMPLETED for legitimate no-PR"
        assert len(failed_events) == 0, "Should not emit TASK_FAILED"

        # Should clean up
        mock_session.close.assert_awaited_once()
        cleanup_worktrees.assert_called_once()

        # Should NOT clear recovery state to prevent infinite retries if re-dispatched
        mock_recovery.clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_no_pr_preserves_recovery_state_for_re_dispatch(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        event_bus,
        mock_recovery,
        cleanup_worktrees,
    ):
        """Recovery state should persist after legitimate completion to bound re-dispatch retries."""
        from orchestrator.recovery import RecoveryState

        # First completion
        state = RecoveryState(issue_key="QR-42")
        state.no_pr_count = 1
        state.last_output = "Research complete"
        state.no_pr_cost = 0.5
        mock_recovery.record_no_pr.return_value = state

        result = AgentResult(
            success=True,
            output="Research complete",
            pr_url=None,
            cost_usd=0.5,
        )

        await orchestrator_agent._handle_success_no_pr(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
        )

        # Recovery state should NOT be cleared
        mock_recovery.clear.assert_not_called()

        # If task gets re-dispatched and completes without PR again,
        # it should continue from no_pr_count=1, not reset to 0


class TestOrchestratorDecisionEvent:
    """Test ORCHESTRATOR_DECISION event is published."""

    @pytest.mark.asyncio
    async def test_decision_event_published_on_result(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        event_bus,
        mock_recovery,
    ):
        from orchestrator.constants import EventType

        result = AgentResult(
            success=True,
            output="Created PR",
            pr_url="https://github.com/org/repo/pull/1",
            cost_usd=0.42,
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        events = event_bus.get_task_history("QR-42")
        decision_events = [e for e in events if e.type == EventType.ORCHESTRATOR_DECISION]
        assert len(decision_events) == 1
        assert decision_events[0].data["success"] is True
        assert decision_events[0].data["has_pr"] is True
        assert decision_events[0].data["cost"] == 0.42


class TestTrackerSignalBlocked:
    """Test tracker_signal_blocked tool (QR-247).

    Agents should use tracker_signal_blocked when blocked by another agent,
    instead of completing with success and relying on pattern matching.
    """

    @pytest.mark.asyncio
    async def test_signal_blocked_transitions_to_needs_info(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        event_bus,
        mock_needs_info_monitor,
    ):
        """Agent calls tracker_signal_blocked → treated as needs_info."""
        from orchestrator.constants import EventType

        # Simulate agent calling tracker_signal_blocked
        tool_state.blocked_by_agent = "QR-123"
        tool_state.blocking_reason = "Waiting for API contract"
        tool_state.needs_info_requested = True

        result = AgentResult(
            success=True,
            output="Cannot proceed, waiting for QR-123",
            needs_info=True,
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Should enter needs_info monitoring
        mock_needs_info_monitor.add.assert_called_once()

        # Should publish NEEDS_INFO event
        events = event_bus.get_task_history("QR-42")
        needs_info_events = [e for e in events if e.type == EventType.NEEDS_INFO]
        assert len(needs_info_events) == 1


class TestSuccessNoPrCompletion:
    """Test success without PR completion (QR-247).

    Agents can complete without PR for legitimate reasons (research, config, docs).
    If blocked by another agent, they should use tracker_signal_blocked instead.
    """

    @pytest.mark.asyncio
    async def test_success_no_pr_completes_normally(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        event_bus,
        mock_recovery,
    ):
        """Agent completes without PR → task marked as complete."""
        from orchestrator.constants import EventType
        from orchestrator.recovery import RecoveryState

        # Mock record_no_pr to return first completion state
        state = RecoveryState(issue_key="QR-42")
        state.no_pr_count = 1
        state.last_output = (
            "Research completed. Issue is caused by outdated auth-lib v1.2. Upgrade to v2.0 recommended."
        )
        state.no_pr_cost = 0.0
        mock_recovery.record_no_pr.return_value = state

        result = AgentResult(
            success=True,
            output="Research completed. Issue is caused by outdated auth-lib v1.2. Upgrade to v2.0 recommended.",
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Should record no-PR completion (not clear recovery state)
        mock_recovery.record_no_pr.assert_called_once()
        mock_session.close.assert_awaited_once()

        events = event_bus.get_task_history("QR-42")
        completed_events = [e for e in events if e.type == EventType.TASK_COMPLETED]
        assert len(completed_events) == 1
        assert completed_events[0].data["has_pr"] is False


class TestMailboxLifecycle:
    """Test that mailbox registration is managed correctly during result handling.

    Agents should stay registered while their session lives on in monitors,
    and only unregister when the session truly ends.
    """

    @pytest.mark.asyncio
    async def test_success_with_pr_keeps_mailbox_registration(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        mock_mailbox,
    ):
        """Agent that creates PR stays registered — session transfers to PR monitor."""
        result = AgentResult(
            success=True,
            output="Created PR",
            pr_url="https://github.com/org/repo/pull/123",
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_mailbox.unregister_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_needs_info_keeps_mailbox_registration(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        mock_mailbox,
    ):
        """Agent requesting info stays registered — session transfers to needs-info monitor."""
        tool_state.needs_info_requested = True
        tool_state.needs_info_text = "What endpoint?"
        result = AgentResult(success=True, output="Need info", needs_info=True)

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_mailbox.unregister_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_no_pr_unregisters_from_mailbox(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        mock_mailbox,
        mock_recovery,
    ):
        """Agent that completes without PR is unregistered — session is closed."""
        result = AgentResult(success=True, output="Done, no PR needed")

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_mailbox.unregister_agent.assert_called_once_with("QR-42")

    @pytest.mark.asyncio
    async def test_failure_unregisters_from_mailbox(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        mock_mailbox,
        mock_recovery,
    ):
        """Failed agent is unregistered — session is closed."""
        mock_recovery.record_failure.return_value = MagicMock(should_retry=False, attempt_count=3)
        result = AgentResult(success=False, output="Fatal error")

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_mailbox.unregister_agent.assert_called_once_with("QR-42")

    @pytest.mark.asyncio
    async def test_retryable_failure_unregisters_from_mailbox(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        mock_mailbox,
        mock_recovery,
    ):
        """Retryable failure also unregisters — session is closed for retry."""
        mock_recovery.record_failure.return_value = MagicMock(should_retry=True, attempt_count=1)
        result = AgentResult(success=False, output="Timeout")

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        mock_mailbox.unregister_agent.assert_called_once_with("QR-42")


class TestRateLimitHandling:
    """Test that is_rate_limited field is properly consumed."""

    @pytest.mark.asyncio
    async def test_rate_limited_result_is_retried_not_completed(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        dispatched,
        event_bus,
        mock_recovery,
        cleanup_worktrees,
    ):
        """When SDK returns success=True with is_rate_limited=True, treat as retryable failure.

        The bug: is_rate_limited field is detected and merged but never consumed.
        When send() returns success=True with is_rate_limited=True, orchestrator
        treats it as genuine success and completes the task. This test verifies
        the fix: rate-limited results should be retried, not completed.
        """
        mock_recovery.record_failure.return_value = MagicMock(should_retry=True, attempt_count=1)
        result = AgentResult(
            success=True,  # SDK returns success=True
            output="Rate limited by API",
            is_rate_limited=True,  # But was rate-limited
        )

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Should record failure and retry, NOT complete the task
        mock_recovery.record_failure.assert_called_once()

        # Should be removed from dispatched set to allow retry
        assert "QR-42" not in dispatched

        # Session should be closed for retry
        mock_session.close.assert_awaited_once()

        # Worktrees should be cleaned up for retry
        cleanup_worktrees.assert_called_once()

        # Should publish TASK_FAILED event (not TASK_COMPLETED)
        from orchestrator.constants import EventType

        events = event_bus.get_task_history("QR-42")
        failed_events = [e for e in events if e.type == EventType.TASK_FAILED]
        completed_events = [e for e in events if e.type == EventType.TASK_COMPLETED]

        assert len(failed_events) == 1, "Should publish TASK_FAILED event"
        assert failed_events[0].data["retryable"] is True, "Failure should be retryable"
        assert len(completed_events) == 0, "Should NOT publish TASK_COMPLETED event"

    @pytest.mark.asyncio
    async def test_rate_limited_with_confusing_output_still_retryable(
        self,
        orchestrator_agent,
        mock_issue,
        mock_session,
        workspace_state,
        tool_state,
        dispatched,
        mock_recovery,
    ):
        """Rate-limited results should be retried even if output contains non-retryable keywords.

        Bug: When is_rate_limited=True, _handle_failure calls record_failure
        without category=ErrorCategory.RATE_LIMIT. If agent output contains
        patterns like 'not found', 'auth', or 'budget', classify_error
        misclassifies it as NON_RETRYABLE and task permanently fails.

        This test verifies the fix: rate-limited failures should pass
        category=ErrorCategory.RATE_LIMIT to avoid misclassification.
        """
        # Simulate rate-limited result with output containing "not found"
        # which would be misclassified as CLI error (non-retryable)
        result = AgentResult(
            success=True,
            output="File not found while processing rate limit response",
            is_rate_limited=True,
        )

        # Mock recovery to use real classify_error logic
        from orchestrator.recovery import ErrorCategory, RecoveryAttempt, RecoveryState

        real_state = RecoveryState(issue_key="QR-42")

        def _record_failure(key, error, category=None):
            real_state.attempts.append(
                RecoveryAttempt(
                    timestamp=0.0,
                    category=category if category else ErrorCategory.CLI,
                    error_message=str(error),
                )
            )
            return real_state

        mock_recovery.record_failure.side_effect = _record_failure
        mock_recovery.get_state.return_value = real_state

        await orchestrator_agent.handle_result(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
            tool_state=tool_state,
        )

        # Verify record_failure was called with explicit RATE_LIMIT category
        mock_recovery.record_failure.assert_called_once()
        call_args = mock_recovery.record_failure.call_args
        assert call_args.kwargs.get("category") == ErrorCategory.RATE_LIMIT, (
            "record_failure should be called with category=ErrorCategory.RATE_LIMIT "
            "to prevent misclassification based on output text"
        )


class TestSessionLockCleanup:
    """Test that session locks are cleaned up in all no-PR paths.

    Session lock cleanup prevents memory leaks in the _session_locks dict.
    When locks are not cleaned up, they accumulate until orchestrator restart.
    """

    @pytest.fixture
    def mock_cleanup_session_lock(self):
        return MagicMock()

    @pytest.fixture
    def orchestrator_with_lock_cleanup(
        self,
        event_bus,
        mock_tracker,
        mock_pr_monitor,
        mock_needs_info_monitor,
        mock_recovery,
        dispatched,
        cleanup_worktrees,
        mock_mailbox,
        mock_cleanup_session_lock,
    ):
        from orchestrator.orchestrator_agent import OrchestratorAgent

        return OrchestratorAgent(
            event_bus=event_bus,
            tracker=mock_tracker,
            pr_monitor=mock_pr_monitor,
            needs_info_monitor=mock_needs_info_monitor,
            recovery=mock_recovery,
            dispatched_set=dispatched,
            cleanup_worktrees_callback=cleanup_worktrees,
            cleanup_session_lock=mock_cleanup_session_lock,
            mailbox=mock_mailbox,
        )

    @pytest.mark.asyncio
    async def test_no_pr_rate_limit_path_cleans_up_session_lock(
        self,
        orchestrator_with_lock_cleanup,
        mock_issue,
        mock_session,
        workspace_state,
        mock_recovery,
        mock_cleanup_session_lock,
    ):
        """Rate limit path should cleanup session lock."""
        mock_recovery.record_no_pr.return_value = MagicMock(
            no_pr_count=1,
            should_retry_no_pr=False,
            should_retry=False,
            last_output="You've hit your limit · resets 2pm (UTC)",
            no_pr_cost=2.83,
        )
        result = AgentResult(
            success=True,
            output="You've hit your limit · resets 2pm (UTC)",
            cost_usd=2.83,
        )

        await orchestrator_with_lock_cleanup._handle_success_no_pr(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
        )

        # Should cleanup session lock to prevent memory leak
        mock_cleanup_session_lock.assert_called_once_with("QR-42")

    @pytest.mark.asyncio
    async def test_no_pr_first_completion_path_cleans_up_session_lock(
        self,
        orchestrator_with_lock_cleanup,
        mock_issue,
        mock_session,
        workspace_state,
        mock_recovery,
        mock_cleanup_session_lock,
    ):
        """First no-PR completion path should cleanup session lock."""
        from orchestrator.recovery import RecoveryState

        state = RecoveryState(issue_key="QR-42")
        state.no_pr_count = 1
        state.last_output = "Task complete, no PR needed"
        state.no_pr_cost = 0.5
        mock_recovery.record_no_pr.return_value = state

        result = AgentResult(
            success=True,
            output="Task complete, no PR needed",
            cost_usd=0.5,
            duration_seconds=60.0,
        )

        await orchestrator_with_lock_cleanup._handle_success_no_pr(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
        )

        # Should cleanup session lock to prevent memory leak
        mock_cleanup_session_lock.assert_called_once_with("QR-42")

    @pytest.mark.asyncio
    async def test_no_pr_retry_path_cleans_up_session_lock(
        self,
        orchestrator_with_lock_cleanup,
        mock_issue,
        mock_session,
        workspace_state,
        mock_recovery,
        mock_cleanup_session_lock,
    ):
        """Retry path (2nd no-PR attempt) should cleanup session lock."""
        from orchestrator.recovery import RecoveryState

        state = RecoveryState(issue_key="QR-42")
        state.no_pr_count = 2  # Second attempt
        state.last_output = "Made changes but no PR again"
        state.no_pr_cost = 1.00
        mock_recovery.record_no_pr.return_value = state

        result = AgentResult(
            success=True,
            output="Made changes but no PR again",
            cost_usd=0.50,
        )

        await orchestrator_with_lock_cleanup._handle_success_no_pr(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
        )

        # Should cleanup session lock to prevent memory leak
        mock_cleanup_session_lock.assert_called_once_with("QR-42")

    @pytest.mark.asyncio
    async def test_no_pr_max_attempts_path_cleans_up_session_lock(
        self,
        orchestrator_with_lock_cleanup,
        mock_issue,
        mock_session,
        workspace_state,
        mock_recovery,
        mock_cleanup_session_lock,
    ):
        """Max attempts path should cleanup session lock."""
        mock_recovery.record_no_pr.return_value = MagicMock(
            no_pr_count=3,
            should_retry_no_pr=False,
            should_retry=False,
            no_pr_cost=1.50,
            last_output=None,
        )
        result = AgentResult(
            success=True,
            output="Made changes but no PR",
            cost_usd=0.50,
        )

        await orchestrator_with_lock_cleanup._handle_success_no_pr(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
        )

        # Should cleanup session lock to prevent memory leak
        mock_cleanup_session_lock.assert_called_once_with("QR-42")

    @pytest.mark.asyncio
    async def test_failure_paths_cleanup_session_lock_for_comparison(
        self,
        orchestrator_with_lock_cleanup,
        mock_issue,
        mock_session,
        workspace_state,
        mock_recovery,
        mock_cleanup_session_lock,
    ):
        """Verify failure paths DO cleanup session lock (for comparison).

        This test documents the existing correct behavior in _handle_failure.
        """
        # Test retryable failure path
        mock_recovery.record_failure.return_value = MagicMock(should_retry=True, attempt_count=1)
        result = AgentResult(
            success=False,
            output="Connection timeout",
        )

        await orchestrator_with_lock_cleanup._handle_failure(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
        )

        # Failure path DOES cleanup session lock
        mock_cleanup_session_lock.assert_called_once_with("QR-42")
        mock_cleanup_session_lock.reset_mock()

        # Test non-retryable failure path
        mock_recovery.record_failure.return_value = MagicMock(should_retry=False, attempt_count=3)
        result = AgentResult(
            success=False,
            output="Auth error: unauthorized",
        )

        await orchestrator_with_lock_cleanup._handle_failure(
            issue=mock_issue,
            result=result,
            session=mock_session,
            workspace_state=workspace_state,
        )

        # Failure path DOES cleanup session lock
        mock_cleanup_session_lock.assert_called_once_with("QR-42")


class TestContinuationExhausted:
    """Tests for continuation_exhausted handling in _handle_success_no_pr."""

    @pytest.fixture
    def _orchestrator_parts(
        self,
        event_bus,
        mock_tracker,
        mock_pr_monitor,
        mock_needs_info_monitor,
        mock_recovery,
    ):
        from orchestrator.orchestrator_agent import (
            OrchestratorAgent,
        )

        oa = OrchestratorAgent(
            event_bus=event_bus,
            tracker=mock_tracker,
            pr_monitor=mock_pr_monitor,
            needs_info_monitor=mock_needs_info_monitor,
            recovery=mock_recovery,
            dispatched_set=set(),
            cleanup_worktrees_callback=MagicMock(),
        )
        issue = MagicMock()
        issue.key = "QR-50"
        issue.summary = "Exhaustion test"
        session = AsyncMock()
        session.session_id = "sid-50"
        session.close = AsyncMock()
        ws = WorkspaceState(issue_key="QR-50")
        return oa, issue, session, ws, mock_recovery

    @pytest.mark.asyncio
    async def test_exhausted_retryable_publishes_failed(
        self,
        _orchestrator_parts,
        event_bus,
    ):
        """continuation_exhausted + should_retry → TASK_FAILED retryable."""
        oa, issue, session, ws, mock_recovery = _orchestrator_parts
        mock_recovery.record_no_pr.return_value = MagicMock(
            no_pr_count=2,
            should_retry_no_pr=True,
            last_output="output",
        )
        result = AgentResult(
            success=True,
            output="done",
            continuation_exhausted=True,
        )
        q = event_bus.subscribe_task("QR-50")
        await oa._handle_success_no_pr(
            issue,
            result,
            session,
            ws,
        )
        events = []
        while not q.empty():
            events.append(await q.get())
        failed = [e for e in events if e.type == "task_failed"]
        assert len(failed) == 1
        assert failed[0].data["retryable"] is True
        assert failed[0].data["continuation_exhausted"] is True

    @pytest.mark.asyncio
    async def test_exhausted_not_retryable_calls_fail_task(
        self,
        _orchestrator_parts,
        mock_recovery,
    ):
        """continuation_exhausted + not retryable → fail_task_impl."""
        oa, issue, session, ws, _ = _orchestrator_parts
        mock_recovery.record_no_pr.return_value = MagicMock(
            no_pr_count=3,
            should_retry_no_pr=False,
            last_output="output",
        )
        result = AgentResult(
            success=True,
            output="done",
            continuation_exhausted=True,
        )
        await oa._handle_success_no_pr(
            issue,
            result,
            session,
            ws,
        )
        # fail_task_impl closes session + publishes event
        session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_exhausted_first_no_pr_completes(
        self,
        _orchestrator_parts,
        event_bus,
    ):
        """First no-PR (count=1) without exhaustion → complete."""
        oa, issue, session, ws, mock_recovery = _orchestrator_parts
        mock_recovery.record_no_pr.return_value = MagicMock(
            no_pr_count=1,
            should_retry_no_pr=False,
            last_output="output",
        )
        result = AgentResult(
            success=True,
            output="research done",
        )
        q = event_bus.subscribe_task("QR-50")
        await oa._handle_success_no_pr(
            issue,
            result,
            session,
            ws,
        )
        events = []
        while not q.empty():
            events.append(await q.get())
        completed = [e for e in events if e.type == "task_completed"]
        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_exhausted_count_one_still_completes(
        self,
        _orchestrator_parts,
        event_bus,
    ):
        """Exhausted but no_pr_count==1 → not caught by guard."""
        oa, issue, session, ws, mock_recovery = _orchestrator_parts
        mock_recovery.record_no_pr.return_value = MagicMock(
            no_pr_count=1,
            should_retry_no_pr=False,
            last_output="output",
        )
        result = AgentResult(
            success=True,
            output="done",
            continuation_exhausted=True,
        )
        q = event_bus.subscribe_task("QR-50")
        await oa._handle_success_no_pr(
            issue,
            result,
            session,
            ws,
        )
        events = []
        while not q.empty():
            events.append(await q.get())
        # Falls through to no_pr_count==1 → complete
        completed = [e for e in events if e.type == "task_completed"]
        assert len(completed) == 1


class TestExternallyResolved:
    """Tests for externally_resolved handling in handle_result."""

    @pytest.fixture
    def _orchestrator_parts(
        self,
        event_bus,
        mock_tracker,
        mock_pr_monitor,
        mock_needs_info_monitor,
        mock_recovery,
    ):
        from orchestrator.orchestrator_agent import (
            OrchestratorAgent,
        )

        oa = OrchestratorAgent(
            event_bus=event_bus,
            tracker=mock_tracker,
            pr_monitor=mock_pr_monitor,
            needs_info_monitor=mock_needs_info_monitor,
            recovery=mock_recovery,
            dispatched_set=set(),
            cleanup_worktrees_callback=MagicMock(),
        )
        issue = MagicMock()
        issue.key = "QR-60"
        issue.summary = "External resolve test"
        session = AsyncMock()
        session.session_id = "sid-60"
        session.close = AsyncMock()
        ws = WorkspaceState(issue_key="QR-60")
        return oa, issue, session, ws, mock_recovery

    @pytest.mark.asyncio
    async def test_externally_resolved_publishes_cancelled(
        self,
        _orchestrator_parts,
        event_bus,
    ):
        """externally_resolved → TASK_FAILED with cancelled=True."""
        oa, issue, session, ws, _mock_recovery = _orchestrator_parts
        result = AgentResult(
            success=True,
            output="was working on it",
            externally_resolved=True,
        )
        q = event_bus.subscribe_task("QR-60")
        tool_state = ToolState()
        await oa.handle_result(
            issue,
            result,
            session,
            ws,
            tool_state,
        )
        events = []
        while not q.empty():
            events.append(await q.get())
        failed = [e for e in events if e.type == "task_failed"]
        assert len(failed) == 1
        assert failed[0].data["cancelled"] is True

    @pytest.mark.asyncio
    async def test_externally_resolved_records_cancelled(
        self,
        _orchestrator_parts,
    ):
        """externally_resolved → records CANCELLED in recovery."""
        oa, issue, session, ws, mock_recovery = _orchestrator_parts
        result = AgentResult(
            success=True,
            output="was working",
            externally_resolved=True,
        )
        tool_state = ToolState()
        await oa.handle_result(
            issue,
            result,
            session,
            ws,
            tool_state,
        )
        mock_recovery.record_failure.assert_called_once()
        call_kwargs = mock_recovery.record_failure.call_args
        from orchestrator.recovery import ErrorCategory

        assert call_kwargs.kwargs["category"] == ErrorCategory.CANCELLED

    @pytest.mark.asyncio
    async def test_externally_resolved_no_tracker_comment(
        self,
        _orchestrator_parts,
        mock_tracker,
    ):
        """externally_resolved → no Tracker comment posted."""
        oa, issue, session, ws, _ = _orchestrator_parts
        result = AgentResult(
            success=True,
            output="was working",
            externally_resolved=True,
        )
        tool_state = ToolState()
        await oa.handle_result(
            issue,
            result,
            session,
            ws,
            tool_state,
        )
        mock_tracker.add_comment.assert_not_called()
