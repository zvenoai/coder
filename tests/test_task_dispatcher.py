"""Tests for TaskDispatcher — PR review comment regressions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from orchestrator.agent_runner import AgentResult
from orchestrator.config import Config, RepoInfo, ReposConfig
from orchestrator.constants import EventType, PRState, ResolutionType
from orchestrator.task_dispatcher import TaskDispatcher

_TEST_REPO = RepoInfo(url="https://github.com/test/repo.git", path="/ws/repo", description="Test repo")


@dataclass
class FakeIssue:
    key: str = "QR-99"
    summary: str = "Test issue"
    description: str = "desc"
    components: list[str] | None = None
    tags: list[str] | None = None
    status: str = "open"
    type_key: str = "task"
    assignee: str | None = None
    parent_key: str | None = None

    def __post_init__(self):
        if self.components is None:
            self.components = ["Бекенд"]
        if self.tags is None:
            self.tags = ["ai-task"]


def make_config(**overrides) -> Config:
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(all_repos=[_TEST_REPO]),
        worktree_base_dir="/tmp/test-wt",
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_dispatcher(
    config: Config | None = None,
    epic_coordinator: AsyncMock | None = None,
    dependency_manager: MagicMock | None = None,
) -> tuple[TaskDispatcher, dict[str, MagicMock]]:
    """Create a TaskDispatcher with all mocked dependencies.

    Returns dispatcher and a dict of named mocks for assertions.
    """
    cfg = config or make_config()
    mocks = {
        "tracker": MagicMock(),
        "agent": AsyncMock(),
        "github": MagicMock(),
        "resolver": MagicMock(),
        "workspace": MagicMock(),
        "recovery": MagicMock(),
        "event_bus": AsyncMock(),
        "handle_result": AsyncMock(),
        "resume_needs_info": AsyncMock(return_value=False),
        "find_existing_pr": MagicMock(return_value=None),
        "cleanup_worktrees": MagicMock(),
    }

    # recovery — get_state returns a state with should_retry=True
    mocks["recovery"].get_state.return_value = MagicMock(should_retry=True)
    mocks["recovery"].wait_for_retry = AsyncMock()
    # Tracker returns resolved by default to skip continuation loop.
    # Tests for continuation override this.
    mocks["tracker"].get_issue.return_value = FakeIssue(status="closed")
    # agent.create_session returns a mock session
    mock_session = AsyncMock()
    mock_session.close = AsyncMock()
    mock_session.send = AsyncMock(
        return_value=AgentResult(
            success=True,
            output="done",
            pr_url=None,
            needs_info=False,
        )
    )
    # Prevent infinite drain loop: has_pending_messages must return False by default
    mock_session.has_pending_messages = MagicMock(return_value=False)
    mock_session.get_pending_message = MagicMock(return_value=None)
    mocks["agent"].create_session.return_value = mock_session

    dispatcher = TaskDispatcher(
        tracker=mocks["tracker"],
        agent=mocks["agent"],
        github=mocks["github"],
        resolver=mocks["resolver"],
        workspace=mocks["workspace"],
        recovery=mocks["recovery"],
        event_bus=mocks["event_bus"],
        config=cfg,
        semaphore=asyncio.Semaphore(1),
        dispatched_set=set(),
        tasks_set=set(),
        handle_result_callback=mocks["handle_result"],
        resume_needs_info_callback=mocks["resume_needs_info"],
        find_existing_pr_callback=mocks["find_existing_pr"],
        cleanup_worktrees_callback=mocks["cleanup_worktrees"],
        epic_coordinator=epic_coordinator,
        dependency_manager=dependency_manager,
    )
    return dispatcher, mocks


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestSessionCreation:
    """Session creation and lifecycle tests."""

    async def test_single_session_created(self, mock_bws) -> None:
        """One session is created for a normal dispatch."""
        dispatcher, mocks = make_dispatcher()

        await dispatcher._dispatch_task(FakeIssue())

        assert mocks["agent"].create_session.call_count == 1

    async def test_needs_info_skips_session_creation(self, mock_bws) -> None:
        """When issue is needs-info and resumed, no extra sessions should be created beyond what resume needs."""
        dispatcher, mocks = make_dispatcher()
        mocks["resume_needs_info"].return_value = True  # needs-info resumed

        await dispatcher._dispatch_task(FakeIssue(status="needsInfo"))

        # resume_needs_info was called and returned True, so dispatch exits early.
        # Only 1 session should be created (for the needs-info resume).
        assert mocks["agent"].create_session.call_count == 1

    async def test_needs_info_session_closed_when_not_resumed(self, mock_bws) -> None:
        """If needs-info status but _resume_needs_info returns False, the ni_session must be closed."""
        dispatcher, mocks = make_dispatcher()
        mocks["resume_needs_info"].return_value = False  # not actually resumed

        ni_session = mocks["agent"].create_session.return_value

        await dispatcher._dispatch_task(FakeIssue(status="needsInfo"))

        # The ni_session created for needs-info check should be closed
        # since resume returned False. A new session is created for the task.
        ni_session.close.assert_awaited()


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestNeedsInfoSessionFailure:
    """PR review: needs-info session failure leaves task permanently stuck.

    When create_session fails in the needs-info path, _dispatch_task must
    publish TASK_FAILED and discard from _dispatched so the task can be retried.
    """

    async def test_publishes_task_failed_on_session_error(self, mock_bws) -> None:
        """TASK_FAILED event must be published when needs-info session creation fails."""
        dispatcher, mocks = make_dispatcher()
        mocks["agent"].create_session.side_effect = RuntimeError("SDK init error")

        issue = FakeIssue(status="needsInfo")
        await dispatcher._dispatch_task(issue)

        # Check that TASK_FAILED was published
        published_events = [call.args[0].type for call in mocks["event_bus"].publish.call_args_list]
        assert EventType.TASK_FAILED in published_events, f"Expected TASK_FAILED event, got: {published_events}"

    async def test_discards_from_dispatched_on_session_error(self, mock_bws) -> None:
        """Task key must be removed from _dispatched so it can be re-dispatched."""
        dispatcher, mocks = make_dispatcher()
        mocks["agent"].create_session.side_effect = RuntimeError("SDK init error")

        # Simulate poll_and_dispatch adding key to dispatched set
        dispatcher._dispatched.add("QR-99")

        issue = FakeIssue(status="needsInfo")
        await dispatcher._dispatch_task(issue)

        assert "QR-99" not in dispatcher._dispatched


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestMainSessionFailure:
    """PR review: main session creation failure leaves task permanently stuck.

    Same issue as needs-info path — must publish TASK_FAILED and discard
    from _dispatched when create_session fails in the main path.
    """

    async def test_publishes_task_failed_on_main_session_error(self, mock_bws) -> None:
        """TASK_FAILED event must be published when main session creation fails."""
        dispatcher, mocks = make_dispatcher()
        mocks["agent"].create_session.side_effect = RuntimeError("SDK init error")

        issue = FakeIssue(status="open")  # not needs-info → hits main path
        await dispatcher._dispatch_task(issue)

        published_events = [call.args[0].type for call in mocks["event_bus"].publish.call_args_list]
        assert EventType.TASK_FAILED in published_events, f"Expected TASK_FAILED event, got: {published_events}"

    async def test_discards_from_dispatched_on_main_session_error(self, mock_bws) -> None:
        """Task key must be removed from _dispatched so it can be re-dispatched."""
        dispatcher, mocks = make_dispatcher()
        mocks["agent"].create_session.side_effect = RuntimeError("SDK init error")

        dispatcher._dispatched.add("QR-99")

        issue = FakeIssue(status="open")
        await dispatcher._dispatch_task(issue)

        assert "QR-99" not in dispatcher._dispatched


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestResumePRSessionFailure:
    """All error paths in _try_resume_pr must publish TASK_FAILED and discard from _dispatched."""

    async def test_publishes_task_failed_on_resume_pr_session_error(self, mock_bws) -> None:
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)
        mocks["agent"].create_session.side_effect = RuntimeError("SDK error")

        dispatcher._dispatched.add("QR-99")
        await dispatcher._dispatch_task(FakeIssue())

        published_events = [call.args[0].type for call in mocks["event_bus"].publish.call_args_list]
        assert EventType.TASK_FAILED in published_events
        assert "QR-99" not in dispatcher._dispatched

    async def test_publishes_task_failed_on_handle_result_error(self, mock_bws) -> None:
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)
        mocks["handle_result"].side_effect = RuntimeError("callback error")

        dispatcher._dispatched.add("QR-99")
        await dispatcher._dispatch_task(FakeIssue())

        published_events = [call.args[0].type for call in mocks["event_bus"].publish.call_args_list]
        assert EventType.TASK_FAILED in published_events
        assert "QR-99" not in dispatcher._dispatched


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestEmptyReposEarlyReturn:
    """PR review: early return when all_repos is empty leaks task in dispatched set."""

    async def test_discards_from_dispatched(self, mock_bws) -> None:
        cfg = make_config(repos_config=ReposConfig(all_repos=[]))
        dispatcher, _mocks = make_dispatcher(config=cfg)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" not in dispatcher._dispatched

    async def test_publishes_task_failed(self, mock_bws) -> None:
        cfg = make_config(repos_config=ReposConfig(all_repos=[]))
        dispatcher, mocks = make_dispatcher(config=cfg)

        await dispatcher._dispatch_task(FakeIssue())

        published_events = [call.args[0].type for call in mocks["event_bus"].publish.call_args_list]
        assert EventType.TASK_FAILED in published_events

    async def test_records_failure_to_prevent_infinite_retry(self, mock_bws) -> None:
        """record_failure must be called so should_retry eventually returns False.

        Without it, the task is re-dispatched every poll cycle (60s) forever,
        generating TASK_STARTED + TASK_FAILED pairs indefinitely.
        """
        cfg = make_config(repos_config=ReposConfig(all_repos=[]))
        dispatcher, mocks = make_dispatcher(config=cfg)

        await dispatcher._dispatch_task(FakeIssue())

        mocks["recovery"].record_failure.assert_called_once()
        call_args = mocks["recovery"].record_failure.call_args
        assert call_args[0][0] == "QR-99"


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestRetryabilityCheck:
    """All error paths must check should_retry from record_failure return value.

    Reference: main.py:_handle_agent_result uses state.should_retry to decide:
    - should_retry=True  → discard from dispatched (allow re-dispatch on next poll)
    - should_retry=False → do NOT discard (permanent failure, task stays blocked)
    Currently all error paths in task_dispatcher unconditionally discard.
    """

    # -- empty repos --

    async def test_empty_repos_permanent_keeps_in_dispatched(self, mock_bws) -> None:
        """Permanent failure (should_retry=False): task must stay in _dispatched."""
        cfg = make_config(repos_config=ReposConfig(all_repos=[]))
        dispatcher, mocks = make_dispatcher(config=cfg)
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=False)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" in dispatcher._dispatched

    async def test_empty_repos_retryable_discards(self, mock_bws) -> None:
        """Retryable failure (should_retry=True): task must be discarded."""
        cfg = make_config(repos_config=ReposConfig(all_repos=[]))
        dispatcher, mocks = make_dispatcher(config=cfg)
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=True)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" not in dispatcher._dispatched

    # -- needs-info session creation failure --

    async def test_ni_session_error_permanent_keeps_in_dispatched(self, mock_bws) -> None:
        """Permanent failure on needs-info session creation: must stay in _dispatched."""
        dispatcher, mocks = make_dispatcher()
        mocks["agent"].create_session.side_effect = RuntimeError("SDK error")
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=False)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue(status="needsInfo"))

        assert "QR-99" in dispatcher._dispatched

    async def test_ni_session_error_retryable_discards(self, mock_bws) -> None:
        """Retryable failure on needs-info session creation: must discard."""
        dispatcher, mocks = make_dispatcher()
        mocks["agent"].create_session.side_effect = RuntimeError("SDK error")
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=True)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue(status="needsInfo"))

        assert "QR-99" not in dispatcher._dispatched

    # -- main session creation failure --

    async def test_main_session_error_permanent_keeps_in_dispatched(self, mock_bws) -> None:
        """Permanent failure on main session creation: must stay in _dispatched."""
        dispatcher, mocks = make_dispatcher()
        mocks["agent"].create_session.side_effect = RuntimeError("SDK error")
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=False)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue(status="open"))

        assert "QR-99" in dispatcher._dispatched

    async def test_main_session_error_retryable_discards(self, mock_bws) -> None:
        """Retryable failure on main session creation: must discard."""
        dispatcher, mocks = make_dispatcher()
        mocks["agent"].create_session.side_effect = RuntimeError("SDK error")
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=True)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue(status="open"))

        assert "QR-99" not in dispatcher._dispatched

    # -- resume PR session creation failure --

    async def test_resume_pr_session_error_permanent_keeps_in_dispatched(self, mock_bws) -> None:
        """Permanent failure on resume-PR session creation: must stay in _dispatched."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)
        mocks["agent"].create_session.side_effect = RuntimeError("SDK error")
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=False)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" in dispatcher._dispatched

    async def test_resume_pr_session_error_retryable_discards(self, mock_bws) -> None:
        """Retryable failure on resume-PR session creation: must discard."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)
        mocks["agent"].create_session.side_effect = RuntimeError("SDK error")
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=True)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" not in dispatcher._dispatched

    # -- resume PR handle_result failure --

    async def test_resume_pr_handle_result_error_permanent_keeps_in_dispatched(self, mock_bws) -> None:
        """Permanent failure on handle_result for resumed PR: must stay in _dispatched."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)
        mocks["handle_result"].side_effect = RuntimeError("callback error")
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=False)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" in dispatcher._dispatched

    async def test_resume_pr_handle_result_error_retryable_discards(self, mock_bws) -> None:
        """Retryable failure on handle_result for resumed PR: must discard."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)
        mocks["handle_result"].side_effect = RuntimeError("callback error")
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=True)
        dispatcher._dispatched.add("QR-99")

        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" not in dispatcher._dispatched


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestMergedPRSkipsDispatch:
    """PR already merged should skip agent dispatch and close the tracker task."""

    async def test_merged_pr_skips_agent_dispatch(self, mock_bws) -> None:
        """When PR is already merged, agent session should NOT be created."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/17"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.MERGED)

        await dispatcher._dispatch_task(FakeIssue())

        mocks["agent"].create_session.assert_not_called()
        mocks["handle_result"].assert_not_called()

    async def test_merged_pr_closes_tracker_task(self, mock_bws) -> None:
        """When PR is already merged, tracker task should be closed with FIXED resolution."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/17"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.MERGED)

        await dispatcher._dispatch_task(FakeIssue())

        mocks["tracker"].transition_to_closed.assert_called_once()
        call_args = mocks["tracker"].transition_to_closed.call_args
        assert call_args[0][0] == "QR-99"
        assert call_args[1]["resolution"] == ResolutionType.FIXED
        assert "https://github.com/org/repo/pull/17" in call_args[1]["comment"]

    async def test_merged_pr_clears_recovery_state(self, mock_bws) -> None:
        """When PR is already merged, recovery state should be cleared."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/17"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.MERGED)

        await dispatcher._dispatch_task(FakeIssue())

        mocks["recovery"].clear.assert_called_once_with("QR-99")

    async def test_merged_pr_transition_failure_still_skips_dispatch(self, mock_bws) -> None:
        """If closing the tracker task fails, agent dispatch should still be skipped."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/17"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.MERGED)
        mocks["tracker"].transition_to_closed.side_effect = requests.RequestException("Tracker API error")

        await dispatcher._dispatch_task(FakeIssue())

        mocks["agent"].create_session.assert_not_called()

    async def test_closed_pr_allows_normal_dispatch(self, mock_bws) -> None:
        """When PR is closed (not merged), normal dispatch should proceed to allow retry."""
        dispatcher, mocks = make_dispatcher()
        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/17"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.CLOSED)

        await dispatcher._dispatch_task(FakeIssue())

        mocks["agent"].create_session.assert_called_once()
        mocks["tracker"].transition_to_in_progress.assert_called_once_with("QR-99")
        mocks["tracker"].transition_to_closed.assert_not_called()


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestRunningSessionTracking:
    """Running sessions are tracked during dispatch and removed after completion."""

    async def test_session_stored_during_send(self, mock_bws) -> None:
        """Session should be in _running_sessions while agent send() is running."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value
        captured_sessions: dict = {}

        original_send = mock_session.send

        async def capture_send(prompt):
            # Snapshot running sessions at the moment send is executing
            captured_sessions.update(dispatcher._running_sessions)
            return AgentResult(success=True, output="ok", proposals=[])

        mock_session.send = AsyncMock(side_effect=capture_send)

        await dispatcher._dispatch_task(FakeIssue())

        # During send(), session should have been in _running_sessions
        assert "QR-99" in captured_sessions

    async def test_session_removed_before_handle_result(self, mock_bws) -> None:
        """Session must be removed from _running_sessions BEFORE _handle_result.

        Bug: _handle_result can close/transfer the session (PR monitor, needs-info).
        If session stays in _running_sessions during _handle_result, a user interrupt
        arrives but session.send() on closed/transferred session causes errors.
        """
        dispatcher, mocks = make_dispatcher()
        captured_sessions: dict = {}

        async def capture_handle_result(issue, result, session, ws_state, tool_state):
            captured_sessions.update(dispatcher._running_sessions)

        mocks["handle_result"].side_effect = capture_handle_result
        await dispatcher._dispatch_task(FakeIssue())

        # Session must NOT be in _running_sessions when _handle_result executes
        assert "QR-99" not in captured_sessions

    async def test_session_removed_after_dispatch(self, mock_bws) -> None:
        """Session should be removed from _running_sessions after dispatch completes."""
        dispatcher, _mocks = make_dispatcher()
        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" not in dispatcher._running_sessions

    async def test_session_removed_on_failure(self, mock_bws) -> None:
        """Session should be removed from _running_sessions even if dispatch fails."""
        dispatcher, mocks = make_dispatcher()
        mocks["handle_result"].side_effect = RuntimeError("handling failed")

        with pytest.raises(RuntimeError):
            await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" not in dispatcher._running_sessions

    async def test_get_running_session_returns_session(self, mock_bws) -> None:
        """get_running_session should return the session while send() is running."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value
        captured_session = None

        async def capture_send(prompt):
            nonlocal captured_session
            captured_session = dispatcher.get_running_session("QR-99")
            return AgentResult(success=True, output="ok", proposals=[])

        mock_session.send = AsyncMock(side_effect=capture_send)

        await dispatcher._dispatch_task(FakeIssue())

        assert captured_session is not None

    async def test_get_running_session_returns_none_when_not_running(self, mock_bws) -> None:
        """get_running_session should return None for non-running tasks."""
        dispatcher, _mocks = make_dispatcher()
        assert dispatcher.get_running_session("QR-99") is None

    async def test_get_running_sessions_keys(self, mock_bws) -> None:
        """get_running_sessions should return keys of running sessions during send()."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value
        captured_keys: list = []

        async def capture_send(prompt):
            captured_keys.extend(dispatcher.get_running_sessions())
            return AgentResult(success=True, output="ok", proposals=[])

        mock_session.send = AsyncMock(side_effect=capture_send)

        await dispatcher._dispatch_task(FakeIssue())

        assert "QR-99" in captured_keys


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestMessageDrainLoop:
    """After session.send(), pending messages should be drained via additional send() calls."""

    async def test_drain_pending_messages(self, mock_bws) -> None:
        """drain_pending_messages is called after initial send."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value

        base_result = AgentResult(success=True, output="ok", proposals=[])
        drained_result = AgentResult(
            success=True,
            output="drained",
            proposals=[],
            cost_usd=0.1,
            duration_seconds=5.0,
        )
        mock_session.send = AsyncMock(return_value=base_result)
        mock_session.drain_pending_messages = AsyncMock(return_value=drained_result)

        await dispatcher._dispatch_task(FakeIssue())

        mock_session.drain_pending_messages.assert_awaited_once()

    async def test_drain_preserves_original_result(self, mock_bws) -> None:
        """drain_pending_messages result is passed to _handle_result."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value

        base_result = AgentResult(
            success=True,
            output="PR created",
            pr_url="https://github.com/org/repo/pull/42",
            cost_usd=1.5,
            duration_seconds=60.0,
            proposals=[{"title": "prop"}],
        )
        # Simulate drain merging: base pr_url preserved, costs accumulated
        drained_result = AgentResult(
            success=True,
            output="acknowledged",
            pr_url="https://github.com/org/repo/pull/42",
            cost_usd=1.6,
            duration_seconds=65.0,
            proposals=[{"title": "prop"}],
        )
        mock_session.send = AsyncMock(return_value=base_result)
        mock_session.drain_pending_messages = AsyncMock(return_value=drained_result)

        await dispatcher._dispatch_task(FakeIssue())

        handle_call = mocks["handle_result"].call_args
        result_arg = handle_call[0][1]
        assert result_arg.pr_url == "https://github.com/org/repo/pull/42"
        assert result_arg.cost_usd == 1.6
        assert result_arg.duration_seconds == 65.0
        assert result_arg.proposals == [{"title": "prop"}]

    async def test_handle_result_called_after_session_removed(self, mock_bws) -> None:
        """_handle_result must be called even when session is removed from _running_sessions.

        The session is popped before _handle_result (to prevent interrupts on
        a closing/transferring session), but _handle_result must still execute.
        """
        dispatcher, mocks = make_dispatcher()

        await dispatcher._dispatch_task(FakeIssue())

        mocks["handle_result"].assert_called_once()

    async def test_drain_returns_base_on_internal_error(self, mock_bws) -> None:
        """drain_pending_messages catches errors internally and returns base.

        The method's contract: exceptions do NOT propagate; the base result
        is preserved so _handle_result always receives a valid result.
        """
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value

        base_result = AgentResult(
            success=True,
            output="PR created",
            pr_url="https://github.com/org/repo/pull/99",
            proposals=[],
        )
        # drain returns base unchanged (simulating internal error catch)
        mock_session.send = AsyncMock(return_value=base_result)
        mock_session.drain_pending_messages = AsyncMock(return_value=base_result)

        await dispatcher._dispatch_task(FakeIssue())

        mocks["handle_result"].assert_called_once()
        result_arg = mocks["handle_result"].call_args[0][1]
        assert result_arg.pr_url == "https://github.com/org/repo/pull/99"

    async def test_drain_result_with_pr_updates_original(self, mock_bws) -> None:
        """drain_pending_messages merges PR from drain into result."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value

        base_result = AgentResult(
            success=True,
            output="working...",
            pr_url=None,
            cost_usd=1.0,
            duration_seconds=50.0,
            proposals=[],
        )
        drained = AgentResult(
            success=True,
            output="PR created",
            pr_url="https://github.com/org/repo/pull/77",
            cost_usd=1.5,
            duration_seconds=80.0,
            proposals=[{"title": "improvement"}],
        )
        mock_session.send = AsyncMock(return_value=base_result)
        mock_session.drain_pending_messages = AsyncMock(return_value=drained)

        await dispatcher._dispatch_task(FakeIssue())

        result_arg = mocks["handle_result"].call_args[0][1]
        assert result_arg.pr_url == "https://github.com/org/repo/pull/77"

    async def test_drain_result_with_needs_info_updates_original(self, mock_bws) -> None:
        """drain_pending_messages can set needs_info=True in result."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value

        base_result = AgentResult(
            success=True,
            output="done",
            needs_info=False,
            cost_usd=0.8,
            duration_seconds=40.0,
            proposals=[],
        )
        drained = AgentResult(
            success=True,
            output="need info",
            needs_info=True,
            cost_usd=1.0,
            duration_seconds=50.0,
            proposals=[],
        )
        mock_session.send = AsyncMock(return_value=base_result)
        mock_session.drain_pending_messages = AsyncMock(return_value=drained)

        await dispatcher._dispatch_task(FakeIssue())

        result_arg = mocks["handle_result"].call_args[0][1]
        assert result_arg.needs_info is True

    async def test_drain_accumulates_costs_across_multiple_sends(self, mock_bws) -> None:
        """drain_pending_messages returns accumulated result."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value

        base_result = AgentResult(
            success=True,
            output="step1",
            cost_usd=1.0,
            duration_seconds=30.0,
            proposals=[{"title": "p1"}],
        )
        drained = AgentResult(
            success=True,
            output="final",
            pr_url="https://github.com/org/repo/pull/88",
            cost_usd=1.8,
            duration_seconds=60.0,
            proposals=[{"title": "p1"}, {"title": "p2"}],
        )
        mock_session.send = AsyncMock(return_value=base_result)
        mock_session.drain_pending_messages = AsyncMock(return_value=drained)

        await dispatcher._dispatch_task(FakeIssue())

        result_arg = mocks["handle_result"].call_args[0][1]
        assert result_arg.pr_url == "https://github.com/org/repo/pull/88"
        assert result_arg.cost_usd == 1.8
        assert result_arg.duration_seconds == 60.0
        assert result_arg.proposals == [{"title": "p1"}, {"title": "p2"}]

    async def test_no_drain_when_no_pending(self, mock_bws) -> None:
        """When no pending messages, only the initial send should happen."""
        dispatcher, mocks = make_dispatcher()
        mock_session = mocks["agent"].create_session.return_value
        mock_session.has_pending_messages = MagicMock(return_value=False)

        await dispatcher._dispatch_task(FakeIssue())

        mock_session.send.assert_called_once()


class TestMergeResultsSuccessPreservation:
    """merge_results must not let a failed drain overwrite a successful base result."""

    def test_failed_drain_preserves_base_success(self) -> None:
        """When base.success=True (PR created) but drain fails (success=False),
        the merged result must remain success=True to avoid orphaning the PR."""
        from orchestrator.agent_runner import merge_results

        base = AgentResult(
            success=True,
            output="PR created",
            pr_url="https://github.com/org/repo/pull/42",
            cost_usd=1.0,
            duration_seconds=60.0,
            proposals=[],
        )
        drain = AgentResult(
            success=False,
            output="drain failed",
            error_category="timeout",
            cost_usd=0.1,
            duration_seconds=5.0,
            proposals=[],
        )
        merged = merge_results(base, drain)

        assert merged.success is True, "Failed drain must not overwrite base success"
        assert merged.pr_url == "https://github.com/org/repo/pull/42"
        assert merged.cost_usd == 1.1
        assert merged.duration_seconds == 65.0

    def test_successful_drain_overrides_base_failure(self) -> None:
        """When base failed but drain succeeded (created PR), merged should be success."""
        from orchestrator.agent_runner import merge_results

        base = AgentResult(
            success=False,
            output="initial failure",
            error_category="timeout",
            cost_usd=0.5,
            duration_seconds=30.0,
            proposals=[],
        )
        drain = AgentResult(
            success=True,
            output="PR created after retry",
            pr_url="https://github.com/org/repo/pull/55",
            cost_usd=0.3,
            duration_seconds=20.0,
            proposals=[],
        )
        merged = merge_results(base, drain)

        assert merged.success is True
        assert merged.pr_url == "https://github.com/org/repo/pull/55"

    def test_merge_uses_latest_token_counts_not_sum(self) -> None:
        """Token counts should use latest values (not accumulate) to avoid double-counting context.

        Each SDK call's input_tokens already includes the full conversation history.
        If we sum them, we double-count the context window usage.

        Example:
        - Main call: 100K input tokens (full history)
        - Drain call: 105K input tokens (same history + new prompt)
        - Merged should be 105K (latest), NOT 205K (sum)
        """
        from orchestrator.agent_runner import merge_results

        base = AgentResult(
            success=True,
            output="base output",
            cost_usd=0.5,
            duration_seconds=30.0,
            input_tokens=100000,  # Main call with conversation history
            output_tokens=5000,
            proposals=[],
        )
        drain = AgentResult(
            success=True,
            output="drain output",
            cost_usd=0.3,
            duration_seconds=20.0,
            input_tokens=105000,  # Drain call includes same history + new prompt
            output_tokens=3000,
            proposals=[],
        )
        merged = merge_results(base, drain)

        # Token counts should use latest values (like pr_url), not accumulate (like cost)
        assert merged.input_tokens == 105000  # Latest, not 205000
        assert merged.output_tokens == 3000  # Latest, not 8000 (prior outputs in input_tokens)
        assert merged.total_tokens == 108000  # 105000 + 3000 (current conversation size)
        # Cost and duration still accumulate
        assert merged.cost_usd == 0.8
        assert merged.duration_seconds == 50.0

    def test_merge_handles_zero_input_tokens_correctly(self) -> None:
        """When update.input_tokens is 0 (SDK didn't report usage), merge should use 0, not fallback to base."""
        from orchestrator.agent_runner import merge_results

        base = AgentResult(
            success=True,
            output="first",
            cost_usd=0.5,
            duration_seconds=30.0,
            input_tokens=100000,
            output_tokens=5000,
            proposals=[],
        )
        drain = AgentResult(
            success=True,
            output="second",
            cost_usd=0.3,
            duration_seconds=20.0,
            input_tokens=0,  # SDK didn't report usage (or legitimately 0)
            output_tokens=3000,
            proposals=[],
        )
        merged = merge_results(base, drain)

        # Should use latest value (0), not fallback to base.input_tokens (100000)
        assert merged.input_tokens == 0  # Latest, not 100000
        assert merged.output_tokens == 3000  # Latest, not sum

    def test_merge_total_tokens_does_not_double_count_output(self) -> None:
        """total_tokens should not overcount by summing output_tokens when they're already in input_tokens.

        Problem: SDK's input_tokens includes the full conversation history (including prior outputs).
        If we sum output_tokens across calls, total_tokens double-counts those outputs.

        Example:
        - First call: 100K input, 5K output
        - Second call: 105K input (includes the 100K + 5K from first call), 3K output
        - Merged total_tokens should be 105K + 3K = 108K (latest conversation size)
        - NOT 105K + 8K = 113K (which double-counts the first 5K output)
        """
        from orchestrator.agent_runner import merge_results

        base = AgentResult(
            success=True,
            output="first",
            cost_usd=0.5,
            duration_seconds=30.0,
            input_tokens=100000,  # First conversation
            output_tokens=5000,  # First output
            proposals=[],
        )
        drain = AgentResult(
            success=True,
            output="second",
            cost_usd=0.3,
            duration_seconds=20.0,
            input_tokens=105000,  # Includes 100K + 5K from first call
            output_tokens=3000,  # Second output
            proposals=[],
        )
        merged = merge_results(base, drain)

        # Latest conversation size, not overcounted
        assert merged.input_tokens == 105000
        assert merged.output_tokens == 3000  # Latest only, not sum
        assert merged.total_tokens == 108000  # 105K + 3K, not 113K

    def test_merge_preserves_tokens_when_drain_fails(self) -> None:
        """When drain send() fails, merge should preserve base tokens, not overwrite with zeros.

        Problem: Failed drain returns AgentResult with input_tokens=0, output_tokens=0 (defaults).
        Latest-wins strategy would overwrite base's real token counts with these zeros.
        This prevents compaction from triggering when context window is actually full.

        Solution: Only use drain tokens if they're non-zero (meaningful data).
        """
        from orchestrator.agent_runner import merge_results

        base = AgentResult(
            success=True,
            output="main call succeeded",
            cost_usd=0.5,
            duration_seconds=30.0,
            input_tokens=150000,  # Main call with large context
            output_tokens=10000,
            proposals=[],
        )
        drain = AgentResult(
            success=False,  # Drain failed
            output="Error: connection lost",
            cost_usd=0.0,  # No cost on failure
            duration_seconds=5.0,
            input_tokens=0,  # Defaults - no usage data
            output_tokens=0,
            proposals=[],
        )
        merged = merge_results(base, drain)

        # Should preserve base tokens, not overwrite with drain's zeros
        assert merged.input_tokens == 150000  # Preserved, not 0
        assert merged.output_tokens == 10000  # Preserved, not 0
        assert merged.total_tokens == 160000  # Correct for compaction decision


class TestNeedsInfoStatusFunction:
    """_is_needs_info_status should delegate to needs_info_monitor, not duplicate logic."""

    def test_uses_canonical_function(self) -> None:
        """task_dispatcher._is_needs_info_status should be the same function
        as needs_info_monitor.is_needs_info_status (no duplication)."""
        from orchestrator.needs_info_monitor import is_needs_info_status
        from orchestrator.task_dispatcher import _is_needs_info_status

        assert _is_needs_info_status is is_needs_info_status


class TestEpicDispatch:
    async def test_epic_is_registered_by_coordinator(self) -> None:
        epic_coordinator = AsyncMock()
        dispatcher, mocks = make_dispatcher(epic_coordinator=epic_coordinator)
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-200", type_key="epic")]
        mocks["recovery"].get_state.return_value = MagicMock(should_retry=True)
        dispatcher._dispatch_task = AsyncMock()

        await dispatcher.poll_and_dispatch()

        epic_coordinator.register_epic.assert_awaited_once()
        dispatcher._dispatch_task.assert_not_called()

    async def test_epic_discover_children_called_after_register(self) -> None:
        """After register_epic, discover_children must be called (not analyze_and_activate)."""
        epic_coordinator = AsyncMock()
        dispatcher, mocks = make_dispatcher(epic_coordinator=epic_coordinator)
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-200", type_key="epic")]
        mocks["recovery"].get_state.return_value = MagicMock(should_retry=True)
        dispatcher._dispatch_task = AsyncMock()

        await dispatcher.poll_and_dispatch()

        epic_coordinator.register_epic.assert_awaited_once()
        epic_coordinator.discover_children.assert_awaited_once_with("QR-200")

    async def test_epic_registration_failure_uses_recovery_and_unblocks(self) -> None:
        epic_coordinator = AsyncMock()
        epic_coordinator.register_epic.side_effect = ValueError("registration error")
        dispatcher, mocks = make_dispatcher(epic_coordinator=epic_coordinator)
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-201", type_key="epic")]
        mocks["recovery"].record_failure.return_value = MagicMock(should_retry=True)

        await dispatcher.poll_and_dispatch()

        mocks["recovery"].record_failure.assert_called_once()
        assert "QR-201" not in dispatcher._dispatched
        published_events = [call.args[0].type for call in mocks["event_bus"].publish.call_args_list]
        assert EventType.TASK_FAILED in published_events

    async def test_skips_epic_child_when_not_ready(self) -> None:
        epic_coordinator = MagicMock()
        epic_coordinator.is_epic_child.return_value = True
        epic_coordinator.is_child_ready_for_dispatch.return_value = False
        dispatcher, mocks = make_dispatcher(epic_coordinator=epic_coordinator)
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-210", type_key="task")]
        dispatcher._dispatch_task = AsyncMock()

        await dispatcher.poll_and_dispatch()

        dispatcher._dispatch_task.assert_not_called()
        epic_coordinator.mark_child_dispatched.assert_not_called()

    async def test_dispatches_epic_child_when_ready(self) -> None:
        epic_coordinator = MagicMock()
        epic_coordinator.is_epic_child.return_value = True
        epic_coordinator.is_child_ready_for_dispatch.return_value = True
        dispatcher, mocks = make_dispatcher(epic_coordinator=epic_coordinator)
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-211", type_key="task")]
        dispatcher._dispatch_task = AsyncMock()

        await dispatcher.poll_and_dispatch()

        dispatcher._dispatch_task.assert_called_once()
        epic_coordinator.mark_child_dispatched.assert_called_once_with("QR-211")


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestCompactionLoop:
    """Compaction loop: when context exceeds threshold, summarize and create new session."""

    async def test_compaction_triggered_when_tokens_high(self, mock_bws) -> None:
        """When should_compact returns True once, summarize_output is called,
        a new session is created, and continuation prompt is sent."""
        cfg = make_config(compaction_enabled=True)
        dispatcher, mocks = make_dispatcher(config=cfg)

        call_count = 0
        first_session = AsyncMock()
        first_session.has_pending_messages = MagicMock(return_value=False)
        second_session = AsyncMock()
        second_session.has_pending_messages = MagicMock(return_value=False)

        async def fake_first_send(prompt):
            return AgentResult(
                success=True, output="working...", input_tokens=170000, output_tokens=15000, proposals=[]
            )

        first_session.send = AsyncMock(side_effect=fake_first_send)
        first_session.close = AsyncMock()

        second_session.send = AsyncMock(
            return_value=AgentResult(success=True, output="done", input_tokens=50000, output_tokens=5000, proposals=[])
        )
        second_session.close = AsyncMock()

        def create_session_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_session
            return second_session

        mocks["agent"].create_session = AsyncMock(side_effect=create_session_side_effect)

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                side_effect=[True, False],  # compact once, then stop
            ),
            patch(
                "orchestrator.task_dispatcher.summarize_output",
                new_callable=AsyncMock,
                return_value="## Summary\nDid stuff",
            ) as mock_summarize,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # summarize_output was called with first session's output
        mock_summarize.assert_awaited_once()
        assert mock_summarize.call_args[0][0] == "working..."
        # First session was closed
        first_session.close.assert_awaited_once()
        # Second session was created and received continuation prompt
        assert call_count == 2
        continuation_prompt = second_session.send.call_args[0][0]
        assert "## Summary\nDid stuff" in continuation_prompt
        assert "QR-99" in continuation_prompt

    async def test_no_compaction_when_below_threshold(self, mock_bws) -> None:
        """When should_compact returns False, no compaction occurs."""
        dispatcher, mocks = make_dispatcher()

        with patch("orchestrator.task_dispatcher.should_compact", return_value=False):
            await dispatcher._dispatch_task(FakeIssue())

        # Only one session, one send call
        assert mocks["agent"].create_session.call_count == 1
        session = mocks["agent"].create_session.return_value
        session.send.assert_awaited_once()

    async def test_compaction_skipped_when_pr_created(self, mock_bws) -> None:
        """When result has pr_url, compaction loop should not trigger."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value
        session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="PR created",
                pr_url="https://github.com/org/repo/pull/1",
                input_tokens=190000,
                output_tokens=10000,
                proposals=[],
            )
        )

        with (
            patch("orchestrator.task_dispatcher.should_compact", return_value=True),
            patch("orchestrator.task_dispatcher.summarize_output", new_callable=AsyncMock) as mock_summarize,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        mock_summarize.assert_not_awaited()

    async def test_compaction_max_cycles_limit(self, mock_bws) -> None:
        """Compaction loop must not exceed MAX_COMPACTION_CYCLES iterations."""
        from orchestrator.constants import MAX_COMPACTION_CYCLES

        cfg = make_config(compaction_enabled=True)
        dispatcher, mocks = make_dispatcher(config=cfg)

        sessions: list[AsyncMock] = []
        for i in range(MAX_COMPACTION_CYCLES + 1):
            s = AsyncMock()
            s.has_pending_messages = MagicMock(return_value=False)
            s.send = AsyncMock(
                return_value=AgentResult(
                    success=True, output=f"output-{i}", input_tokens=190000, output_tokens=10000, proposals=[]
                )
            )
            s.close = AsyncMock()
            sessions.append(s)

        session_idx = 0

        def create_session_side_effect(*args, **kwargs):
            nonlocal session_idx
            s = sessions[min(session_idx, len(sessions) - 1)]
            session_idx += 1
            return s

        mocks["agent"].create_session = AsyncMock(side_effect=create_session_side_effect)

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                return_value=True,  # Always True — must be bounded by MAX_COMPACTION_CYCLES
            ),
            patch(
                "orchestrator.task_dispatcher.summarize_output",
                new_callable=AsyncMock,
                return_value="summary",
            ) as mock_summarize,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        assert mock_summarize.await_count == MAX_COMPACTION_CYCLES

    async def test_compaction_publishes_event(self, mock_bws) -> None:
        """COMPACTION_TRIGGERED event is published with cycle and tokens data."""
        cfg = make_config(compaction_enabled=True)
        dispatcher, mocks = make_dispatcher(config=cfg)

        first_session = AsyncMock()
        first_session.has_pending_messages = MagicMock(return_value=False)
        first_session.send = AsyncMock(
            return_value=AgentResult(
                success=True, output="working", input_tokens=170000, output_tokens=15000, proposals=[]
            )
        )
        first_session.close = AsyncMock()

        second_session = AsyncMock()
        second_session.has_pending_messages = MagicMock(return_value=False)
        second_session.send = AsyncMock(
            return_value=AgentResult(success=True, output="done", input_tokens=50000, output_tokens=5000, proposals=[])
        )

        call_count = 0

        def create_session_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_session
            return second_session

        mocks["agent"].create_session = AsyncMock(side_effect=create_session_side_effect)

        with (
            patch("orchestrator.task_dispatcher.should_compact", side_effect=[True, False]),
            patch(
                "orchestrator.task_dispatcher.summarize_output",
                new_callable=AsyncMock,
                return_value="summary",
            ),
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # Find COMPACTION_TRIGGERED event
        published_events = [call.args[0] for call in mocks["event_bus"].publish.call_args_list]
        compaction_events = [e for e in published_events if e.type == EventType.COMPACTION_TRIGGERED]
        assert len(compaction_events) == 1
        assert compaction_events[0].data["cycle"] == 1
        assert compaction_events[0].data["tokens"] == 185000  # 170000 + 15000

    async def test_compaction_transfers_pending_messages(self, mock_bws) -> None:
        """Compaction must transfer pending messages from old session to new."""
        cfg = make_config(compaction_enabled=True)
        dispatcher, mocks = make_dispatcher(config=cfg)

        first_session = AsyncMock()
        first_session.has_pending_messages = MagicMock(return_value=False)
        first_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="working",
                input_tokens=170000,
                output_tokens=15000,
                proposals=[],
            )
        )
        first_session.close = AsyncMock()
        first_session.transfer_pending_messages = MagicMock(return_value=0)

        second_session = AsyncMock()
        second_session.has_pending_messages = MagicMock(return_value=False)
        second_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="done",
                input_tokens=50000,
                output_tokens=5000,
                proposals=[],
            )
        )

        call_count = 0

        def create_session_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_session
            return second_session

        mocks["agent"].create_session = AsyncMock(side_effect=create_session_side_effect)

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                side_effect=[True, False],
            ),
            patch(
                "orchestrator.task_dispatcher.summarize_output",
                new_callable=AsyncMock,
                return_value="summary",
            ),
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # transfer_pending_messages must have been called on old session
        first_session.transfer_pending_messages.assert_called_once_with(second_session)

    async def test_compaction_updates_registry_before_close(self, mock_bws) -> None:
        """Registry must be updated before closing old session to avoid
        a race where interrupts land in the closed session's queue."""
        cfg = make_config(compaction_enabled=True)
        dispatcher, mocks = make_dispatcher(config=cfg)

        first_session = AsyncMock()
        first_session.has_pending_messages = MagicMock(return_value=False)
        first_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="working",
                input_tokens=170000,
                output_tokens=15000,
                proposals=[],
            )
        )
        first_session.transfer_pending_messages = MagicMock(return_value=0)

        second_session = AsyncMock()
        second_session.has_pending_messages = MagicMock(return_value=False)
        second_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="done",
                input_tokens=50000,
                output_tokens=5000,
                proposals=[],
            )
        )

        # Track the order of operations
        order: list[str] = []
        issue_key = FakeIssue().key

        original_close = first_session.close

        async def tracked_close():
            # At close time, registry must already point to new session
            current = dispatcher._running_sessions.get(issue_key)
            if current is second_session:
                order.append("registry_updated")
            order.append("close_called")
            return await original_close()

        first_session.close = AsyncMock(side_effect=tracked_close)

        call_count = 0

        def create_session_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_session
            return second_session

        mocks["agent"].create_session = AsyncMock(
            side_effect=create_session_side_effect,
        )

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                side_effect=[True, False],
            ),
            patch(
                "orchestrator.task_dispatcher.summarize_output",
                new_callable=AsyncMock,
                return_value="summary",
            ),
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # Registry was updated BEFORE close was called
        assert order == ["registry_updated", "close_called"]


