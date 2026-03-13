"""Tests for main module (async orchestrator)."""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import requests

from orchestrator.config import RepoInfo
from orchestrator.constants import EventType
from orchestrator.event_bus import Event
from orchestrator.workspace_tools import WorkspaceState

_TEST_REPO = RepoInfo(url="https://github.com/test/repo.git", path="/ws/repo")


def _make_mock_session(**send_kwargs) -> AsyncMock:
    """Create an AsyncMock session with drain-loop-safe defaults.

    All mock sessions used with _dispatch_task must have has_pending_messages
    and get_pending_message mocked to prevent infinite drain loops.
    drain_pending_messages passes through the result argument unchanged.
    """
    session = AsyncMock()
    session.has_pending_messages = MagicMock(return_value=False)
    session.get_pending_message = MagicMock(return_value=None)
    session.drain_pending_messages = AsyncMock(side_effect=lambda r: r)
    if send_kwargs:
        session.send.return_value = MagicMock(**send_kwargs)
    return session


async def drain_background_tasks(orch) -> None:
    """Await all in-flight fire-and-forget background tasks."""
    for t in list(orch._tasks):
        if not t.done():
            await t


def init_dispatcher_for_test(orch):
    """Initialize dispatcher for testing."""
    from orchestrator.task_dispatcher import TaskDispatcher

    if not hasattr(orch, "_pr_monitor") or orch._pr_monitor is None:
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.find_existing_pr.return_value = None

    if not hasattr(orch, "_needs_info_monitor") or orch._needs_info_monitor is None:
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.is_needs_info_status.return_value = False

    # OrchestratorAgent is normally initialized in run(), so mock it for tests
    if not hasattr(orch, "_orchestrator_agent") or orch._orchestrator_agent is None:
        orch._orchestrator_agent = MagicMock()
        orch._orchestrator_agent.handle_result = AsyncMock()

    orch._dispatcher = TaskDispatcher(
        tracker=orch._tracker,
        agent=orch._agent,
        github=orch._github,
        resolver=orch._resolver,
        workspace=orch._workspace,
        recovery=orch._recovery,
        event_bus=orch._event_bus,
        config=orch._config,
        semaphore=orch._semaphore,
        dispatched_set=orch._dispatched,
        tasks_set=orch._tasks,
        handle_result_callback=orch._orchestrator_agent.handle_result,
        resume_needs_info_callback=orch._resume_needs_info,
        find_existing_pr_callback=orch._pr_monitor.find_existing_pr,
        cleanup_worktrees_callback=orch._cleanup_worktrees,
    )