class TestAbortedSessionSkipsHandleResult:
    """When a session is aborted (removed from _running_sessions externally),
    _dispatch_task must NOT call _handle_result — _abort_task already published
    a terminal TASK_FAILED event."""

    async def test_aborted_task_skips_handle_result(self) -> None:
        dispatcher, mocks = make_dispatcher()
        issue = FakeIssue(key="QR-300")

        # Simulate abort: when send() is called, remove session from _running_sessions
        # (this is what _abort_task does via remove_running_session)
        async def fake_send(prompt):
            dispatcher.remove_running_session("QR-300")
            return AgentResult(success=True, output="done", proposals=[])

        mock_session = AsyncMock()
        mock_session.send = AsyncMock(side_effect=fake_send)
        mock_session.has_pending_messages = MagicMock(return_value=False)
        mocks["agent"].create_session.return_value = mock_session

        await dispatcher._dispatch_task(issue)

        mocks["handle_result"].assert_not_called()


@patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
class TestSessionResumption:
    """Session resumption: persisted session_id is used when recreating sessions."""

    async def test_resume_pr_uses_persisted_session_id(self, mock_bws) -> None:
        """_try_resume_pr should pass persisted session_id to create_session."""
        find_pr_session_id = MagicMock(return_value="ses-pr-123")
        dispatcher, mocks = make_dispatcher()
        dispatcher._find_pr_session_id = find_pr_session_id

        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)

        await dispatcher._dispatch_task(FakeIssue())

        find_pr_session_id.assert_called_once_with("QR-99")
        # Verify create_session was called with resume_session_id
        call_kwargs = mocks["agent"].create_session.call_args.kwargs
        assert call_kwargs.get("resume_session_id") == "ses-pr-123"

    async def test_resume_pr_fallback_on_resume_failure(self, mock_bws) -> None:
        """When resume fails, should retry with fresh session."""
        find_pr_session_id = MagicMock(return_value="ses-pr-stale")
        dispatcher, mocks = make_dispatcher()
        dispatcher._find_pr_session_id = find_pr_session_id

        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)

        call_count = 0

        async def create_session_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and kwargs.get("resume_session_id"):
                raise RuntimeError("Session file not found")
            session = AsyncMock()
            session.send = AsyncMock(return_value=AgentResult(success=True, output="ok", proposals=[]))
            session.has_pending_messages = MagicMock(return_value=False)
            return session

        mocks["agent"].create_session = AsyncMock(side_effect=create_session_effect)

        await dispatcher._dispatch_task(FakeIssue())

        # Should have been called twice: once with resume, once without
        assert call_count == 2
        # handle_result was called (PR was tracked)
        mocks["handle_result"].assert_called_once()

    async def test_resume_pr_no_session_id_skips_resume(self, mock_bws) -> None:
        """When no persisted session_id, should create fresh session."""
        find_pr_session_id = MagicMock(return_value=None)
        dispatcher, mocks = make_dispatcher()
        dispatcher._find_pr_session_id = find_pr_session_id

        mocks["find_existing_pr"].return_value = "https://github.com/org/repo/pull/1"
        mocks["github"].get_pr_status.return_value = MagicMock(state=PRState.OPEN)

        await dispatcher._dispatch_task(FakeIssue())

        call_kwargs = mocks["agent"].create_session.call_args.kwargs
        assert call_kwargs.get("resume_session_id") is None

    async def test_needs_info_resume_uses_persisted_session_id(self, mock_bws) -> None:
        """Needs-info path should pass persisted session_id to create_session."""
        find_ni_session_id = MagicMock(return_value="ses-ni-456")
        dispatcher, mocks = make_dispatcher()
        dispatcher._find_ni_session_id = find_ni_session_id
        mocks["resume_needs_info"].return_value = True

        await dispatcher._dispatch_task(FakeIssue(status="needsInfo"))

        find_ni_session_id.assert_called_once_with("QR-99")
        call_kwargs = mocks["agent"].create_session.call_args.kwargs
        assert call_kwargs.get("resume_session_id") == "ses-ni-456"

    async def test_needs_info_fallback_on_resume_failure(self, mock_bws) -> None:
        """When needs-info resume fails, should retry with fresh session."""
        find_ni_session_id = MagicMock(return_value="ses-ni-stale")
        dispatcher, mocks = make_dispatcher()
        dispatcher._find_ni_session_id = find_ni_session_id
        mocks["resume_needs_info"].return_value = True

        call_count = 0

        async def create_session_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and kwargs.get("resume_session_id"):
                raise RuntimeError("Session file not found")
            session = AsyncMock()
            session.send = AsyncMock(return_value=AgentResult(success=True, output="ok", proposals=[]))
            session.has_pending_messages = MagicMock(return_value=False)
            session.close = AsyncMock()
            return session

        mocks["agent"].create_session = AsyncMock(side_effect=create_session_effect)

        await dispatcher._dispatch_task(FakeIssue(status="needsInfo"))

        assert call_count == 2
        mocks["resume_needs_info"].assert_called_once()


class TestPreflightIntegration:
    """Tests for preflight checker integration in poll_and_dispatch."""

    @pytest.mark.asyncio
    async def test_poll_runs_preflight_check(self):
        """poll_and_dispatch should run preflight check before dispatching."""
        dispatcher, mocks = make_dispatcher()
        preflight_checker = AsyncMock()
        preflight_checker.check.return_value = MagicMock(needs_review=False, reason="", source="")
        dispatcher._preflight_checker = preflight_checker

        issue = FakeIssue(key="QR-201")
        mocks["tracker"].search.return_value = [issue]

        with patch.object(dispatcher, "_dispatch_task", new=AsyncMock()) as mock_dispatch:
            await dispatcher.poll_and_dispatch()

            preflight_checker.check.assert_called_once()
            mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_defers_task_when_preflight_needs_review(self):
        """When preflight returns needs_review=True, task should be deferred via DependencyManager."""
        dep_mgr = MagicMock()
        dep_mgr.is_deferred.return_value = False
        dep_mgr.recheck_deferred = AsyncMock(return_value=[])
        dep_mgr.check_dependencies = AsyncMock(return_value=False)
        dep_mgr.defer_task = AsyncMock(return_value=True)

        dispatcher, mocks = make_dispatcher(dependency_manager=dep_mgr)
        preflight_checker = AsyncMock()
        preflight_checker.check.return_value = MagicMock(
            needs_review=True,
            reason="Git commits found: abc123 feat(QR-211): fix",
            source="evidence_collector",
            evidence=("Git commits found: abc123 feat(QR-211): fix",),
        )
        dispatcher._preflight_checker = preflight_checker

        issue = FakeIssue(key="QR-211")
        mocks["tracker"].search.return_value = [issue]

        with patch.object(dispatcher, "_dispatch_task", new=AsyncMock()) as mock_dispatch:
            await dispatcher.poll_and_dispatch()

            # Should NOT dispatch
            mock_dispatch.assert_not_called()
            # Should defer via dependency manager
            dep_mgr.defer_task.assert_called_once()
            call_args = dep_mgr.defer_task.call_args
            assert call_args[0][0] == "QR-211"
            assert "preflight_review:" in call_args[0][2]

    @pytest.mark.asyncio
    async def test_preflight_error_allows_dispatch(self):
        """When preflight check fails with error, task should still be dispatched (graceful fallback)."""
        dispatcher, mocks = make_dispatcher()
        preflight_checker = AsyncMock()
        preflight_checker.check.side_effect = RuntimeError("API error")
        dispatcher._preflight_checker = preflight_checker

        issue = FakeIssue(key="QR-201")
        mocks["tracker"].search.return_value = [issue]

        with patch.object(dispatcher, "_dispatch_task", new=AsyncMock()) as mock_dispatch:
            await dispatcher.poll_and_dispatch()

            # Task should be dispatched despite error
            mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_preflight_when_checker_not_configured(self):
        """When preflight_checker is None, tasks should be dispatched normally."""
        dispatcher, mocks = make_dispatcher()
        dispatcher._preflight_checker = None

        issue = FakeIssue(key="QR-201")
        mocks["tracker"].search.return_value = [issue]

        with patch.object(dispatcher, "_dispatch_task", new=AsyncMock()) as mock_dispatch:
            await dispatcher.poll_and_dispatch()

            mock_dispatch.assert_called_once()