class TestOrchestrator:
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_poll_no_issues(self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.return_value = []
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        await orch._dispatcher.poll_and_dispatch()
        mock_tracker.search.assert_called_once()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_poll_dispatches_new_issues(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.return_value = [
            TrackerIssue(
                key="QR-10", summary="Test", description="", components=["Бекенд"], tags=["ai-task"], status="open"
            ),
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        # Mock dispatch_task to avoid actual agent execution
        orch._dispatcher._dispatch_task = AsyncMock()

        await orch._dispatcher.poll_and_dispatch()
        assert "QR-10" in orch._dispatched

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_skips_already_dispatched(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.return_value = [
            TrackerIssue(key="QR-10", summary="Test", description="", components=[], tags=[], status="open"),
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker
        orch._dispatched.add("QR-10")

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        await orch._dispatcher.poll_and_dispatch()
        # No new tasks should be created since QR-10 is already dispatched

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dispatch_task_no_repos(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        issue = TrackerIssue(key="QR-1", summary="T", description="", components=[], tags=[], status="open")
        await orch._dispatcher._dispatch_task(issue)
        # Should return early without error (no repos configured)

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_shutdown_event(self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            poll_interval_seconds=1,
        )

        orch = Orchestrator()
        orch._signal_handler()
        assert orch._shutdown.is_set()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_get_state_includes_epics(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )

        orch = Orchestrator()
        orch._epic_coordinator = MagicMock()
        orch._epic_coordinator.get_state.return_value = {"QR-50": {"phase": "executing"}}

        state = orch.get_state()
        assert state["epics"] == {"QR-50": {"phase": "executing"}}

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_routes_task_results(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._orchestrator_agent = MagicMock()
        orch._orchestrator_agent.handle_epic_child_event = AsyncMock()
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(Event(type=EventType.TASK_COMPLETED, task_key="QR-1", data={}))
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task

        orch._orchestrator_agent.handle_epic_child_event.assert_awaited_with("QR-1", "completed")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_skips_when_no_orchestrator_agent(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        # _orchestrator_agent is None by default (initialized in run())
        assert orch._orchestrator_agent is None
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(Event(type=EventType.TASK_COMPLETED, task_key="QR-xyz", data={}))
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task
        # No error — events are silently ignored when _orchestrator_agent is None

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_forwards_no_pr_task_completed(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Agent-driven completion: all TASK_COMPLETED events are real completions."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._orchestrator_agent = MagicMock()
        orch._orchestrator_agent.handle_epic_child_event = AsyncMock()
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(Event(type=EventType.TASK_COMPLETED, task_key="QR-123", data={"no_pr": True}))
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task

        orch._orchestrator_agent.handle_epic_child_event.assert_awaited_once_with("QR-123", "completed")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_routes_task_failed(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """TASK_FAILED should route to handle_epic_child_event with 'failed'."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._orchestrator_agent = MagicMock()
        orch._orchestrator_agent.handle_epic_child_event = AsyncMock()
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(Event(type=EventType.TASK_FAILED, task_key="QR-7", data={"error": "timeout"}))
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task

        orch._orchestrator_agent.handle_epic_child_event.assert_awaited_with("QR-7", "failed")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_routes_cancelled_as_cancelled(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """TASK_FAILED with cancelled=True should route as 'cancelled', not 'failed'."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._orchestrator_agent = MagicMock()
        orch._orchestrator_agent.handle_epic_child_event = AsyncMock()
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-8",
                data={"error": "Cancelled: no longer needed", "cancelled": True},
            )
        )
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task

        orch._orchestrator_agent.handle_epic_child_event.assert_awaited_with("QR-8", "cancelled")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_sends_plan_to_supervisor(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """EPIC_AWAITING_PLAN should trigger auto_send to supervisor chat."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._tasks_manager = AsyncMock()
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(
            Event(
                type=EventType.EPIC_AWAITING_PLAN,
                task_key="QR-50",
                data={
                    "children": [
                        {"key": "QR-51", "summary": "Task A", "status": "pending"},
                        {"key": "QR-52", "summary": "Task B", "status": "pending"},
                    ],
                },
            )
        )
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task

        orch._tasks_manager.auto_send.assert_awaited_once()
        prompt = orch._tasks_manager.auto_send.call_args[0][0]
        assert "QR-50" in prompt
        assert "epic_set_plan" in prompt

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_skips_plan_when_no_tasks_manager(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """EPIC_AWAITING_PLAN should be silently ignored when tasks_manager is None."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            supervisor_enabled=False,
        )
        orch = Orchestrator()
        assert orch._tasks_manager is None
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(
            Event(
                type=EventType.EPIC_AWAITING_PLAN,
                task_key="QR-50",
                data={"children": []},
            )
        )
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task
        # No error — event is silently ignored

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_cleans_pr_on_task_skipped(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """TASK_SKIPPED should cleanup tracked PR to avoid stale PR polling."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.cleanup = AsyncMock()
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_SKIPPED,
                task_key="QR-100",
                data={"reason": "test skip"},
            )
        )
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task

        orch._pr_monitor.cleanup.assert_called_once_with("QR-100")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_cleans_pr_on_task_completed(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """TASK_COMPLETED should cleanup tracked PR to avoid stale PR polling."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._orchestrator_agent = MagicMock()
        orch._orchestrator_agent.handle_epic_child_event = AsyncMock()
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.cleanup = AsyncMock()
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-101",
                data={},
            )
        )
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task

        orch._pr_monitor.cleanup.assert_called_once_with("QR-101")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_watch_epic_events_cleans_children_prs_on_epic_completed(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """EPIC_COMPLETED should cleanup all child PRs to avoid stale PR polling."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.epic_coordinator import ChildStatus, ChildTask, EpicState
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.cleanup = AsyncMock()
        orch._epic_coordinator = MagicMock()
        mock_state = EpicState(
            epic_key="QR-200",
            epic_summary="Test epic",
            children={
                "QR-201": ChildTask(
                    key="QR-201",
                    summary="Child 1",
                    status=ChildStatus.COMPLETED,
                    depends_on=[],
                    tracker_status="closed",
                ),
                "QR-202": ChildTask(
                    key="QR-202",
                    summary="Child 2",
                    status=ChildStatus.COMPLETED,
                    depends_on=[],
                    tracker_status="closed",
                ),
            },
        )
        orch._epic_coordinator.get_epic_state.return_value = mock_state
        import asyncio

        task = asyncio.create_task(orch._watch_epic_events())
        await asyncio.sleep(0)
        await orch._event_bus.publish(
            Event(
                type=EventType.EPIC_COMPLETED,
                task_key="QR-200",
                data={},
            )
        )
        await asyncio.sleep(0)
        orch._shutdown.set()
        await task

        # Verify cleanup was called for both children
        assert orch._pr_monitor.cleanup.call_count == 2
        orch._pr_monitor.cleanup.assert_any_call("QR-201")
        orch._pr_monitor.cleanup.assert_any_call("QR-202")


class TestCancelTask:
    """Test _cancel_task uses CANCELLED category and publishes cancellation event."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_cancel_prevents_retry(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Cancelled task should not be retried on next poll."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.recovery import ErrorCategory

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker
        orch._dispatched.add("QR-10")

        await orch._cancel_task("QR-10", "no longer needed")

        state = orch._recovery.get_state("QR-10")
        assert state.last_category == ErrorCategory.CANCELLED
        assert state.should_retry is False

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_cancel_publishes_event_with_cancelled_flag(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Cancelled task should publish TASK_FAILED with cancelled=True for epic routing."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        queue = orch._event_bus.subscribe_global()
        await orch._cancel_task("QR-11", "obsolete")

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        cancelled_events = [e for e in events if e.type == EventType.TASK_FAILED and e.data.get("cancelled")]
        assert len(cancelled_events) == 1
        assert cancelled_events[0].task_key == "QR-11"


class TestSuccessWithoutPr:
    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_comments_and_undispatches_on_success_without_pr(
        self,
        mock_gh_cls,
        mock_ws,
        mock_resolver_cls,
        mock_tracker_cls,
        mock_load_config,
        mock_bws,
    ) -> None:
        from orchestrator.agent_runner import AgentResult
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = []  # No existing PR
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker
        orch._github = MagicMock()
        # Disable continuation loop for this test
        orch._config = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
            max_continuation_cost=0.0,
        )

        mock_session = _make_mock_session()
        mock_session.send.return_value = AgentResult(
            success=True,
            output="Compilation failed" * 20,
            pr_url=None,
            cost_usd=3.85,
            duration_seconds=608.0,
        )
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session
        orch._recovery = MagicMock()
        orch._recovery.wait_for_retry = AsyncMock()
        # no_pr tracking removed — agent-driven completion

        # Pre-mark as dispatched
        orch._dispatched.add("QR-50")

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        queue = orch._event_bus.subscribe_global()

        issue = TrackerIssue(
            key="QR-50",
            summary="Test task",
            description="",
            components=["Бекенд"],
            tags=["ai-task"],
            status="open",
        )
        await orch._dispatcher._dispatch_task(issue)

        # Result handling is delegated to OrchestratorAgent.handle_result
        # (commenting, un-dispatching, recovery are tested in test_orchestrator_agent.py)
        assert orch._orchestrator_agent is not None
        orch._orchestrator_agent.handle_result.assert_awaited_once()
        call_args = orch._orchestrator_agent.handle_result.call_args
        called_issue = call_args[0][0]
        called_result = call_args[0][1]
        assert called_issue.key == "QR-50"
        assert called_result.success is True
        assert called_result.pr_url is None
        assert called_result.cost_usd == 3.85
        assert called_result.duration_seconds == 608.0

        orch._event_bus.unsubscribe_global(queue)

    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_handles_comment_failure_gracefully(
        self,
        mock_gh_cls,
        mock_ws,
        mock_resolver_cls,
        mock_tracker_cls,
        mock_load_config,
        mock_bws,
    ) -> None:
        from orchestrator.agent_runner import AgentResult
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = []
        mock_tracker.add_comment.side_effect = RuntimeError("API error")
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker
        orch._github = MagicMock()

        mock_session = _make_mock_session()
        mock_session.send.return_value = AgentResult(
            success=True,
            output="Done",
            pr_url=None,
            cost_usd=1.0,
            duration_seconds=60.0,
        )
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session
        orch._recovery = MagicMock()
        orch._recovery.wait_for_retry = AsyncMock()
        # no_pr tracking removed — agent-driven completion
        orch._dispatched.add("QR-51")

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        issue = TrackerIssue(
            key="QR-51",
            summary="T",
            description="",
            components=["Бекенд"],
            tags=["ai-task"],
            status="open",
        )
        # Should not raise — dispatch delegates result handling to OrchestratorAgent
        await orch._dispatcher._dispatch_task(issue)

        # handle_result was called (commenting/dispatched cleanup happens there)
        assert orch._orchestrator_agent is not None
        orch._orchestrator_agent.handle_result.assert_awaited_once()
        called_result = orch._orchestrator_agent.handle_result.call_args[0][1]
        assert called_result.success is True
        assert called_result.pr_url is None


class TestParsePrUrl:
    def test_valid_url(self) -> None:
        from orchestrator.pr_monitor import parse_pr_url

        result = parse_pr_url("https://github.com/org/repo/pull/42")
        assert result == ("org", "repo", 42)

    def test_dots_in_names(self) -> None:
        from orchestrator.pr_monitor import parse_pr_url

        result = parse_pr_url("https://github.com/my.org/my-repo.js/pull/123")
        assert result == ("my.org", "my-repo.js", 123)

    def test_invalid_url(self) -> None:
        from orchestrator.pr_monitor import parse_pr_url

        assert parse_pr_url("https://example.com/not-a-pr") is None

    def test_embedded_in_text(self) -> None:
        from orchestrator.pr_monitor import parse_pr_url

        result = parse_pr_url("Created PR: https://github.com/org/repo/pull/5 done")
        assert result == ("org", "repo", 5)


class TestTrackPr:
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_track_pr(self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        session = AsyncMock()
        workspace_state = WorkspaceState(issue_key="QR-1")

        orch._pr_monitor.track("QR-1", "https://github.com/org/repo/pull/42", session, workspace_state)
        assert "QR-1" in orch._pr_monitor.get_tracked()
        pr = orch._pr_monitor.get_tracked()["QR-1"]
        assert pr.owner == "org"
        assert pr.repo == "repo"
        assert pr.pr_number == 42

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_track_pr_invalid_url(self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._pr_monitor.track("QR-1", "not-a-url", AsyncMock(), WorkspaceState(issue_key="QR-1"))
        assert "QR-1" not in orch._pr_monitor.get_tracked()


class TestReviewWatcher:
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_closes_merged_pr(
        self, mock_gh_cls, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.github_client import PRStatus
        from orchestrator.main import Orchestrator
        from orchestrator.pr_monitor import TrackedPR

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            review_check_delay_seconds=0,
        )

        mock_gh = MagicMock()
        mock_gh.get_pr_status.return_value = PRStatus(state="MERGED", review_decision="APPROVED")
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._github = mock_gh

        session = AsyncMock()
        orch._pr_monitor._tracked_prs["QR-1"] = TrackedPR(
            issue_key="QR-1",
            pr_url="https://github.com/org/repo/pull/1",
            owner="org",
            repo="repo",
            pr_number=1,
            session=session,
            workspace_state=WorkspaceState(issue_key="QR-1"),
            last_check_at=0.0,
        )

        # Run one iteration then shutdown
        orch._shutdown.set()
        await orch._pr_monitor._check_all()

        session.close.assert_awaited_once()
        assert "QR-1" not in orch._pr_monitor.get_tracked()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_sends_reviews_to_agent(
        self, mock_gh_cls, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.agent_runner import AgentResult
        from orchestrator.config import Config, ReposConfig
        from orchestrator.github_client import PRStatus, ReviewThread, ThreadComment
        from orchestrator.main import Orchestrator
        from orchestrator.pr_monitor import TrackedPR

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            review_check_delay_seconds=0,
        )

        mock_gh = MagicMock()
        mock_gh.get_pr_status.return_value = PRStatus(state="OPEN", review_decision="CHANGES_REQUESTED")
        mock_gh.get_unresolved_threads.return_value = [
            ReviewThread(
                id="T_1",
                is_resolved=False,
                path="src/main.py",
                line=10,
                comments=[ThreadComment(author="reviewer", body="Fix this", created_at="2025-01-01")],
            ),
        ]
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._github = mock_gh

        session = AsyncMock()
        session.drain_pending_messages = AsyncMock(side_effect=lambda r: r)
        session.send.return_value = AgentResult(success=True, output="Fixed")
        orch._pr_monitor._tracked_prs["QR-1"] = TrackedPR(
            issue_key="QR-1",
            pr_url="https://github.com/org/repo/pull/1",
            owner="org",
            repo="repo",
            pr_number=1,
            session=session,
            workspace_state=WorkspaceState(issue_key="QR-1"),
            last_check_at=0.0,
        )

        # Run one iteration then shutdown
        orch._shutdown.set()
        await orch._pr_monitor._check_all()

        session.send.assert_awaited_once()
        prompt_arg = session.send.call_args[0][0]
        assert "Fix this" in prompt_arg
        assert "T_1" in orch._pr_monitor.get_tracked()["QR-1"].seen_thread_ids

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_skips_seen_threads(
        self, mock_gh_cls, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.github_client import PRStatus, ReviewThread, ThreadComment
        from orchestrator.main import Orchestrator
        from orchestrator.pr_monitor import TrackedPR

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            review_check_delay_seconds=0,
        )

        mock_gh = MagicMock()
        mock_gh.get_pr_status.return_value = PRStatus(state="OPEN", review_decision="")
        mock_gh.get_unresolved_threads.return_value = [
            ReviewThread(
                id="T_1",
                is_resolved=False,
                path="src/main.py",
                line=10,
                comments=[ThreadComment(author="r", body="old", created_at="2025-01-01")],
            ),
        ]
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._github = mock_gh

        session = AsyncMock()
        orch._pr_monitor._tracked_prs["QR-1"] = TrackedPR(
            issue_key="QR-1",
            pr_url="https://github.com/org/repo/pull/1",
            owner="org",
            repo="repo",
            pr_number=1,
            session=session,
            workspace_state=WorkspaceState(issue_key="QR-1"),
            last_check_at=0.0,
            seen_thread_ids={"T_1"},  # Already seen
        )

        orch._shutdown.set()
        await orch._pr_monitor._check_all()

        session.send.assert_not_awaited()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_skips_if_not_enough_time_elapsed(
        self, mock_gh_cls, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.pr_monitor import TrackedPR

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            review_check_delay_seconds=9999,
        )

        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._github = mock_gh

        session = AsyncMock()
        orch._pr_monitor._tracked_prs["QR-1"] = TrackedPR(
            issue_key="QR-1",
            pr_url="https://github.com/org/repo/pull/1",
            owner="org",
            repo="repo",
            pr_number=1,
            session=session,
            workspace_state=WorkspaceState(issue_key="QR-1"),
            last_check_at=time.monotonic(),  # Just checked
        )

        orch._shutdown.set()
        await orch._pr_monitor._check_all()

        # Should not have checked PR status at all due to rate limiting
        mock_gh.get_pr_status.assert_not_called()


class TestFindExistingPr:
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_finds_pr_in_comments(self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"text": "Started working"},
            {"text": "Created PR: https://github.com/org/repo/pull/42"},
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker

        result = orch._pr_monitor.find_existing_pr("QR-1")
        assert result == "https://github.com/org/repo/pull/42"

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_returns_none_when_no_pr(self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"text": "Just a comment"},
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker

        result = orch._pr_monitor.find_existing_pr("QR-1")
        assert result is None

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_returns_none_on_api_error(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.get_comments.side_effect = requests.ConnectionError("API error")
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker

        result = orch._pr_monitor.find_existing_pr("QR-1")
        assert result is None

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_prefers_newest_comment(self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"text": "Old PR: https://github.com/org/repo/pull/1"},
            {"text": "New PR: https://github.com/org/repo/pull/99"},
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker

        result = orch._pr_monitor.find_existing_pr("QR-1")
        assert result == "https://github.com/org/repo/pull/99"


class TestResumePr:
    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_resumes_open_pr(
        self, mock_gh_cls, mock_ws, mock_resolver_cls, mock_tracker_cls, mock_load_config, mock_bws
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.github_client import PRStatus
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"text": "PR: https://github.com/org/repo/pull/42"},
        ]
        mock_tracker_cls.return_value = mock_tracker

        mock_gh = MagicMock()
        mock_gh.get_pr_status.return_value = PRStatus(state="OPEN", review_decision="")
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._github = mock_gh

        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        issue = TrackerIssue(
            key="QR-1", summary="T", description="", components=["Бекенд"], tags=["ai-task"], status="open"
        )
        await orch._dispatcher._dispatch_task(issue)

        # handle_result should have been called with resumed PR result
        assert orch._orchestrator_agent is not None
        orch._orchestrator_agent.handle_result.assert_awaited_once()
        called_result = orch._orchestrator_agent.handle_result.call_args[0][1]
        assert called_result.pr_url == "https://github.com/org/repo/pull/42"
        assert called_result.resumed is True
        # Agent should NOT have been sent a task prompt
        mock_session.send.assert_not_awaited()

    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_resume_pr_event_includes_resumed_flag(
        self, mock_gh_cls, mock_ws, mock_resolver_cls, mock_tracker_cls, mock_load_config, mock_bws
    ) -> None:
        """PR_TRACKED event should include 'resumed: True' when PR is resumed."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.github_client import PRStatus
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"text": "PR: https://github.com/org/repo/pull/42"},
        ]
        mock_tracker_cls.return_value = mock_tracker

        mock_gh = MagicMock()
        mock_gh.get_pr_status.return_value = PRStatus(state="OPEN", review_decision="")
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._github = mock_gh

        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None

        issue = TrackerIssue(
            key="QR-2", summary="T", description="", components=["Бекенд"], tags=["ai-task"], status="open"
        )
        await orch._dispatcher._dispatch_task(issue)

        # handle_result should have been called with resumed PR result
        # (PR_TRACKED event publishing is now done inside OrchestratorAgent.handle_result,
        # tested in test_orchestrator_agent.py)
        assert orch._orchestrator_agent is not None
        orch._orchestrator_agent.handle_result.assert_awaited_once()
        called_result = orch._orchestrator_agent.handle_result.call_args[0][1]
        assert called_result.pr_url == "https://github.com/org/repo/pull/42"
        assert called_result.resumed is True

    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_does_not_resume_merged_pr(
        self, mock_gh_cls, mock_ws, mock_resolver_cls, mock_tracker_cls, mock_load_config, mock_bws
    ) -> None:
        from orchestrator.agent_runner import AgentResult
        from orchestrator.config import Config, ReposConfig
        from orchestrator.github_client import PRStatus
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"text": "PR: https://github.com/org/repo/pull/42"},
        ]
        mock_tracker_cls.return_value = mock_tracker

        mock_gh = MagicMock()
        mock_gh.get_pr_status.return_value = PRStatus(state="MERGED", review_decision="APPROVED")
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._github = mock_gh

        mock_session = _make_mock_session()
        mock_session.send.return_value = AgentResult(success=True, output="Done", pr_url=None)
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session
        orch._recovery = MagicMock()
        orch._recovery.wait_for_retry = AsyncMock()

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        issue = TrackerIssue(
            key="QR-1", summary="T", description="", components=["Бекенд"], tags=["ai-task"], status="open"
        )
        await orch._dispatcher._dispatch_task(issue)

        # Should NOT resume — PR is merged, should close the task and skip dispatch
        assert "QR-1" not in orch._pr_monitor.get_tracked()
        # Should close the task (not proceed with normal dispatch)
        mock_tracker.transition_to_closed.assert_called_once()
        # Should NOT transition to in-progress (dispatch was skipped)
        mock_tracker.transition_to_in_progress.assert_not_called()
        # Agent session should NOT be created (dispatch was skipped)
        orch._agent.create_session.assert_not_called()

    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_resume_pr_create_session_failure_cleans_worktrees(
        self, mock_gh_cls, mock_ws, mock_resolver_cls, mock_tracker_cls, mock_load_config, mock_bws
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.github_client import PRStatus
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"text": "PR: https://github.com/org/repo/pull/42"},
        ]
        mock_tracker_cls.return_value = mock_tracker

        mock_gh = MagicMock()
        mock_gh.get_pr_status.return_value = PRStatus(state="OPEN", review_decision="")
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._github = mock_gh

        resume_error = RuntimeError("resume session failed")
        orch._agent = AsyncMock()
        orch._agent.create_session.side_effect = resume_error
        orch._recovery = MagicMock()

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        issue = TrackerIssue(
            key="QR-3", summary="T", description="", components=["Бекенд"], tags=["ai-task"], status="open"
        )
        await orch._dispatcher._dispatch_task(issue)

        orch._recovery.record_failure.assert_called_once_with("QR-3", resume_error)
        # workspace_state is empty (no lazy worktrees created before failure)
        orch._workspace.cleanup_issue.assert_called_once_with("QR-3", [])

    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_resume_pr_handle_result_failure_cleans_worktrees(
        self, mock_gh_cls, mock_ws, mock_resolver_cls, mock_tracker_cls, mock_load_config, mock_bws
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.github_client import PRStatus
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"text": "PR: https://github.com/org/repo/pull/42"},
        ]
        mock_tracker_cls.return_value = mock_tracker

        mock_gh = MagicMock()
        mock_gh.get_pr_status.return_value = PRStatus(state="OPEN", review_decision="")
        mock_gh_cls.return_value = mock_gh

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._github = mock_gh

        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session
        resume_error = RuntimeError("resume handling failed")
        orch._orchestrator_agent = MagicMock()
        orch._orchestrator_agent.handle_result = AsyncMock(side_effect=resume_error)
        orch._recovery = MagicMock()

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        issue = TrackerIssue(
            key="QR-4", summary="T", description="", components=["Бекенд"], tags=["ai-task"], status="open"
        )
        await orch._dispatcher._dispatch_task(issue)

        orch._orchestrator_agent.handle_result.assert_awaited_once()
        orch._recovery.record_failure.assert_called_once_with("QR-4", resume_error)
        # workspace_state is empty (no lazy worktrees created before failure)
        orch._workspace.cleanup_issue.assert_called_once_with("QR-4", [])
        # Session must be closed to avoid resource leaks
        mock_session.close.assert_awaited_once()


class TestCleanupTrackedPr:
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_cleanup_removes_and_cleans_worktrees(
        self, mock_gh, mock_ws_cls, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.pr_monitor import TrackedPR
        from orchestrator.workspace import WorktreeInfo

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )

        mock_ws_inst = MagicMock()
        mock_ws_cls.return_value = mock_ws_inst

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        # Pre-populate workspace_state with a worktree to test cleanup
        ws = WorkspaceState(issue_key="QR-1")
        ws.created_worktrees.append(
            WorktreeInfo(path=Path("/wt/QR-1/repo"), branch="ai/QR-1", repo_path=Path("/ws/repo"))
        )
        repo_paths = ws.repo_paths
        orch._pr_monitor._tracked_prs["QR-1"] = TrackedPR(
            issue_key="QR-1",
            pr_url="https://github.com/org/repo/pull/1",
            owner="org",
            repo="repo",
            pr_number=1,
            session=AsyncMock(),
            workspace_state=ws,
            last_check_at=0.0,
        )

        await orch._pr_monitor.cleanup("QR-1")
        assert "QR-1" not in orch._pr_monitor.get_tracked()
        orch._workspace.cleanup_issue.assert_called_once_with("QR-1", repo_paths)


class TestNeedsInfo:
    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dispatch_tracks_needs_info(
        self,
        mock_gh_cls,
        mock_ws,
        mock_resolver_cls,
        mock_tracker_cls,
        mock_load_config,
        mock_bws,
    ) -> None:
        from orchestrator.agent_runner import AgentResult
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [{"id": 100, "text": "blocker info"}]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker
        orch._github = MagicMock()

        mock_session = _make_mock_session()
        mock_session.send.return_value = AgentResult(
            success=True,
            output="Need info",
            needs_info=True,
        )
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session
        orch._recovery = MagicMock()
        orch._recovery.wait_for_retry = AsyncMock()

        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )

        # Initialize needs info monitor for test
        from orchestrator.needs_info_monitor import NeedsInfoMonitor

        orch._bot_login = "bot"
        orch._needs_info_monitor = NeedsInfoMonitor(
            tracker=orch._tracker,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            bot_login=orch._bot_login,
            track_pr_callback=orch._pr_monitor.track,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
            get_latest_comment_id_callback=orch._get_latest_comment_id,
            dispatched_set=orch._dispatched,
        )

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        issue = TrackerIssue(
            key="QR-60",
            summary="Test",
            description="",
            components=["Бекенд"],
            tags=["ai-task"],
            status="open",
        )
        await orch._dispatcher._dispatch_task(issue)

        # Result handling is delegated to OrchestratorAgent.handle_result
        # (needs-info tracking is now done inside OrchestratorAgent.handle_result,
        # tested in test_orchestrator_agent.py)
        assert orch._orchestrator_agent is not None
        orch._orchestrator_agent.handle_result.assert_awaited_once()
        called_result = orch._orchestrator_agent.handle_result.call_args[0][1]
        assert called_result.needs_info is True

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_needs_info_watcher_detects_new_comment(
        self,
        mock_gh_cls,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        from orchestrator.agent_runner import AgentResult
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.needs_info_monitor import TrackedNeedsInfo
        from orchestrator.tracker_tools import ToolState

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            needs_info_check_delay_seconds=0,
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"id": 100, "text": "Bot question", "createdBy": {"login": "bot", "display": "Bot"}},
            {"id": 200, "text": "Human answer", "createdBy": {"login": "human", "display": "Human"}},
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._bot_login = "bot"

        # Initialize needs info monitor for test
        from orchestrator.needs_info_monitor import NeedsInfoMonitor

        orch._needs_info_monitor = NeedsInfoMonitor(
            tracker=orch._tracker,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            bot_login=orch._bot_login,
            track_pr_callback=orch._pr_monitor.track,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
            get_latest_comment_id_callback=orch._get_latest_comment_id,
            dispatched_set=orch._dispatched,
        )

        session = AsyncMock()
        session.drain_pending_messages = AsyncMock(side_effect=lambda r: r)
        session.send.return_value = AgentResult(
            success=True,
            output="Done",
            pr_url="https://github.com/org/repo/pull/99",
        )

        orch._needs_info_monitor.add(
            TrackedNeedsInfo(
                issue_key="QR-70",
                session=session,
                workspace_state=WorkspaceState(issue_key="QR-70"),
                tool_state=ToolState(),
                last_check_at=0.0,
                last_seen_comment_id=100,
            )
        )

        orch._shutdown.set()
        await orch._needs_info_monitor._check_all()

        session.send.assert_awaited_once()
        prompt_arg = session.send.call_args[0][0]
        assert "Human answer" in prompt_arg
        # Should have moved to PR tracking
        assert "QR-70" in orch._pr_monitor.get_tracked()
        assert "QR-70" not in orch._needs_info_monitor.get_tracked()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_needs_info_watcher_skips_bot_comments(
        self,
        mock_gh_cls,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.needs_info_monitor import TrackedNeedsInfo
        from orchestrator.tracker_tools import ToolState

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            needs_info_check_delay_seconds=0,
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"id": 100, "text": "Bot question", "createdBy": {"login": "bot", "display": "Bot"}},
            {"id": 200, "text": "Bot followup", "createdBy": {"login": "bot", "display": "Bot"}},
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._bot_login = "bot"

        # Initialize needs info monitor for test
        from orchestrator.needs_info_monitor import NeedsInfoMonitor

        orch._needs_info_monitor = NeedsInfoMonitor(
            tracker=orch._tracker,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            bot_login=orch._bot_login,
            track_pr_callback=orch._pr_monitor.track,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
            get_latest_comment_id_callback=orch._get_latest_comment_id,
            dispatched_set=orch._dispatched,
        )

        session = AsyncMock()
        orch._needs_info_monitor.add(
            TrackedNeedsInfo(
                issue_key="QR-71",
                session=session,
                workspace_state=WorkspaceState(issue_key="QR-71"),
                tool_state=ToolState(),
                last_check_at=0.0,
                last_seen_comment_id=100,
            )
        )

        orch._shutdown.set()
        await orch._needs_info_monitor._check_all()

        # Should NOT have sent anything to agent — all comments are from bot
        session.send.assert_not_awaited()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_needs_info_agent_creates_pr_after_response(
        self,
        mock_gh_cls,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        from orchestrator.agent_runner import AgentResult
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.needs_info_monitor import TrackedNeedsInfo
        from orchestrator.tracker_tools import ToolState

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            needs_info_check_delay_seconds=0,
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"id": 100, "text": "Question", "createdBy": {"login": "bot", "display": "Bot"}},
            {"id": 200, "text": "Answer", "createdBy": {"login": "user", "display": "User"}},
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._bot_login = "bot"

        # Initialize needs info monitor for test
        from orchestrator.needs_info_monitor import NeedsInfoMonitor

        orch._needs_info_monitor = NeedsInfoMonitor(
            tracker=orch._tracker,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            bot_login=orch._bot_login,
            track_pr_callback=orch._pr_monitor.track,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
            get_latest_comment_id_callback=orch._get_latest_comment_id,
            dispatched_set=orch._dispatched,
        )

        session = AsyncMock()
        session.drain_pending_messages = AsyncMock(side_effect=lambda r: r)
        session.send.return_value = AgentResult(
            success=True,
            output="Created PR",
            pr_url="https://github.com/org/repo/pull/55",
        )

        orch._needs_info_monitor.add(
            TrackedNeedsInfo(
                issue_key="QR-72",
                session=session,
                workspace_state=WorkspaceState(issue_key="QR-72"),
                tool_state=ToolState(),
                last_check_at=0.0,
                last_seen_comment_id=100,
            )
        )

        orch._shutdown.set()
        await orch._needs_info_monitor._check_all()

        # Should transition to PR tracking
        assert "QR-72" in orch._pr_monitor.get_tracked()
        assert orch._pr_monitor.get_tracked()["QR-72"].pr_url == "https://github.com/org/repo/pull/55"
        assert "QR-72" not in orch._needs_info_monitor.get_tracked()
        mock_tracker.transition_to_review.assert_called_once_with("QR-72")

    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_needs_info_resume_after_restart(
        self,
        mock_gh_cls,
        mock_ws,
        mock_resolver_cls,
        mock_tracker_cls,
        mock_load_config,
        mock_bws,
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [
            {"id": 50, "text": "Bot asked question", "createdBy": {"login": "bot"}},
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        # Initialize PR monitor for test
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._github = MagicMock()

        # Initialize needs info monitor for test
        from orchestrator.needs_info_monitor import NeedsInfoMonitor

        orch._bot_login = "bot"
        orch._needs_info_monitor = NeedsInfoMonitor(
            tracker=orch._tracker,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            bot_login=orch._bot_login,
            track_pr_callback=orch._pr_monitor.track,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
            get_latest_comment_id_callback=orch._get_latest_comment_id,
            dispatched_set=orch._dispatched,
        )

        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None  # for mypy

        # Issue with needInfo status (simulating restart)
        issue = TrackerIssue(
            key="QR-80",
            summary="Test",
            description="",
            components=["Бекенд"],
            tags=["ai-task"],
            status="needInfo",
        )
        await orch._dispatcher._dispatch_task(issue)

        # Should have entered needs-info monitoring, NOT run agent
        assert "QR-80" in orch._needs_info_monitor.get_tracked()
        ni = orch._needs_info_monitor.get_tracked()["QR-80"]
        assert ni.last_seen_comment_id == 50
        mock_session.send.assert_not_awaited()

    @patch("orchestrator.task_dispatcher.build_workspace_server", return_value=MagicMock())
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_needs_info_resume_skips_backoff(
        self,
        mock_gh_cls,
        mock_ws,
        mock_resolver_cls,
        mock_tracker_cls,
        mock_load_config,
        mock_bws,
    ) -> None:
        """Needs-info resume must not wait for recovery backoff."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(all_repos=[_TEST_REPO]),
            worktree_base_dir="/tmp/test-wt",
        )

        mock_tracker = MagicMock()
        mock_tracker.get_comments.return_value = [{"id": 10, "text": "info"}]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        from orchestrator.pr_monitor import PRMonitor

        orch._pr_monitor = PRMonitor(
            tracker=orch._tracker,
            github=orch._github,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
        )
        orch._tracker = mock_tracker
        orch._github = MagicMock()

        from orchestrator.needs_info_monitor import NeedsInfoMonitor

        orch._bot_login = "bot"
        orch._needs_info_monitor = NeedsInfoMonitor(
            tracker=orch._tracker,
            event_bus=orch._event_bus,
            proposal_manager=orch._proposal_manager,
            config=orch._config,
            semaphore=orch._semaphore,
            session_locks=orch._session_locks,
            shutdown_event=orch._shutdown,
            bot_login=orch._bot_login,
            track_pr_callback=orch._pr_monitor.track,
            cleanup_worktrees_callback=orch._cleanup_worktrees,
            get_latest_comment_id_callback=orch._get_latest_comment_id,
            dispatched_set=orch._dispatched,
        )

        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        mock_recovery = MagicMock()
        mock_recovery.wait_for_retry = AsyncMock()
        mock_recovery.should_retry.return_value = True
        orch._recovery = mock_recovery

        init_dispatcher_for_test(orch)
        assert orch._dispatcher is not None

        issue = TrackerIssue(
            key="QR-90",
            summary="Waiting for info",
            description="",
            components=["Бекенд"],
            tags=["ai-task"],
            status="needInfo",
        )
        await orch._dispatcher._dispatch_task(issue)

        # Should have entered needs-info monitoring
        assert "QR-90" in orch._needs_info_monitor.get_tracked()
        # wait_for_retry must NOT have been called — needs-info resumes immediately
        mock_recovery.wait_for_retry.assert_not_awaited()


class TestProposalApproveConfig:
    """Test that proposal approve uses config instead of hardcoded values."""

    async def test_approve_uses_config_for_project_and_boards(self) -> None:
        """Approve should read project_id and boards from config,
        not hardcode magic numbers."""
        from orchestrator.config import Config
        from orchestrator.event_bus import EventBus
        from orchestrator.proposal_manager import ProposalManager, StoredProposal

        tracker = MagicMock()
        tracker.create_issue.return_value = {"key": "QR-100"}
        tracker.add_link.return_value = {}
        event_bus = EventBus()
        config = MagicMock(spec=Config)
        config.tracker_queue = "QR"
        config.tracker_project_id = 42
        config.tracker_boards = [99]
        config.component_assignee_map = {
            "backend": ("Backend", "test.user"),
        }

        pm = ProposalManager(tracker, event_bus, config)
        # Manually add a pending proposal
        pm._proposals["abc123"] = StoredProposal(
            id="abc123",
            source_task_key="QR-50",
            summary="Test improvement",
            description="Some description",
            component="backend",
            category="tooling",
        )

        await pm.approve("abc123")

        # Verify create_issue was called with config values, not hardcoded 13/[14]
        call_kwargs = tracker.create_issue.call_args
        assert call_kwargs.kwargs.get("project_id") == 42, (
            f"Expected project_id=42 from config, got {call_kwargs.kwargs.get('project_id')}"
        )
        assert call_kwargs.kwargs.get("boards") == [99], (
            f"Expected boards=[99] from config, got {call_kwargs.kwargs.get('boards')}"
        )


class TestEventTypesEnum:
    """Test that all event type strings used in the codebase are in EventType enum."""

    def test_all_event_types_in_enum(self) -> None:
        """Every event type string literal in the codebase should be
        a member of EventType enum for consistency."""
        from orchestrator.constants import EventType

        all_event_types = {
            # proposal_manager.py
            "task_proposed",
            "proposal_approved",
            "proposal_rejected",
            # agent_runner.py
            "agent_output",
            "agent_result",
            # main.py
            "user_message",
            # needs_info_monitor.py
            "needs_info_response",
        }
        enum_values = {e.value for e in EventType}
        missing = all_event_types - enum_values
        assert not missing, f"Event types not in EventType enum: {missing}"


class TestNeedsInfoPRTrackingSummary:
    """PR tracked from needs-info path should carry issue_summary."""

    def test_track_pr_callback_passes_issue_summary(self) -> None:
        """TrackPRCallback should include issue_summary so PR monitor has it."""
        import typing

        from orchestrator.needs_info_monitor import TrackPRCallback

        hints = typing.get_args(TrackPRCallback)
        # hints[0] is the parameter types list, hints[1] is return type
        param_types = hints[0]
        assert len(param_types) == 5, (
            f"TrackPRCallback should have 5 params (including issue_summary), got {len(param_types)}"
        )


class TestStorageInitFailure:
    """When storage init fails in run(), supervisor and recovery should not hold stale references."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_supervisor_storage_cleared_on_init_failure(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """If storage.open() fails, supervisor must not hold a broken storage reference."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            supervisor_enabled=True,
        )
        mock_tracker_cls.return_value = MagicMock()

        orch = Orchestrator()
        # Storage and supervisor are initialized in __init__
        storage = orch._storage
        supervisor = orch._supervisor
        assert storage is not None
        assert supervisor is not None

        # Simulate storage.open() failure
        storage.open = AsyncMock(side_effect=RuntimeError("DB open failed"))

        # Run the init part by calling run() which we'll abort after init
        orch._shutdown.set()  # Immediately stop
        mock_tracker_cls.return_value.get_myself_login.return_value = "bot"
        await orch.run()

        # After failure, both orchestrator and supervisor should have None storage
        assert orch._storage is None
        assert supervisor._storage is None

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_storage_closed_on_partial_init_failure(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """If open() succeeds but later init step fails, storage should be closed before discarding."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker_cls.return_value = MagicMock()

        orch = Orchestrator()
        assert orch._storage is not None

        # open() succeeds, but make recovery.load() fail
        mock_open = AsyncMock()
        mock_close = AsyncMock()
        orch._storage.open = mock_open
        orch._storage.close = mock_close
        orch._recovery.load = AsyncMock(side_effect=RuntimeError("load failed"))

        orch._shutdown.set()
        mock_tracker_cls.return_value.get_myself_login.return_value = "bot"
        await orch.run()

        # Storage should have been closed before being discarded
        mock_close.assert_called_once()
        assert orch._storage is None


class TestOrphanedCollectorStopped:
    """If recovery.load() fails after collector.start(), the collector must be stopped."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_collector_stopped_on_recovery_load_failure(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker_cls.return_value = MagicMock()

        orch = Orchestrator()
        assert orch._storage is not None

        mock_open = AsyncMock()
        mock_close = AsyncMock()
        orch._storage.open = mock_open
        orch._storage.close = mock_close

        # Collector mock to verify stop() is called
        mock_collector = MagicMock()
        mock_collector.start = AsyncMock()
        mock_collector.stop = AsyncMock()

        # Patch StatsCollector constructor to return our mock
        with patch("orchestrator.main.StatsCollector", return_value=mock_collector):
            orch._recovery.load = AsyncMock(side_effect=RuntimeError("load failed"))
            orch._shutdown.set()
            mock_tracker_cls.return_value.get_myself_login.return_value = "bot"
            await orch.run()

        # Collector should have been started then stopped
        mock_collector.start.assert_called_once()
        mock_collector.stop.assert_called_once()


class TestNeedsInfoStatusNormalization:
    """Test that _NEEDS_INFO_STATUSES has no dead entries after normalization."""

    def test_frozenset_has_no_dead_entries(self) -> None:
        """Every entry in _NEEDS_INFO_STATUSES must already be in normalized form,
        otherwise it's dead code that can never be matched."""
        from orchestrator.needs_info_monitor import _NEEDS_INFO_STATUSES

        for entry in _NEEDS_INFO_STATUSES:
            normalized = entry.lower().replace("-", "").replace("_", "").replace(" ", "")
            assert normalized == entry, f"Entry '{entry}' is dead code — normalized form is '{normalized}'"


class TestSendMessageRunningSession:
    """send_message_to_agent should route to running sessions in the dispatcher."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_routes_to_running_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        mock_session = _make_mock_session()
        mock_session.interrupt_with_message = AsyncMock()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = mock_session
        orch._dispatcher = mock_dispatcher
        orch._event_bus = AsyncMock()

        await orch.send_message_to_agent("QR-1", "stop and fix X")

        mock_session.interrupt_with_message.assert_awaited_once_with("stop and fix X")
        orch._event_bus.publish.assert_awaited_once()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_falls_through_to_pr_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When no running session, should fall through to PR monitor session."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher

        mock_session = _make_mock_session()
        mock_pr_monitor = MagicMock()
        mock_pr_monitor.get_tracked.return_value = {"QR-1": MagicMock(session=mock_session)}
        orch._pr_monitor = mock_pr_monitor
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._event_bus = AsyncMock()

        await orch.send_message_to_agent("QR-1", "hello")

        await drain_background_tasks(orch)

        mock_session.send.assert_awaited_once_with("hello")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_raises_when_no_session_id_available(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When no session exists and no session_id in storage — raises ValueError."""
        import pytest

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = None

        with pytest.raises(ValueError, match="No session_id"):
            await orch.send_message_to_agent("QR-1", "hello")


class TestGetStateRunning:
    """get_state() should include running_sessions from the dispatcher."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_includes_running_sessions(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = ["QR-1", "QR-2"]
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        state = orch.get_state()
        assert state["running_sessions"] == ["QR-1", "QR-2"]

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_empty_running_sessions_when_no_dispatcher(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()
        # dispatcher is None before run() is called

        state = orch.get_state()
        assert state["running_sessions"] == []


class TestCancelTaskRecording:
    """_cancel_task should record failure exactly once and post comment."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_records_failure_exactly_once(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        # No running session — _abort_task will raise ValueError (caught)
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._recovery = MagicMock()
        orch._event_bus = AsyncMock()

        await orch._cancel_task("QR-10", "no longer needed")

        from orchestrator.recovery import ErrorCategory

        orch._recovery.record_failure.assert_called_once_with(
            "QR-10", "Cancelled: no longer needed", category=ErrorCategory.CANCELLED
        )

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_removes_from_dispatched(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._recovery = MagicMock()
        orch._event_bus = AsyncMock()
        orch._dispatched.add("QR-10")

        await orch._cancel_task("QR-10", "done")

        assert "QR-10" not in orch._dispatched


class TestAbortTask:
    """_abort_task should interrupt, close session, remove from running, publish TASK_FAILED."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_aborts_running_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        mock_session = AsyncMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = mock_session
        orch._dispatcher = mock_dispatcher
        orch._event_bus = AsyncMock()
        orch._mailbox = MagicMock()
        orch._mailbox.unregister_agent = AsyncMock()
        orch._dispatched.add("QR-20")

        await orch._abort_task("QR-20")

        mock_session.interrupt.assert_awaited_once()
        mock_session.close.assert_awaited_once()
        mock_dispatcher.remove_running_session.assert_called_once_with("QR-20")
        assert "QR-20" not in orch._dispatched
        orch._event_bus.publish.assert_awaited_once()
        # Mailbox must unregister the agent to prevent stale discovery
        orch._mailbox.unregister_agent.assert_called_once_with("QR-20")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_raises_when_no_running_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        import pytest

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher

        with pytest.raises(ValueError, match="No running session"):
            await orch._abort_task("QR-20")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_closes_session_even_when_interrupt_fails(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        mock_session = AsyncMock()
        mock_session.interrupt.side_effect = RuntimeError("interrupt failed")
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = mock_session
        orch._dispatcher = mock_dispatcher
        orch._event_bus = AsyncMock()

        await orch._abort_task("QR-20")

        mock_session.close.assert_awaited_once()
        orch._event_bus.publish.assert_awaited_once()


class TestListRunningTasks:
    """_list_running_tasks aggregates from dispatcher, pr_monitor, and needs_info_monitor."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_aggregates_all_sources(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = ["QR-1"]
        orch._dispatcher = mock_dispatcher

        mock_pr_monitor = MagicMock()
        mock_pr_monitor.get_tracked.return_value = {"QR-2": MagicMock(pr_url="https://github.com/pr/1")}
        orch._pr_monitor = mock_pr_monitor

        mock_ni_monitor = MagicMock()
        mock_ni_monitor.get_tracked.return_value = {"QR-3": MagicMock()}
        orch._needs_info_monitor = mock_ni_monitor

        tasks = orch._list_running_tasks()
        assert len(tasks) == 3
        assert tasks[0]["task_key"] == "QR-1"
        assert tasks[0]["status"] == "running"
        assert tasks[0]["tracker_status"] == ""
        assert tasks[1]["status"] == "in_review"
        assert tasks[2]["status"] == "needs_info"

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_handles_no_monitors(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()
        # dispatcher/monitors are None before run()
        assert orch._list_running_tasks() == []


class TestListAgentInfo:
    """_list_agent_info should only return agents registered in mailbox."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_excludes_unregistered_monitor_agents(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """PR/needs-info monitor agents are NOT registered in mailbox and must be excluded."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(tracker_token="t", tracker_org_id="o", repos_config=ReposConfig())
        orch = Orchestrator()

        # Dispatcher has one running agent (registered in mailbox)
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = ["QR-1"]
        orch._dispatcher = mock_dispatcher

        # PR monitor has one tracked PR (NOT registered in mailbox)
        mock_pr = MagicMock()
        mock_pr.get_tracked.return_value = {"QR-2": MagicMock(issue_summary="PR task")}
        orch._pr_monitor = mock_pr

        # Needs-info monitor has one tracked (NOT registered in mailbox)
        mock_ni = MagicMock()
        mock_ni.get_tracked.return_value = {"QR-3": MagicMock(issue_summary="NI task")}
        orch._needs_info_monitor = mock_ni

        # Only QR-1 is registered in the mailbox (as dispatcher does)
        orch._mailbox.register_agent("QR-1")

        agents = await orch._list_agent_info()

        # Only the registered agent should be returned
        keys = [a.task_key for a in agents]
        assert "QR-1" in keys
        assert "QR-2" not in keys
        assert "QR-3" not in keys


class TestInterruptAgentForComm:
    """_interrupt_agent_for_comm fallback chain."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_falls_back_to_on_demand_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When no live session exists, interrupts on-demand session."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        on_demand_session = AsyncMock()
        orch._on_demand_sessions["QR-1"] = on_demand_session

        await orch._interrupt_agent_for_comm("QR-1", "hello")

        on_demand_session.interrupt_with_message.assert_awaited_once_with(
            "hello",
        )

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_falls_back_to_send_message_to_agent(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When no session at all, falls back to send_message_to_agent."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        orch.send_message_to_agent = AsyncMock()

        await orch._interrupt_agent_for_comm("QR-1", "hello")

        orch.send_message_to_agent.assert_awaited_once_with(
            "QR-1",
            "hello",
        )

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_logs_warning_when_no_saved_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """ValueError from send_message_to_agent is caught and logged."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        orch.send_message_to_agent = AsyncMock(
            side_effect=ValueError("No session_id"),
        )

        # Should not raise — ValueError is caught
        await orch._interrupt_agent_for_comm("QR-1", "hello")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_catches_non_value_errors_from_fallback(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Non-ValueError from send_message_to_agent (e.g. network error)
        should also be caught — not just ValueError.
        """
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        orch.send_message_to_agent = AsyncMock(
            side_effect=requests.RequestException("connection timeout"),
        )

        # Should not raise — all exceptions should be caught
        await orch._interrupt_agent_for_comm("QR-1", "hello")


class TestSetMaxAgents:
    """Orchestrator.set_max_agents() resizes semaphore and get_state reflects it."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_increase_releases_semaphore_permits(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            max_concurrent_agents=2,
        )
        orch = Orchestrator()
        initial_value = orch._semaphore._value
        orch.set_max_agents(5)
        assert orch._semaphore._value == initial_value + 3
        assert orch._max_concurrent_agents == 5

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_decrease_reduces_semaphore_value(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            max_concurrent_agents=5,
        )
        orch = Orchestrator()
        orch.set_max_agents(2)
        assert orch._semaphore._value == 2
        assert orch._max_concurrent_agents == 2

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_get_state_reflects_new_max(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            max_concurrent_agents=2,
        )
        orch = Orchestrator()
        state = orch.get_state()
        assert state["config"]["max_agents"] == 2
        orch.set_max_agents(10)
        state = orch.get_state()
        assert state["config"]["max_agents"] == 10

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_set_max_agents_minimum_one(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            max_concurrent_agents=3,
        )
        orch = Orchestrator()
        orch.set_max_agents(1)
        assert orch._max_concurrent_agents == 1
        assert orch._semaphore._value >= 0

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    def test_set_max_agents_zero_raises(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        import pytest

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            max_concurrent_agents=2,
        )
        orch = Orchestrator()
        with pytest.raises(ValueError, match="at least 1"):
            orch.set_max_agents(0)
        assert orch._max_concurrent_agents == 2


class TestOnDemandSession:
    """send_message_to_agent on-demand session creation."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_creates_on_demand_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Creates on-demand session when no active session exists."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()

        # Storage returns a session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Agent creates a resumed session
        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = []

        await orch.send_message_to_agent("QR-1", "hi there")

        await drain_background_tasks(orch)

        mock_session.send.assert_awaited_once_with("hi there")
        assert "QR-1" in orch._on_demand_sessions
        assert orch._on_demand_sessions["QR-1"] is mock_session

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_reuses_existing_on_demand_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Reuses an existing on-demand session on second call."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._event_bus = AsyncMock()

        mock_session = _make_mock_session()
        orch._on_demand_sessions["QR-1"] = mock_session

        await orch.send_message_to_agent("QR-1", "second msg")

        await drain_background_tasks(orch)

        mock_session.send.assert_awaited_once_with("second msg")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_falls_back_to_fresh_session_on_resume_failure(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Falls back to fresh session when resume fails."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = "sess-old"
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = None

        # First create_session (resume) fails, second (fresh) succeeds
        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.side_effect = [
            RuntimeError("resume failed"),
            mock_session,
        ]

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="Test task",
            components=[],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = []

        await orch.send_message_to_agent("QR-1", "hello")

        await drain_background_tasks(orch)

        assert orch._agent.create_session.call_count == 2
        # Fresh fallback session should prepend context
        mock_session.send.assert_awaited_once()
        sent_msg = mock_session.send.call_args[0][0]
        assert "hello" in sent_msg
        assert "Task Context (Fallback Session)" in sent_msg
        assert "QR-1" in sent_msg
        assert orch._on_demand_sessions["QR-1"] is mock_session

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_fallback_context_includes_tracker_comments(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Fallback context includes Tracker comments."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = "sess-old"
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = None

        # Resume fails, fresh session created
        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.side_effect = [
            RuntimeError("resume failed"),
            mock_session,
        ]

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-2",
            summary="Test",
            description="Task description",
            components=[],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = [
            {
                "createdBy": {"display": "Alice"},
                "text": "Check the API docs",
                "createdAt": "2025-06-01",
            }
        ]

        await orch.send_message_to_agent("QR-2", "continue work")

        await drain_background_tasks(orch)

        mock_session.send.assert_awaited_once()
        sent_msg = mock_session.send.call_args[0][0]
        assert "continue work" in sent_msg
        assert "Check the API docs" in sent_msg
        assert "Alice" in sent_msg

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_fallback_context_includes_mailbox_messages(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Fallback context includes inter-agent message history."""
        from orchestrator.agent_mailbox import AgentMessage, MessageType
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = "sess-old"
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = None

        # Resume fails, fresh session created
        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.side_effect = [
            RuntimeError("resume failed"),
            mock_session,
        ]

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-3",
            summary="Test",
            description="Task description",
            components=[],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = []

        # Add a message to mailbox
        import uuid

        msg = AgentMessage(
            id=str(uuid.uuid4()),
            sender_task_key="QR-10",
            sender_summary="Peer task summary",
            target_task_key="QR-3",
            text="What endpoint should I call?",
            msg_type=MessageType.REQUEST,
            reply_text=None,
        )
        # Mock mailbox to return the message
        orch._mailbox = MagicMock()
        orch._mailbox.get_all_messages.return_value = [msg]
        orch._mailbox.register_agent = MagicMock()

        await orch.send_message_to_agent("QR-3", "continue work")

        await drain_background_tasks(orch)

        mock_session.send.assert_awaited_once()
        sent_msg = mock_session.send.call_args[0][0]
        assert "continue work" in sent_msg
        assert "What endpoint should I call?" in sent_msg
        assert "QR-10" in sent_msg

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_fallback_context_survives_comment_fetch_failure(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Session created even if get_comments fails."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = "sess-old"
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = None

        # Resume fails, fresh session created
        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.side_effect = [
            RuntimeError("resume failed"),
            mock_session,
        ]

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-4",
            summary="Test",
            description="Task description",
            components=[],
            tags=[],
            status="open",
        )
        # get_comments fails
        orch._tracker.get_comments.side_effect = RuntimeError("Tracker API error")

        await orch.send_message_to_agent("QR-4", "continue work")

        await drain_background_tasks(orch)

        # Session still created and message sent with partial context
        mock_session.send.assert_awaited_once()
        sent_msg = mock_session.send.call_args[0][0]
        assert "continue work" in sent_msg
        assert "Task Context (Fallback Session)" in sent_msg
        assert orch._on_demand_sessions["QR-4"] is mock_session

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_resumed_session_no_context_prepended(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Successfully resumed session sends raw message without context."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()

        # Storage returns a session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Agent successfully resumes session
        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-5",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = []

        await orch.send_message_to_agent("QR-5", "test message")

        await drain_background_tasks(orch)

        # Message should be sent without context prepending
        mock_session.send.assert_awaited_once_with("test message")
        # Context should NOT be in the message
        sent_msg = mock_session.send.call_args[0][0]
        assert "Task Context (Fallback Session)" not in sent_msg

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_get_state_includes_on_demand(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """get_state() includes on_demand_sessions list."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._on_demand_sessions["QR-5"] = MagicMock()
        orch._on_demand_sessions["QR-7"] = MagicMock()

        state = orch.get_state()
        assert set(state["on_demand_sessions"]) == {"QR-5", "QR-7"}

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_close_on_demand_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """_close_on_demand_session closes session and removes from dict."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_session = AsyncMock()
        orch._on_demand_sessions["QR-1"] = mock_session

        await orch._close_on_demand_session("QR-1")

        mock_session.close.assert_awaited_once()
        assert "QR-1" not in orch._on_demand_sessions

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_close_on_demand_survives_session_error(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """_close_on_demand_session handles session.close() errors."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_session = AsyncMock()
        mock_session.close.side_effect = RuntimeError("boom")
        orch._on_demand_sessions["QR-1"] = mock_session

        # Should not raise
        await orch._close_on_demand_session("QR-1")

        assert "QR-1" not in orch._on_demand_sessions

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_concurrent_on_demand_does_not_leak(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Two concurrent send_message_to_agent must not leak sessions."""
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-1"

        sessions_created: list[AsyncMock] = []

        def _make_session(*args, **kwargs):
            s = _make_mock_session()
            sessions_created.append(s)
            return s

        orch._agent = AsyncMock()
        orch._agent.create_session.side_effect = _make_session

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="T",
            description="",
            components=[],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = []

        await asyncio.gather(
            orch.send_message_to_agent("QR-1", "msg1"),
            orch.send_message_to_agent("QR-1", "msg2"),
        )

        await drain_background_tasks(orch)

        # Only ONE session should have been created
        assert orch._agent.create_session.call_count == 1
        assert len(sessions_created) == 1

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_on_demand_session_registers_in_mailbox(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """On-demand session is registered in mailbox when created."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()

        # Storage returns a session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Agent creates a resumed session
        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=["Бекенд"],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = []

        await orch.send_message_to_agent("QR-1", "hi there")
        await drain_background_tasks(orch)

        # Verify session was created and registered in mailbox
        assert "QR-1" in orch._on_demand_sessions
        assert orch._mailbox.is_registered("QR-1")

        # Verify component metadata was stored
        metadata = orch._mailbox.get_agent_metadata("QR-1")
        assert metadata["component"] == "Бекенд"

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_on_demand_session_unregisters_on_close(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """On-demand session is unregistered from mailbox when closed."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_session = AsyncMock()
        orch._on_demand_sessions["QR-1"] = mock_session

        # Manually register in mailbox (simulating what _create_on_demand_session will do)
        orch._mailbox.register_agent("QR-1", component="Бекенд")
        assert orch._mailbox.is_registered("QR-1")

        await orch._close_on_demand_session("QR-1")

        mock_session.close.assert_awaited_once()
        assert "QR-1" not in orch._on_demand_sessions
        assert not orch._mailbox.is_registered("QR-1")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_list_agent_info_includes_on_demand(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """_list_agent_info includes on-demand sessions registered in mailbox."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        # Set up empty monitors
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = []
        orch._dispatcher = mock_dispatcher

        mock_pr = MagicMock()
        mock_pr.get_tracked.return_value = {}
        orch._pr_monitor = mock_pr

        mock_ni = MagicMock()
        mock_ni.get_tracked.return_value = {}
        orch._needs_info_monitor = mock_ni

        # Create on-demand session and register in mailbox
        mock_session = AsyncMock()
        orch._on_demand_sessions["QR-5"] = mock_session
        orch._mailbox.register_agent("QR-5", component="Бекенд")

        # Mock _get_task_summary
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = MagicMock(summary="On-demand task")

        agents = await orch._list_agent_info()

        # Verify on-demand session appears in the list
        keys = [a.task_key for a in agents]
        assert "QR-5" in keys

        on_demand_agent = next(a for a in agents if a.task_key == "QR-5")
        assert on_demand_agent.status == "on_demand"
        assert on_demand_agent.component == "Бекенд"

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_on_demand_lifecycle_register_then_unregister(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Full lifecycle: on-demand session registers on creation, unregisters on close."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()

        # Storage returns a session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-456"

        # Agent creates a resumed session
        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-7",
            summary="Lifecycle test",
            description="",
            components=["Frontend"],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = []

        # Phase 1: Create on-demand session
        await orch.send_message_to_agent("QR-7", "test message")
        await drain_background_tasks(orch)

        # Verify registration
        assert "QR-7" in orch._on_demand_sessions
        assert orch._mailbox.is_registered("QR-7")

        # Phase 2: Close on-demand session
        await orch._close_on_demand_session("QR-7")

        # Verify unregistration
        assert "QR-7" not in orch._on_demand_sessions
        assert not orch._mailbox.is_registered("QR-7")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_close_nonexistent_on_demand_does_not_unregister_other_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Closing non-existent on-demand session does not unregister other live sessions."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        # Simulate a running session (not on-demand) registered in mailbox
        orch._mailbox.register_agent("QR-1", component="Бекенд")
        assert orch._mailbox.is_registered("QR-1")

        # QR-1 is NOT in _on_demand_sessions
        assert "QR-1" not in orch._on_demand_sessions

        # Attempt to close a non-existent on-demand session
        await orch._close_on_demand_session("QR-1")

        # The running session should still be registered (bug: it gets unregistered)
        assert orch._mailbox.is_registered("QR-1"), (
            "Running session was incorrectly unregistered when closing non-existent on-demand session"
        )


class TestOnDemandSessionCommTools:
    """On-demand sessions must include comm tools (mailbox)."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_passes_mailbox_to_create_session(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """On-demand session must receive mailbox for comm tools."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._event_bus = AsyncMock()

        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-1"

        mock_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = mock_session

        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = []

        # Set a real mailbox on the orchestrator
        mock_mailbox = MagicMock()
        orch._mailbox = mock_mailbox

        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # create_session must have been called with mailbox kwarg
        call_kwargs = orch._agent.create_session.call_args
        assert call_kwargs.kwargs.get("mailbox") is mock_mailbox, "On-demand sessions must pass mailbox for comm tools"


class TestNonBlockingSend:
    """send_message_to_agent should return immediately (fire-and-forget)."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_send_to_pr_session_returns_before_send_completes(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """send_message_to_agent returns while session.send() is still pending."""
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        # Block session.send() until we release the event
        send_started = asyncio.Event()
        send_release = asyncio.Event()

        async def _slow_send(msg: str):
            send_started.set()
            await send_release.wait()
            return MagicMock(
                success=True,
                output="ok",
                proposals=[],
            )

        mock_session = _make_mock_session()
        mock_session.send = AsyncMock(side_effect=_slow_send)

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher

        mock_pr_monitor = MagicMock()
        mock_pr_monitor.get_tracked.return_value = {
            "QR-1": MagicMock(session=mock_session),
        }
        orch._pr_monitor = mock_pr_monitor
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._event_bus = AsyncMock()

        # send_message_to_agent must return immediately
        await orch.send_message_to_agent("QR-1", "hello")

        # The background task should have started send
        await asyncio.sleep(0)
        assert send_started.is_set(), "session.send() should have been called"

        # But it hasn't completed yet — still blocked on send_release
        assert not send_release.is_set()

        # Release and let background task finish
        send_release.set()
        await asyncio.sleep(0)

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_send_processes_proposals_in_background(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Proposals from session.send() are processed in background task."""
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_session = _make_mock_session()
        mock_result = MagicMock(
            success=True,
            output="done",
            proposals=[{"title": "p1"}],
        )
        mock_session.send.return_value = mock_result

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._event_bus = AsyncMock()

        mock_session2 = _make_mock_session()
        mock_session2.send.return_value = mock_result
        orch._on_demand_sessions["QR-1"] = mock_session2

        orch._proposal_manager = AsyncMock()

        await orch.send_message_to_agent("QR-1", "msg")

        # Let background task run
        await asyncio.sleep(0)
        await drain_background_tasks(orch)

        orch._proposal_manager.process_proposals.assert_awaited_once_with(
            "QR-1",
            [{"title": "p1"}],
        )

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_send_error_does_not_propagate(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Errors from background session.send() are logged, not raised."""
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_session = _make_mock_session()
        mock_session.send.side_effect = RuntimeError("boom")

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {
            "QR-1": MagicMock(session=mock_session),
        }
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._event_bus = AsyncMock()

        # Should not raise
        await orch.send_message_to_agent("QR-1", "hello")

        # Let background task run and fail gracefully
        await asyncio.sleep(0)
        await drain_background_tasks(orch)

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_validation_errors_still_propagate(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """ValueError from session creation still propagates synchronously."""
        import pytest

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = None

        with pytest.raises(ValueError, match="No session_id"):
            await orch.send_message_to_agent("QR-1", "hello")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_concurrent_sends_serialized_by_lock(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Multiple sends to the same task_key are serialized, not dropped."""
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        sent_messages: list[str] = []
        send_release = asyncio.Event()

        async def _slow_send(msg: str):
            await send_release.wait()
            sent_messages.append(msg)
            return MagicMock(
                success=True,
                output="ok",
                proposals=[],
            )

        mock_session = _make_mock_session()
        mock_session.send = AsyncMock(side_effect=_slow_send)

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {
            "QR-1": MagicMock(session=mock_session),
        }
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._event_bus = AsyncMock()

        # Both sends schedule background tasks
        await orch.send_message_to_agent("QR-1", "first")
        await orch.send_message_to_agent("QR-1", "second")

        # Release and drain — lock serializes them
        send_release.set()
        await drain_background_tasks(orch)

        # Both messages delivered
        assert mock_session.send.await_count == 2
        assert sent_messages == ["first", "second"]


class TestCleanupStaleWorktrees:
    """Tests for _cleanup_stale_worktrees() startup cleanup."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_cleans_terminal_status_worktrees(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config, tmp_path
    ) -> None:
        """Worktrees for issues in terminal statuses are cleaned up."""
        from orchestrator.config import Config, RepoInfo, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)
        (base / "QR-2").mkdir(parents=True)
        (base / "QR-3").mkdir(parents=True)

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(
                all_repos=[RepoInfo(url="u", path="/ws/repo")],
            ),
            worktree_base_dir=str(base),
        )

        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker

        # QR-1: resolved (terminal), QR-2: open (active), QR-3: closed (terminal)
        mock_tracker.search.return_value = [
            TrackerIssue(key="QR-1", summary="s1", description="", components=[], tags=[], status="resolved"),
            TrackerIssue(key="QR-2", summary="s2", description="", components=[], tags=[], status="open"),
            TrackerIssue(key="QR-3", summary="s3", description="", components=[], tags=[], status="closed"),
        ]

        orch = Orchestrator()
        mock_workspace = MagicMock()
        mock_workspace.cleanup_stale.return_value = 2
        orch._workspace = mock_workspace

        await orch._cleanup_stale_worktrees()

        # Should call cleanup_stale with terminal keys only
        mock_workspace.cleanup_stale.assert_called_once()
        stale_keys = mock_workspace.cleanup_stale.call_args[0][0]
        assert "QR-1" in stale_keys
        assert "QR-3" in stale_keys
        assert "QR-2" not in stale_keys

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_cleans_missing_issues(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config, tmp_path
    ) -> None:
        """Worktrees for issues not found in Tracker are cleaned up."""
        from orchestrator.config import Config, RepoInfo, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)
        (base / "QR-2").mkdir(parents=True)

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(
                all_repos=[RepoInfo(url="u", path="/ws/repo")],
            ),
            worktree_base_dir=str(base),
        )

        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker

        # Only QR-1 found (active); QR-2 was deleted from Tracker
        mock_tracker.search.return_value = [
            TrackerIssue(key="QR-1", summary="s1", description="", components=[], tags=[], status="open"),
        ]

        orch = Orchestrator()
        mock_workspace = MagicMock()
        mock_workspace.cleanup_stale.return_value = 1
        orch._workspace = mock_workspace

        await orch._cleanup_stale_worktrees()

        stale_keys = mock_workspace.cleanup_stale.call_args[0][0]
        assert "QR-2" in stale_keys
        assert "QR-1" not in stale_keys

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_skips_non_issue_directories(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config, tmp_path
    ) -> None:
        """Non QR-* directories are not considered for cleanup."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)
        (base / ".git").mkdir(parents=True)
        (base / "random-dir").mkdir(parents=True)

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            worktree_base_dir=str(base),
        )

        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker
        mock_tracker.search.return_value = []

        orch = Orchestrator()
        mock_workspace = MagicMock()
        mock_workspace.cleanup_stale.return_value = 1
        orch._workspace = mock_workspace

        await orch._cleanup_stale_worktrees()

        # Only QR-1 is in the stale set (missing from Tracker)
        stale_keys = mock_workspace.cleanup_stale.call_args[0][0]
        assert "QR-1" in stale_keys
        assert ".git" not in stale_keys
        assert "random-dir" not in stale_keys

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_tracker_error_does_not_break_startup(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config, tmp_path
    ) -> None:
        """Tracker API errors are caught — cleanup is best-effort."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            worktree_base_dir=str(base),
        )

        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker
        mock_tracker.search.side_effect = requests.RequestException("timeout")

        orch = Orchestrator()
        mock_workspace = MagicMock()
        orch._workspace = mock_workspace

        # Should not raise
        await orch._cleanup_stale_worktrees()

        # cleanup_stale should not have been called
        mock_workspace.cleanup_stale.assert_not_called()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_no_worktree_dir(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config, tmp_path
    ) -> None:
        """If worktree base dir doesn't exist, cleanup is a no-op."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            worktree_base_dir=str(tmp_path / "nonexistent"),
        )

        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        mock_workspace = MagicMock()
        orch._workspace = mock_workspace

        # Should not raise
        await orch._cleanup_stale_worktrees()

        mock_tracker.search.assert_not_called()
        mock_workspace.cleanup_stale.assert_not_called()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_excludes_dispatched_issues(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config, tmp_path
    ) -> None:
        """Currently dispatched issues are never cleaned up."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)
        (base / "QR-2").mkdir(parents=True)

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
            worktree_base_dir=str(base),
        )

        mock_tracker = MagicMock()
        mock_tracker_cls.return_value = mock_tracker
        # Both resolved
        mock_tracker.search.return_value = [
            TrackerIssue(key="QR-1", summary="", description="", components=[], tags=[], status="resolved"),
            TrackerIssue(key="QR-2", summary="", description="", components=[], tags=[], status="resolved"),
        ]

        orch = Orchestrator()
        orch._dispatched.add("QR-1")  # QR-1 is currently running

        mock_workspace = MagicMock()
        mock_workspace.cleanup_stale.return_value = 1
        orch._workspace = mock_workspace

        await orch._cleanup_stale_worktrees()

        stale_keys = mock_workspace.cleanup_stale.call_args[0][0]
        assert "QR-1" not in stale_keys  # protected by dispatched set
        assert "QR-2" in stale_keys


class TestOnDemandSendContention:
    """Bug: send_message_to_agent blocks on lock when on-demand session already exists."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_second_send_skips_lock_when_session_exists(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """send_message_to_agent should not contend on the session lock
        when the on-demand session already exists.

        Scenario: first send is holding the lock inside _fire_and_forget_send
        (long-running session.send). A second send arrives for the same
        task_key — it should find the on-demand session pre-lock and skip
        the lock entirely, returning immediately.
        """
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        # Block session.send() indefinitely (simulating long send)
        send_release = asyncio.Event()

        async def _slow_send(msg: str):
            await send_release.wait()
            return MagicMock(success=True, output="ok", proposals=[])

        mock_session = _make_mock_session()
        mock_session.send = AsyncMock(side_effect=_slow_send)

        # Pre-populate on-demand session (as if first call created it)
        orch._on_demand_sessions["QR-1"] = mock_session

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._event_bus = AsyncMock()

        # Acquire the lock manually (simulating _fire_and_forget_send holding it)
        lock = orch._session_locks.setdefault("QR-1", asyncio.Lock())
        await lock.acquire()

        # send_message_to_agent must return immediately even though
        # the lock is held — it should find the existing on-demand
        # session before trying to acquire the lock.
        try:
            done = asyncio.Event()

            async def _send_and_signal():
                await orch.send_message_to_agent("QR-1", "hello")
                done.set()

            task = asyncio.create_task(_send_and_signal())
            # Give event loop a chance to run the task
            await asyncio.sleep(0.05)
            assert done.is_set(), (
                "send_message_to_agent should return immediately when on-demand session exists, not block on lock"
            )
            send_release.set()
            await task
        finally:
            lock.release()
            send_release.set()


class TestShutdownTaskDrainOrder:
    """Bug: shutdown closes sessions before draining fire-and-forget tasks."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_tasks_drain_before_sessions_close(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Fire-and-forget tasks in self._tasks should complete before
        sessions are closed during shutdown. Otherwise in-flight
        session.send() calls fail because the session is already closed.
        """
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        # Track the order of operations
        call_order: list[str] = []

        mock_session = _make_mock_session()

        async def _slow_send(msg: str):
            call_order.append("send_start")
            await asyncio.sleep(0.01)
            call_order.append("send_end")
            return MagicMock(success=True, output="ok", proposals=[])

        mock_session.send = AsyncMock(side_effect=_slow_send)

        # Set up on-demand session
        orch._on_demand_sessions["QR-1"] = mock_session

        original_close = orch._close_on_demand_session

        async def _tracked_close(key: str) -> None:
            call_order.append("session_close")
            await original_close(key)

        orch._close_on_demand_session = _tracked_close

        # Set up infrastructure
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {
            "QR-1": MagicMock(session=mock_session),
        }
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._event_bus = AsyncMock()
        orch._proposal_manager = AsyncMock()

        # Schedule a fire-and-forget send
        await orch.send_message_to_agent("QR-1", "hello")

        # Let the background task start
        await asyncio.sleep(0)

        # Now simulate shutdown: drain tasks, then close sessions
        if orch._tasks:
            await asyncio.gather(*orch._tasks, return_exceptions=True)

        await orch._close_on_demand_session("QR-1")

        # send_end must come before session_close
        assert "send_end" in call_order, "send should have completed"
        assert "session_close" in call_order, "session should have been closed"
        send_end_idx = call_order.index("send_end")
        close_idx = call_order.index("session_close")
        assert send_end_idx < close_idx, f"Tasks should drain before sessions close, but got order: {call_order}"


class TestDeadSessionRecovery:
    """Tests for dead session detection and automatic recreation."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dead_session_detected_on_send_exception(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When session.send() raises, dead session is recreated."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage with session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a dead session that raises on send
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Connection lost")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new healthy session for recreation
        new_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send a message to the dead session
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Dead session should have been replaced
        assert orch._on_demand_sessions["QR-1"] is new_session
        assert orch._on_demand_sessions["QR-1"] is not dead_session

        # New session should have received the message
        new_session.send.assert_awaited_once_with("hello")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dead_session_detected_on_result_failure(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When session.send() returns success=False, dead session is recreated."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage with session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a dead session that returns failure
        dead_session = _make_mock_session(success=False, output="Session failed")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new healthy session for recreation
        new_session = _make_mock_session(success=True, output="OK")
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send a message to the dead session
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Dead session should have been replaced
        assert orch._on_demand_sessions["QR-1"] is new_session
        assert orch._on_demand_sessions["QR-1"] is not dead_session

        # New session should have received the message
        new_session.send.assert_awaited_once_with("hello")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dead_session_context_preserved(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When dead session is recreated, context is preserved via session_id resume."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage with session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-old-123"

        # Create a dead session that raises
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new session
        new_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send a message to trigger recreation
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Verify that create_session was called with the resume_session_id
        assert orch._agent.create_session.call_count == 1
        call_kwargs = orch._agent.create_session.call_args.kwargs
        assert call_kwargs["resume_session_id"] == "sess-old-123"

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dead_session_recovery_logged(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Dead session recreation is logged at INFO level."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mocks
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create dead and new sessions
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        new_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Patch logger to capture log calls
        with patch("orchestrator.main.logger") as mock_logger:
            await orch.send_message_to_agent("QR-1", "hello")
            await drain_background_tasks(orch)

            # Check that recreation was logged
            info_calls = list(mock_logger.info.call_args_list)
            recreation_logged = any(
                "Dead on-demand session detected" in str(call) or "recreating" in str(call).lower()
                for call in info_calls
            )
            assert recreation_logged, f"Expected recreation log, got: {info_calls}"

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dead_session_recovery_on_interrupt_failure(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When interrupt_with_message() fails, dead session is cleaned up."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()
        orch._dispatcher = MagicMock()
        orch._dispatcher.get_running_session.return_value = None
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor.get_persisted_session_id.return_value = None

        # Setup tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup storage
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a dead on-demand session that fails on interrupt
        dead_session = _make_mock_session()
        dead_session.interrupt_with_message = AsyncMock(side_effect=Exception("Dead"))
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new session for recreation
        new_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Try to interrupt the dead session
        await orch._interrupt_agent_for_comm("QR-1", "message from peer")
        await drain_background_tasks(orch)

        # Dead session should have been cleaned up and replaced
        assert "QR-1" not in orch._on_demand_sessions or orch._on_demand_sessions["QR-1"] is new_session

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dead_session_recreation_failure_does_not_loop(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """If recreation fails, error is logged and not retried infinitely."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # No session_id available for recreation
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = None
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_persisted_session_id.return_value = None
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_persisted_session_id.return_value = None

        # Create a dead session
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Mock tracker (will be called by _create_on_demand_session if we get that far)
        orch._tracker = MagicMock()

        # Attempt to send message - should fail but not loop
        with patch("orchestrator.main.logger") as mock_logger:
            await orch.send_message_to_agent("QR-1", "hello")
            await drain_background_tasks(orch)

            # Should have logged a warning about recreation failure
            warning_calls = list(mock_logger.warning.call_args_list)
            assert len(warning_calls) > 0, "Expected warning about recreation failure"

        # Dead session should be removed, but no new session created
        assert "QR-1" not in orch._on_demand_sessions

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dead_session_mailbox_lifecycle(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Dead session mailbox is unregistered, new session is registered."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=["Backend"],
            tags=[],
            status="open",
        )

        # Setup storage
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create dead session
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Manually register in mailbox (simulating initial creation)
        orch._mailbox.register_agent("QR-1", component="Backend")

        # Create new session
        new_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Track mailbox calls
        with (
            patch.object(orch._mailbox, "unregister_agent", wraps=orch._mailbox.unregister_agent) as mock_unreg,
            patch.object(orch._mailbox, "register_agent", wraps=orch._mailbox.register_agent) as mock_reg,
        ):
            # Send message to trigger recreation
            await orch.send_message_to_agent("QR-1", "hello")
            await drain_background_tasks(orch)

            # Should have unregistered old session and registered new one
            mock_unreg.assert_called_once_with("QR-1")
            # register_agent should be called for the new session
            assert any(call[0][0] == "QR-1" for call in mock_reg.call_args_list)

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_exception_path_processes_proposals(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """After exception-path recreation, proposals from retry are processed."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage with session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Setup proposal manager
        orch._proposal_manager = AsyncMock()

        # Create a dead session that raises on send
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new session that returns proposals
        new_session = _make_mock_session(
            success=True,
            output="Done",
        )
        # Add proposals to the result
        new_session.send.return_value.proposals = [{"summary": "Test proposal", "description": "Test"}]
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send a message
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Proposals should have been processed
        orch._proposal_manager.process_proposals.assert_awaited_once_with(
            "QR-1",
            [{"summary": "Test proposal", "description": "Test"}],
        )

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_session_lock_preserved_during_recreation(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Session lock is not removed during recreation to prevent race conditions."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mocks
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create dead session
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Track lock identity during recreation
        lock_ids_during_recreation = []

        async def track_lock_during_create(*args, **kwargs):
            # Capture lock ID during async creation
            lock_ids_during_recreation.append(id(orch._session_locks.get("QR-1")))
            return _make_mock_session()

        orch._agent = AsyncMock()
        orch._agent.create_session.side_effect = track_lock_during_create

        # Send message to trigger recreation
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Lock should still exist and be the same object throughout
        assert "QR-1" in orch._session_locks
        # All lock IDs captured should be the same (not None, not changing)
        assert all(lid is not None for lid in lock_ids_during_recreation)
        assert len(set(lock_ids_during_recreation)) == 1  # Same lock object

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_event_publish_failure_does_not_mask_recreation(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """If event publish fails, session recreation still succeeds."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()

        # Make event bus raise only on SESSION_RECREATED event
        async def selective_publish(event):
            from orchestrator.constants import EventType

            if event.type == EventType.SESSION_RECREATED:
                raise Exception("Event bus down")

        orch._event_bus = AsyncMock()
        orch._event_bus.publish.side_effect = selective_publish

        # Setup mocks
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create dead session
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create new session
        new_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send message - should succeed despite event publish failure
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Session should have been recreated successfully
        assert "QR-1" in orch._on_demand_sessions
        assert orch._on_demand_sessions["QR-1"] is new_session
        # Message should have been sent to new session
        new_session.send.assert_awaited_once_with("hello")

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_exception_path_holds_lock_during_recreation(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """Exception path must hold lock during recreation to prevent race conditions."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mocks
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Track if lock is held during recreation
        lock_held_during_recreation = []

        async def track_lock_during_recreate(*args, **kwargs):
            # Check if lock is held when create_session is called
            lock = orch._session_locks.get("QR-1")
            if lock is not None:
                # If lock.locked() returns True, lock is held
                lock_held_during_recreation.append(lock.locked())
            else:
                lock_held_during_recreation.append(False)
            return _make_mock_session()

        # Create dead session that throws
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        orch._agent = AsyncMock()
        orch._agent.create_session.side_effect = track_lock_during_recreate

        # Send message - will trigger exception path
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Lock should have been held during recreation
        assert len(lock_held_during_recreation) > 0, "Recreation should have happened"
        assert all(held for held in lock_held_during_recreation), (
            f"Lock was not held during recreation: {lock_held_during_recreation}"
        )

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_proposal_processing_error_does_not_cause_duplicate_send(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When session.send() succeeds but process_proposals() raises, message should not be re-sent."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a healthy session that successfully sends
        healthy_session = _make_mock_session()
        healthy_session.send.return_value = MagicMock(
            success=True,
            output="response",
            proposals=[{"id": "p1", "data": "test"}],
        )
        orch._on_demand_sessions["QR-1"] = healthy_session

        # Mock proposal manager to raise during process_proposals
        orch._proposal_manager = AsyncMock()
        orch._proposal_manager.process_proposals.side_effect = Exception("Event bus failure")

        # Mock agent for potential recreation (should NOT be called)
        new_session = _make_mock_session()
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send a message - this should succeed, then fail during proposal processing
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # CRITICAL: healthy_session.send should be called ONLY ONCE
        # The bug would cause it to be called twice (recreate + retry)
        assert healthy_session.send.call_count == 1, (
            f"Expected send() to be called once, but was called {healthy_session.send.call_count} times. "
            f"This indicates the message was re-sent after process_proposals() failed."
        )

        # Session should NOT have been recreated (it was healthy)
        assert orch._on_demand_sessions["QR-1"] is healthy_session
        assert orch._on_demand_sessions["QR-1"] is not new_session
        orch._agent.create_session.assert_not_called()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_concurrent_sends_to_dead_session_no_double_recreation(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When multiple sends race to recreate a dead session, only one recreation happens."""
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a dead session that raises on send
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Connection lost")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new healthy session for recreation
        new_session = _make_mock_session()
        new_session.send.return_value = MagicMock(success=True, output="ok", proposals=None)
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send two messages concurrently to trigger race
        await asyncio.gather(
            orch.send_message_to_agent("QR-1", "message1"),
            orch.send_message_to_agent("QR-1", "message2"),
        )
        await drain_background_tasks(orch)

        # CRITICAL: create_session should be called exactly ONCE, not twice
        # The re-check inside the lock should prevent the second sender from recreating
        assert orch._agent.create_session.call_count == 1, (
            f"Expected create_session to be called once, but was called {orch._agent.create_session.call_count} times. "
            f"This indicates both concurrent senders recreated the session (race condition)."
        )

        # The session should be the new_session, not dead_session
        assert orch._on_demand_sessions["QR-1"] is new_session
        assert orch._on_demand_sessions["QR-1"] is not dead_session

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_concurrent_sends_both_messages_delivered_after_recreation(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When concurrent sends race, second sender uses recreated session and delivers its message."""
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a dead session that raises on send
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Connection lost")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new healthy session for recreation
        new_session = _make_mock_session()
        new_session.send.return_value = MagicMock(success=True, output="ok", proposals=None)
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send two messages concurrently to trigger race
        await asyncio.gather(
            orch.send_message_to_agent("QR-1", "message1"),
            orch.send_message_to_agent("QR-1", "message2"),
        )
        await drain_background_tasks(orch)

        # CRITICAL: Both messages must be sent to the new session
        # First sender: recreates session and sends its message
        # Second sender: detects recreation, uses new session, sends its message
        assert new_session.send.call_count == 2, (
            f"Expected both messages to be sent (call_count=2), but got {new_session.send.call_count}. "
            f"This indicates the second sender's message was silently dropped."
        )

        # Verify both messages were sent
        call_args = [call[0][0] for call in new_session.send.call_args_list]
        assert "message1" in call_args, "First message was not sent"
        assert "message2" in call_args, "Second message was not sent"

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_retry_exception_does_not_cause_duplicate_send(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When retry after success=False throws, should not misinterpret as concurrent recreation."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a dead session that returns success=False
        dead_session = _make_mock_session()
        dead_session.send.return_value = MagicMock(success=False, output="error", proposals=None)
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new session for recreation that throws on send
        new_session = _make_mock_session()
        new_session.send.side_effect = Exception("Network error on retry")
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Send a message - should trigger success=False path, then recreate, then retry throws
        await orch.send_message_to_agent("QR-1", "test message")
        await drain_background_tasks(orch)

        # CRITICAL: new_session.send should be called ONCE (the retry that failed)
        # NOT twice (which would indicate it was misinterpreted as concurrent recreation
        # and sent again to the same failed session)
        assert new_session.send.call_count == 1, (
            f"Expected retry to be attempted once, but got {new_session.send.call_count}. "
            f"call_count > 1 indicates the exception from retry was misinterpreted as concurrent "
            f"recreation, causing duplicate send to the same failed session."
        )

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_recreation_failure_after_success_false_does_not_process_result(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When success=False triggers recreation but recreation fails (returns None), should not process result."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a dead session that returns success=False
        dead_session = _make_mock_session()
        dead_session.send.return_value = MagicMock(
            success=False,
            output="error",
            proposals=[{"id": "bad-proposal"}],  # Should NOT be processed
        )
        orch._on_demand_sessions["QR-1"] = dead_session

        # Mock agent to return None (recreation fails)
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = None

        # Mock proposal manager to track if process_proposals is called
        orch._proposal_manager = AsyncMock()

        # Send a message - should trigger success=False path, recreate fails, should NOT process proposals
        await orch.send_message_to_agent("QR-1", "test message")
        await drain_background_tasks(orch)

        # CRITICAL: proposals from the failed result should NOT be processed
        # If they are, it means the code fell through without returning when recreation failed
        (
            orch._proposal_manager.process_proposals.assert_not_called(),
            (
                "process_proposals was called even though session recreation failed. "
                "This indicates the code fell through and processed the dead session's failed result."
            ),
        )

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_dead_session_fresh_fallback_includes_context_prompt(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When dead session is recreated and resume fails (fresh session), context prompt must be included in retry message."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test Task",
            description="Test description",
            components=["backend"],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = [{"text": "Test comment", "createdAt": "2024-01-01T00:00:00Z"}]

        # Setup mock storage with session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-old-123"

        # Create a dead session that raises
        dead_session = _make_mock_session()
        dead_session.send.side_effect = Exception("Dead")
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new fresh session (resume will fail, so fresh session created)
        new_session = _make_mock_session()
        orch._agent = AsyncMock()

        # Simulate resume failure: first call with resume_session_id fails,
        # second call without it succeeds (fresh session)
        orch._agent.create_session.side_effect = [
            Exception("Resume failed"),  # First attempt with resume_session_id
            new_session,  # Second attempt without resume_session_id (fresh)
        ]

        # Send a message to trigger recreation
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Verify new session was created and message was sent
        assert new_session.send.call_count == 1

        # CRITICAL: The message sent to the fresh session MUST include context prompt
        # because the fresh session has no conversation history
        sent_message = new_session.send.call_args[0][0]
        assert "Test Task" in sent_message, "Fresh session should receive context prompt with task summary"
        assert "Test description" in sent_message, "Fresh session should receive context prompt with task description"
        assert "hello" in sent_message, "Fresh session should still receive the original message"

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_fresh_session_dies_immediately_no_double_context(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When send_message creates fresh session and it dies immediately, context should NOT be prepended twice."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test Task",
            description="Test description",
            components=["backend"],
            tags=[],
            status="open",
        )
        orch._tracker.get_comments.return_value = [{"text": "Test comment", "createdAt": "2024-01-01T00:00:00Z"}]

        # Setup mock storage with session_id
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-old-123"

        # Create sessions that will be returned by create_session
        # Both attempts to create session will fail resume (same session_id),
        # so both will be fresh sessions
        first_fresh_session = _make_mock_session()
        first_fresh_session.send.side_effect = Exception("Session died")

        second_fresh_session = _make_mock_session()

        orch._agent = AsyncMock()
        # First call from send_message_to_agent creating initial session:
        #   - try resume with sess-old-123 → fails
        #   - create fresh session → first_fresh_session
        # Second call from _recreate_on_demand_session after first session dies:
        #   - try resume with sess-old-123 → fails again (same reason)
        #   - create fresh session → second_fresh_session
        orch._agent.create_session.side_effect = [
            Exception("Resume failed"),  # First resume attempt
            first_fresh_session,  # First fresh session (dies immediately)
            Exception("Resume failed"),  # Second resume attempt (recreation)
            second_fresh_session,  # Second fresh session (should work)
        ]

        # Send a message - will create first fresh session, prepend context, session dies, recreate
        await orch.send_message_to_agent("QR-1", "hello")
        await drain_background_tasks(orch)

        # Verify second session received message
        assert second_fresh_session.send.call_count == 1
        sent_message = second_fresh_session.send.call_args[0][0]

        # Count how many times "Test Task" appears
        task_count = sent_message.count("Test Task")
        assert task_count == 1, (
            f"Context should appear only ONCE, but found {task_count} occurrences. "
            f"Double-prepending detected! Message: {sent_message[:500]}"
        )

        # Verify the message still has context and original message
        assert "Test Task" in sent_message
        assert "hello" in sent_message

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_success_false_concurrent_recreation_retries_on_replacement(
        self, mock_gh, mock_ws, mock_resolver, mock_tracker_cls, mock_load_config
    ) -> None:
        """When success=False and another sender already recreated, should retry on replacement session."""
        import asyncio

        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        orch = Orchestrator()
        orch._event_bus = AsyncMock()

        # Setup mock tracker
        orch._tracker = MagicMock()
        orch._tracker.get_issue.return_value = TrackerIssue(
            key="QR-1",
            summary="Test",
            description="",
            components=[],
            tags=[],
            status="open",
        )

        # Setup mock storage
        orch._storage = AsyncMock()
        orch._storage.get_latest_session_id.return_value = "sess-123"

        # Create a dead session that returns success=False
        dead_session = _make_mock_session()
        dead_session.send.return_value = MagicMock(success=False, output="error", proposals=None)
        orch._on_demand_sessions["QR-1"] = dead_session

        # Create a new healthy session for recreation
        new_session = _make_mock_session()
        new_session.send.return_value = MagicMock(success=True, output="ok", proposals=None)
        orch._agent = AsyncMock()
        orch._agent.create_session.return_value = new_session

        # Simulate concurrent sends:
        # - First sender: gets success=False, wins race, recreates session
        # - Second sender: gets success=False, loses race (session already recreated)
        # The second sender should detect the replacement and retry on it

        # Start both sends concurrently
        task1 = asyncio.create_task(orch.send_message_to_agent("QR-1", "msg1"))
        task2 = asyncio.create_task(orch.send_message_to_agent("QR-1", "msg2"))
        await asyncio.gather(task1, task2)
        await drain_background_tasks(orch)

        # CRITICAL: Both messages must be sent to the new session
        # Without the fix, the second sender (who lost the race) silently drops its message
        assert new_session.send.call_count == 2, (
            f"Expected both messages to be sent (call_count=2), but got {new_session.send.call_count}. "
            f"The second sender's message was silently dropped when it detected concurrent recreation."
        )

        # Verify both messages were sent
        sent_messages = [call[0][0] for call in new_session.send.call_args_list]
        assert any("msg1" in msg for msg in sent_messages), "First message not sent"
        assert any("msg2" in msg for msg in sent_messages), "Second message not sent"


class TestReconcileTrackerStatuses:
    """F3: auto-kill on Tracker status change."""

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_running_task_cancelled_on_closed_status(
        self,
        mock_gh,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        """Running task whose Tracker status is 'closed' gets cancelled."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.return_value = [
            TrackerIssue(
                key="QR-1",
                summary="T",
                description="",
                components=[],
                tags=[],
                status="closed",
            ),
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        # Mock dispatcher with a running session
        mock_dispatcher = MagicMock()
        mock_session = AsyncMock()
        mock_dispatcher.get_running_sessions.return_value = ["QR-1"]
        mock_dispatcher.get_running_session.return_value = mock_session
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        orch._cancel_task = AsyncMock()

        await orch._reconcile_tracker_statuses()

        orch._cancel_task.assert_awaited_once()
        assert "QR-1" in orch._cancel_task.call_args[0][0]

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_pr_tracked_removed_no_terminal_event(
        self,
        mock_gh,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        """PR-tracked task: remove PR, no terminal event published."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.return_value = [
            TrackerIssue(
                key="QR-2",
                summary="T",
                description="",
                components=[],
                tags=[],
                status="closed",
            ),
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        # No running session
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = []
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher

        # PR monitor has this task
        mock_pr_monitor = MagicMock()
        mock_pr_monitor.get_tracked.return_value = {"QR-2": MagicMock()}
        mock_pr_monitor._tracked_prs = {"QR-2": MagicMock()}
        mock_pr_monitor.remove = AsyncMock()
        orch._pr_monitor = mock_pr_monitor

        mock_ni_monitor = MagicMock()
        mock_ni_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = mock_ni_monitor

        orch._storage = AsyncMock()
        orch._dispatched.add("QR-2")

        queue = orch._event_bus.subscribe_global()

        await orch._reconcile_tracker_statuses()

        # PR removed
        mock_pr_monitor.remove.assert_awaited_once_with("QR-2")
        # record_pr_cancelled called
        orch._storage.record_pr_cancelled.assert_awaited_once()
        # dispatched cleared
        assert "QR-2" not in orch._dispatched

        # No TASK_FAILED event (terminal event already published as PR_TRACKED)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        failed = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed) == 0

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_needs_info_removed_publishes_task_failed(
        self,
        mock_gh,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        """Needs-info task: remove + publish TASK_FAILED."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.return_value = [
            TrackerIssue(
                key="QR-3",
                summary="T",
                description="",
                components=[],
                tags=[],
                status="cancelled",
            ),
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        # No running session
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = []
        mock_dispatcher.get_running_session.return_value = None
        orch._dispatcher = mock_dispatcher

        # No PR tracked
        mock_pr_monitor = MagicMock()
        mock_pr_monitor.get_tracked.return_value = {}
        mock_pr_monitor._tracked_prs = {}
        orch._pr_monitor = mock_pr_monitor

        # Needs-info has this task
        mock_ni_monitor = MagicMock()
        mock_ni_monitor.get_tracked.return_value = {"QR-3": MagicMock()}
        mock_ni_monitor._tracked = {"QR-3": MagicMock()}
        mock_ni_monitor.remove = AsyncMock()
        orch._needs_info_monitor = mock_ni_monitor

        queue = orch._event_bus.subscribe_global()

        await orch._reconcile_tracker_statuses()

        mock_ni_monitor.remove.assert_awaited_once_with("QR-3")

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        failed = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed) == 1
        assert failed[0].data["cancelled"] is True

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_open_status_ignored(
        self,
        mock_gh,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        """Tasks with open status are NOT killed."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.return_value = [
            TrackerIssue(
                key="QR-4",
                summary="T",
                description="",
                components=[],
                tags=[],
                status="open",
            ),
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = ["QR-4"]
        mock_dispatcher.get_running_session.return_value = AsyncMock()
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        orch._cancel_task = AsyncMock()

        await orch._reconcile_tracker_statuses()

        orch._cancel_task.assert_not_awaited()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_tracker_api_error_skips(
        self,
        mock_gh,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        """Tracker API error should skip reconciliation gracefully."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.side_effect = requests.ConnectionError("API down")
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = ["QR-5"]
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        orch._cancel_task = AsyncMock()

        # Should not raise
        await orch._reconcile_tracker_statuses()
        orch._cancel_task.assert_not_awaited()

    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def test_status_cache_populated(
        self,
        mock_gh,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ) -> None:
        """Reconciliation populates _tracker_status_cache."""
        from orchestrator.config import Config, ReposConfig
        from orchestrator.main import Orchestrator
        from orchestrator.tracker_client import TrackerIssue

        mock_load_config.return_value = Config(
            tracker_token="t",
            tracker_org_id="o",
            repos_config=ReposConfig(),
        )
        mock_tracker = MagicMock()
        mock_tracker.search.return_value = [
            TrackerIssue(
                key="QR-6",
                summary="T",
                description="",
                components=[],
                tags=[],
                status="inProgress",
            ),
        ]
        mock_tracker_cls.return_value = mock_tracker

        orch = Orchestrator()
        orch._tracker = mock_tracker

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_sessions.return_value = ["QR-6"]
        mock_dispatcher.get_running_session.return_value = AsyncMock()
        orch._dispatcher = mock_dispatcher
        orch._pr_monitor = MagicMock()
        orch._pr_monitor.get_tracked.return_value = {}
        orch._needs_info_monitor = MagicMock()
        orch._needs_info_monitor.get_tracked.return_value = {}

        await orch._reconcile_tracker_statuses()

        assert orch._tracker_status_cache["QR-6"] == "inProgress"