class TestDependencyDeferral:
    """Integration tests for dependency manager in poll_and_dispatch."""

    async def test_deferred_tasks_excluded_from_dispatch(self) -> None:
        """Tasks already deferred by DependencyManager must be excluded from dispatch."""
        dep_mgr = MagicMock()
        dep_mgr.is_deferred.return_value = True
        dep_mgr.recheck_deferred = AsyncMock(return_value=[])
        dep_mgr.check_dependencies = AsyncMock(return_value=False)

        dispatcher, mocks = make_dispatcher(dependency_manager=dep_mgr)
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-300")]

        with patch.object(dispatcher, "_dispatch_task", new=AsyncMock()) as mock_dispatch:
            await dispatcher.poll_and_dispatch()

            mock_dispatch.assert_not_called()
            dep_mgr.is_deferred.assert_called_once_with("QR-300")

    async def test_dependency_check_runs_for_regular_tasks(self) -> None:
        """check_dependencies must be called for non-epic-child tasks."""
        dep_mgr = MagicMock()
        dep_mgr.is_deferred.return_value = False
        dep_mgr.recheck_deferred = AsyncMock(return_value=[])
        dep_mgr.check_dependencies = AsyncMock(return_value=True)  # deferred

        dispatcher, mocks = make_dispatcher(dependency_manager=dep_mgr)
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-301")]

        with patch.object(dispatcher, "_dispatch_task", new=AsyncMock()) as mock_dispatch:
            await dispatcher.poll_and_dispatch()

            dep_mgr.check_dependencies.assert_awaited_once_with("QR-301", "Test issue", description="desc")
            mock_dispatch.assert_not_called()

    async def test_dependency_check_skipped_for_epic_children(self) -> None:
        """Epic children must NOT go through dependency check — they have EpicCoordinator."""
        epic_coordinator = MagicMock()
        epic_coordinator.is_epic_child.return_value = True
        epic_coordinator.is_child_ready_for_dispatch.return_value = True

        dep_mgr = MagicMock()
        dep_mgr.is_deferred.return_value = False
        dep_mgr.recheck_deferred = AsyncMock(return_value=[])
        dep_mgr.check_dependencies = AsyncMock(return_value=False)

        dispatcher, mocks = make_dispatcher(
            epic_coordinator=epic_coordinator,
            dependency_manager=dep_mgr,
        )
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-302")]

        with patch.object(dispatcher, "_dispatch_task", new=AsyncMock()) as mock_dispatch:
            await dispatcher.poll_and_dispatch()

            dep_mgr.check_dependencies.assert_not_called()
            mock_dispatch.assert_called_once()

    async def test_recheck_deferred_runs_at_poll_start(self) -> None:
        """recheck_deferred must be called at the start of poll_and_dispatch."""
        dep_mgr = MagicMock()
        dep_mgr.is_deferred.return_value = False
        dep_mgr.recheck_deferred = AsyncMock(return_value=["QR-303"])
        dep_mgr.check_dependencies = AsyncMock(return_value=False)

        dispatcher, mocks = make_dispatcher(dependency_manager=dep_mgr)
        mocks["tracker"].search.return_value = []

        await dispatcher.poll_and_dispatch()

        dep_mgr.recheck_deferred.assert_awaited_once()

    async def test_works_without_dependency_manager(self) -> None:
        """Backward compat: when dependency_manager is None, dispatch works normally."""
        dispatcher, mocks = make_dispatcher(dependency_manager=None)
        mocks["tracker"].search.return_value = [FakeIssue(key="QR-304")]

        with patch.object(dispatcher, "_dispatch_task", new=AsyncMock()) as mock_dispatch:
            await dispatcher.poll_and_dispatch()

            mock_dispatch.assert_called_once()


@patch(
    "orchestrator.task_dispatcher.build_workspace_server",
    return_value=MagicMock(),
)
class TestTaskCostTracking:
    """Task cost tracking via _task_costs dict."""

    async def test_cost_tracked_after_send(
        self,
        mock_bws,
    ) -> None:
        """_task_costs updated after session.send()."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value
        session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="done",
                cost_usd=0.75,
            ),
        )

        await dispatcher._dispatch_task(FakeIssue(key="QR-C1"))

        # Cost should be cleaned up in finally
        assert dispatcher.get_task_cost("QR-C1") is None

    async def test_get_task_cost_returns_none_for_unknown(
        self,
        mock_bws,
    ) -> None:
        """get_task_cost returns None for unknown keys."""
        dispatcher, _ = make_dispatcher()
        assert dispatcher.get_task_cost("QR-NOPE") is None

    async def test_cost_cleaned_up_in_finally(
        self,
        mock_bws,
    ) -> None:
        """_task_costs entry removed after dispatch finishes."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value
        session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="done",
                cost_usd=1.23,
            ),
        )

        await dispatcher._dispatch_task(FakeIssue(key="QR-C2"))

        # After dispatch, entry is cleaned up
        assert "QR-C2" not in dispatcher._task_costs

    async def test_compaction_transfers_tokens(
        self,
        mock_bws,
    ) -> None:
        """Compaction calls transfer_cumulative_tokens."""
        cfg = make_config(compaction_enabled=True)
        dispatcher, mocks = make_dispatcher(config=cfg)

        first_session = AsyncMock()
        first_session.has_pending_messages = MagicMock(
            return_value=False,
        )
        first_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="working",
                input_tokens=170000,
                output_tokens=15000,
            ),
        )
        first_session.close = AsyncMock()
        first_session.transfer_pending_messages = MagicMock(
            return_value=0,
        )
        first_session.transfer_cumulative_tokens = MagicMock()

        second_session = AsyncMock()
        second_session.has_pending_messages = MagicMock(
            return_value=False,
        )
        second_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="done",
                input_tokens=50000,
                output_tokens=5000,
            ),
        )

        call_count = 0

        def create_session_side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_session
            return second_session

        mocks["agent"].create_session = AsyncMock(
            side_effect=create_session_side_effect,
        )

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                side_effect=[True, False],
            ),
            patch(
                "orchestrator.task_dispatcher.summarize_output",
                new_callable=AsyncMock,
                return_value="summary",
            ),
        ):
            await dispatcher._dispatch_task(FakeIssue())

        first_session.transfer_cumulative_tokens.assert_called_once_with(second_session)


# ----------------------------------------------------------------
# Multi-turn continuation tests
# ----------------------------------------------------------------


def _make_send_sequence(results: list[AgentResult]):
    """Return an AsyncMock.send that returns results in order."""
    call_idx = 0

    async def _send(prompt):
        nonlocal call_idx
        if call_idx < len(results):
            r = results[call_idx]
            call_idx += 1
            return r
        return results[-1]

    return _send


@patch(
    "orchestrator.task_dispatcher.build_workspace_server",
    return_value=MagicMock(),
)
class TestMultiTurnContinuation:
    """Continuation loop when agent finishes without PR."""

    async def test_continuation_when_no_pr(
        self,
        mock_bws,
    ) -> None:
        """Agent with no PR triggers continuation turns."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="no PR",
            pr_url=None,
        )
        with_pr = AgentResult(
            success=True,
            output="done",
            pr_url="https://github.com/o/r/pull/1",
        )
        session.send = AsyncMock(
            side_effect=[no_pr, with_pr],
        )
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )

        # Tracker says still open
        mocks["tracker"].get_issue.return_value = FakeIssue(status="inProgress")

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # Second send is the continuation prompt
        assert session.send.call_count == 2
        # Result passed to handle_result should have PR
        call_args = mocks["handle_result"].call_args
        result = call_args[0][1]
        assert result.pr_url is not None

    async def test_no_continuation_when_task_complete(
        self,
        mock_bws,
    ) -> None:
        """task_complete=True skips continuation."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="research done",
        )
        session.send = AsyncMock(return_value=no_pr)
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                return_value=False,
            ),
            patch(
                "orchestrator.task_dispatcher.ToolState",
            ) as mock_ts_cls,
        ):
            # ToolState().task_complete = True after first send
            ts = MagicMock()
            ts.task_complete = True
            ts.needs_info_requested = False
            ts.proposals = []
            mock_ts_cls.return_value = ts

            await dispatcher._dispatch_task(FakeIssue())

        # Only 1 send — no continuation
        assert session.send.call_count == 1

    async def test_no_continuation_when_pr_created(
        self,
        mock_bws,
    ) -> None:
        """PR on first turn skips continuation entirely."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        with_pr = AgentResult(
            success=True,
            output="done",
            pr_url="https://github.com/o/r/pull/1",
        )
        session.send = AsyncMock(return_value=with_pr)
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        assert session.send.call_count == 1

    async def test_tracker_resolved_sets_externally_resolved(
        self,
        mock_bws,
    ) -> None:
        """Resolved Tracker status → externally_resolved."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="no PR",
        )
        session.send = AsyncMock(return_value=no_pr)
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )
        mocks["tracker"].get_issue.return_value = FakeIssue(status="Closed")

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        call_args = mocks["handle_result"].call_args
        result = call_args[0][1]
        assert result.externally_resolved is True
        assert result.success is False

    async def test_no_continuation_when_rate_limited(
        self,
        mock_bws,
    ) -> None:
        """Rate-limited result skips continuation."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        rate_limited = AgentResult(
            success=True,
            output="hit limit",
            is_rate_limited=True,
        )
        session.send = AsyncMock(return_value=rate_limited)
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        assert session.send.call_count == 1

    async def test_cost_cap_stops_continuation(
        self,
        mock_bws,
    ) -> None:
        """max_continuation_cost prevents further turns."""
        cfg = make_config(max_continuation_cost=0.5)
        dispatcher, mocks = make_dispatcher(config=cfg)
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="no PR",
            cost_usd=1.0,
        )
        session.send = AsyncMock(return_value=no_pr)
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # Only initial send, cost cap prevents continuation
        assert session.send.call_count == 1

    async def test_stops_at_max_turns(
        self,
        mock_bws,
    ) -> None:
        """Stops after MAX_CONTINUATION_TURNS."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="no PR",
        )
        session.send = AsyncMock(return_value=no_pr)
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )
        mocks["tracker"].get_issue.return_value = FakeIssue(status="inProgress")

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                return_value=False,
            ),
            patch(
                "orchestrator.task_dispatcher.MAX_CONTINUATION_TURNS",
                3,
            ),
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # 1 initial + 3 continuation = 4 total sends
        assert session.send.call_count == 4
        # Result should have continuation_exhausted
        call_args = mocks["handle_result"].call_args
        result = call_args[0][1]
        assert result.continuation_exhausted is True

    async def test_pr_on_second_turn_not_exhausted(
        self,
        mock_bws,
    ) -> None:
        """PR on second turn means continuation_exhausted=False."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="no PR",
        )
        with_pr = AgentResult(
            success=True,
            output="done",
            pr_url="https://github.com/o/r/pull/1",
        )
        session.send = AsyncMock(
            side_effect=[no_pr, with_pr],
        )
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )
        mocks["tracker"].get_issue.return_value = FakeIssue(status="inProgress")

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        call_args = mocks["handle_result"].call_args
        result = call_args[0][1]
        assert result.continuation_exhausted is False
        assert result.pr_url is not None

    async def test_continuation_event_published(
        self,
        mock_bws,
    ) -> None:
        """CONTINUATION_TRIGGERED event published each turn."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="no PR",
        )
        with_pr = AgentResult(
            success=True,
            output="done",
            pr_url="https://github.com/o/r/pull/1",
        )
        session.send = AsyncMock(
            side_effect=[no_pr, with_pr],
        )
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )
        mocks["tracker"].get_issue.return_value = FakeIssue(status="inProgress")

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # Find continuation event
        calls = mocks["event_bus"].publish.call_args_list
        cont_events = [c for c in calls if c[0][0].type == EventType.CONTINUATION_TRIGGERED]
        assert len(cont_events) == 1
        assert cont_events[0][0][0].data["turn"] == 1

    async def test_tracker_api_failure_continues(
        self,
        mock_bws,
    ) -> None:
        """Tracker API failure doesn't stop continuation."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="no PR",
        )
        with_pr = AgentResult(
            success=True,
            output="done",
            pr_url="https://github.com/o/r/pull/1",
        )
        session.send = AsyncMock(
            side_effect=[no_pr, with_pr],
        )
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )
        mocks["tracker"].get_issue.side_effect = Exception(
            "API down",
        )

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        # Should still try continuation despite API error
        assert session.send.call_count == 2

    async def test_exhausted_without_task_complete(
        self,
        mock_bws,
    ) -> None:
        """All turns used + task_complete=False → exhausted."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="still working",
        )
        session.send = AsyncMock(return_value=no_pr)
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )
        mocks["tracker"].get_issue.return_value = FakeIssue(status="inProgress")

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                return_value=False,
            ),
            patch(
                "orchestrator.task_dispatcher.MAX_CONTINUATION_TURNS",
                2,
            ),
        ):
            await dispatcher._dispatch_task(FakeIssue())

        call_args = mocks["handle_result"].call_args
        result = call_args[0][1]
        assert result.continuation_exhausted is True
        assert result.success is True

    async def test_early_exit_not_exhausted(
        self,
        mock_bws,
    ) -> None:
        """Failure during continuation → not exhausted."""
        dispatcher, mocks = make_dispatcher()
        session = mocks["agent"].create_session.return_value

        no_pr = AgentResult(
            success=True,
            output="no PR",
        )
        failed = AgentResult(
            success=False,
            output="error",
        )
        session.send = AsyncMock(
            side_effect=[no_pr, failed],
        )
        session.drain_pending_messages = AsyncMock(
            side_effect=lambda r: r,
        )
        mocks["tracker"].get_issue.return_value = FakeIssue(status="inProgress")

        with patch(
            "orchestrator.task_dispatcher.should_compact",
            return_value=False,
        ):
            await dispatcher._dispatch_task(FakeIssue())

        call_args = mocks["handle_result"].call_args
        result = call_args[0][1]
        assert result.continuation_exhausted is False


@patch(
    "orchestrator.task_dispatcher.build_workspace_server",
    return_value=MagicMock(),
)
class TestRunCompactionLoop:
    """Tests for the extracted _run_compaction_loop helper."""

    async def test_extracted_helper_works(
        self,
        mock_bws,
    ) -> None:
        """_run_compaction_loop behaves like the old inline loop."""
        dispatcher, mocks = make_dispatcher()
        first_session = AsyncMock()
        first_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="first",
            ),
        )
        first_session.has_pending_messages = MagicMock(
            return_value=False,
        )
        first_session.transfer_pending_messages = MagicMock(
            return_value=0,
        )
        first_session.transfer_cumulative_tokens = MagicMock()
        first_session.close = AsyncMock()

        second_session = AsyncMock()
        second_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="compacted",
            ),
        )
        second_session.has_pending_messages = MagicMock(
            return_value=False,
        )
        mocks["agent"].create_session.return_value = second_session

        issue = FakeIssue()
        ts = MagicMock()
        ts.task_complete = False

        result = AgentResult(
            success=True,
            output="initial",
            input_tokens=100000,
            output_tokens=50000,
        )

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                side_effect=[True, False],
            ),
            patch(
                "orchestrator.task_dispatcher.summarize_output",
                new_callable=AsyncMock,
                return_value="summary",
            ),
        ):
            new_session, new_result = await dispatcher._run_compaction_loop(
                first_session,
                result,
                issue,
                ts,
                MagicMock(),
                "/tmp",
                "model",
            )

        assert new_session is second_session
        assert new_result.success is True
        first_session.close.assert_awaited_once()

    async def test_transfers_cumulative_tokens(
        self,
        mock_bws,
    ) -> None:
        """Compaction transfers cumulative tokens."""
        dispatcher, mocks = make_dispatcher()
        first_session = AsyncMock()
        first_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="first",
            ),
        )
        first_session.has_pending_messages = MagicMock(
            return_value=False,
        )
        first_session.transfer_pending_messages = MagicMock(
            return_value=0,
        )
        first_session.transfer_cumulative_tokens = MagicMock()
        first_session.close = AsyncMock()

        second_session = AsyncMock()
        second_session.send = AsyncMock(
            return_value=AgentResult(
                success=True,
                output="compacted",
            ),
        )
        mocks["agent"].create_session.return_value = second_session

        issue = FakeIssue()
        ts = MagicMock()
        ts.task_complete = False

        result = AgentResult(
            success=True,
            output="initial",
            input_tokens=100000,
            output_tokens=50000,
        )

        with (
            patch(
                "orchestrator.task_dispatcher.should_compact",
                side_effect=[True, False],
            ),
            patch(
                "orchestrator.task_dispatcher.summarize_output",
                new_callable=AsyncMock,
                return_value="summary",
            ),
        ):
            await dispatcher._run_compaction_loop(
                first_session,
                result,
                issue,
                ts,
                MagicMock(),
                "/tmp",
                "model",
            )

        first_session.transfer_cumulative_tokens.assert_called_once_with(
            second_session,
        )
