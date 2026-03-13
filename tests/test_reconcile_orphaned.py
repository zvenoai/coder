"""Tests for orphaned task reconciliation."""

from unittest.mock import MagicMock, patch

import pytest
import requests as req

from orchestrator.constants import EventType, is_review_status
from orchestrator.event_bus import Event, EventBus
from orchestrator.needs_info_monitor import is_needs_info_status
from orchestrator.pr_monitor import find_pr_url_in_comments
from orchestrator.tracker_client import TrackerIssue

# ------------------------------------------------------------------ #
# EventBus.get_orphaned_tasks()
# ------------------------------------------------------------------ #


class TestGetOrphanedTasks:
    async def test_no_tasks_returns_empty(self) -> None:
        bus = EventBus()
        assert bus.get_orphaned_tasks() == []

    async def test_task_with_terminal_event_not_orphaned(self) -> None:
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-1",
                data={},
                timestamp=2.0,
            )
        )
        assert bus.get_orphaned_tasks() == []

    async def test_task_without_terminal_event_is_orphaned(self) -> None:
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        assert bus.get_orphaned_tasks() == ["QR-1"]

    async def test_pr_tracked_is_terminal(self) -> None:
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-1",
                data={},
                timestamp=2.0,
            )
        )
        assert bus.get_orphaned_tasks() == []

    async def test_task_failed_is_terminal(self) -> None:
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-1",
                data={},
                timestamp=2.0,
            )
        )
        assert bus.get_orphaned_tasks() == []

    async def test_task_skipped_is_terminal(self) -> None:
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.TASK_SKIPPED,
                task_key="QR-1",
                data={},
                timestamp=2.0,
            )
        )
        assert bus.get_orphaned_tasks() == []

    async def test_task_deferred_is_terminal(self) -> None:
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.TASK_DEFERRED,
                task_key="QR-1",
                data={},
                timestamp=2.0,
            )
        )
        assert bus.get_orphaned_tasks() == []

    async def test_pr_merged_is_terminal(self) -> None:
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.PR_MERGED,
                task_key="QR-1",
                data={},
                timestamp=2.0,
            )
        )
        assert bus.get_orphaned_tasks() == []

    async def test_non_terminal_events_dont_close(self) -> None:
        """Events like AGENT_OUTPUT or NEEDS_INFO don't close a run."""
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.AGENT_OUTPUT,
                task_key="QR-1",
                data={},
                timestamp=2.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.NEEDS_INFO,
                task_key="QR-1",
                data={},
                timestamp=3.0,
            )
        )
        assert bus.get_orphaned_tasks() == ["QR-1"]

    async def test_multiple_runs_only_checks_latest(self) -> None:
        """Only the most recent task_started matters."""
        bus = EventBus()
        # First run — completed
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-1",
                data={},
                timestamp=2.0,
            )
        )
        # Second run — orphaned
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=3.0,
            )
        )
        assert bus.get_orphaned_tasks() == ["QR-1"]

    async def test_multiple_tasks_mixed(self) -> None:
        """Multiple tasks, some orphaned, some not."""
        bus = EventBus()
        # QR-1: orphaned
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        # QR-2: completed
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-2",
                data={},
                timestamp=2.0,
            )
        )
        await bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-2",
                data={},
                timestamp=3.0,
            )
        )
        # QR-3: orphaned
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-3",
                data={},
                timestamp=4.0,
            )
        )
        result = bus.get_orphaned_tasks()
        assert sorted(result) == ["QR-1", "QR-3"]

    async def test_task_without_started_not_orphaned(self) -> None:
        """Tasks with no task_started event are not orphaned."""
        bus = EventBus()
        await bus.publish(
            Event(
                type=EventType.AGENT_OUTPUT,
                task_key="QR-1",
                data={},
                timestamp=1.0,
            )
        )
        assert bus.get_orphaned_tasks() == []


# ------------------------------------------------------------------ #
# Orchestrator._reconcile_orphaned_tasks()
# ------------------------------------------------------------------ #


class TestReconcileOrphanedTasks:
    @patch("orchestrator.main.load_config")
    @patch("orchestrator.main.TrackerClient")
    @patch("orchestrator.main.RepoResolver")
    @patch("orchestrator.main.WorkspaceManager")
    @patch("orchestrator.main.GitHubClient")
    async def _make_orchestrator(
        self,
        mock_gh,
        mock_ws,
        mock_resolver,
        mock_tracker_cls,
        mock_load_config,
    ):
        cfg = MagicMock()
        cfg.tracker_token = "t"
        cfg.tracker_org_id = "o"
        cfg.tracker_queue = "QR"
        cfg.tracker_tag = "ai-task"
        cfg.tracker_project_id = 13
        cfg.tracker_boards = [14]
        cfg.poll_interval_seconds = 10
        cfg.max_concurrent_agents = 2
        cfg.worktree_base_dir = "/tmp/wt"
        cfg.db_path = "/tmp/test.db"
        cfg.repos_config.all_repos = []
        cfg.github_token = "gh"
        cfg.supervisor_enabled = False
        cfg.k8s_logs_enabled = False
        mock_load_config.return_value = cfg

        from orchestrator.main import Orchestrator

        return Orchestrator()

    async def test_no_orphaned_tasks_is_noop(self) -> None:
        orch = await self._make_orchestrator()
        # No events → no orphans
        await orch._reconcile_orphaned_tasks()
        # Should not call tracker.search
        orch._tracker.search.assert_not_called()

    async def test_resolved_task_gets_completed_event(self) -> None:
        orch = await self._make_orchestrator()
        # Simulate orphaned task
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-170",
                data={},
                timestamp=1.0,
            )
        )

        # Mock Tracker returning resolved status
        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-170",
                summary="Test",
                description="",
                components=[],
                tags=[],
                status="resolved",
            ),
        ]

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-170")
        terminal = [e for e in history if e.type == EventType.TASK_COMPLETED]
        assert len(terminal) == 1
        assert terminal[0].data["reconciled"] is True

    async def test_cancelled_task_gets_failed_event(self) -> None:
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-185",
                data={},
                timestamp=1.0,
            )
        )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-185",
                summary="Test",
                description="",
                components=[],
                tags=[],
                status="cancelled",
            ),
        ]

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-185")
        terminal = [e for e in history if e.type == EventType.TASK_FAILED]
        assert len(terminal) == 1
        assert terminal[0].data["reconciled"] is True
        assert terminal[0].data["cancelled"] is True

    async def test_open_task_gets_orphaned_failed_event(self) -> None:
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-190",
                data={},
                timestamp=1.0,
            )
        )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-190",
                summary="Test",
                description="",
                components=[],
                tags=[],
                status="inProgress",
            ),
        ]

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-190")
        terminal = [e for e in history if e.type == EventType.TASK_FAILED]
        assert len(terminal) == 1
        assert terminal[0].data["reconciled"] is True
        assert terminal[0].data["orphaned"] is True

    async def test_tracker_error_is_handled_gracefully(
        self,
    ) -> None:
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-199",
                data={},
                timestamp=1.0,
            )
        )

        import requests as req

        orch._tracker.search.side_effect = req.RequestException(
            "Network error",
        )

        # Should not raise
        await orch._reconcile_orphaned_tasks()

        # No terminal event should be published
        history = orch._event_bus.get_task_history("QR-199")
        terminal = [
            e
            for e in history
            if e.type
            in (
                EventType.TASK_COMPLETED,
                EventType.TASK_FAILED,
            )
        ]
        assert len(terminal) == 0

    async def test_multiple_orphaned_tasks_batch(self) -> None:
        orch = await self._make_orchestrator()
        # Three orphaned tasks
        for key in ("QR-10", "QR-20", "QR-30"):
            await orch._event_bus.publish(
                Event(
                    type=EventType.TASK_STARTED,
                    task_key=key,
                    data={},
                    timestamp=1.0,
                )
            )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-10",
                summary="",
                description="",
                components=[],
                tags=[],
                status="resolved",
            ),
            TrackerIssue(
                key="QR-20",
                summary="",
                description="",
                components=[],
                tags=[],
                status="cancelled",
            ),
            TrackerIssue(
                key="QR-30",
                summary="",
                description="",
                components=[],
                tags=[],
                status="open",
            ),
        ]

        await orch._reconcile_orphaned_tasks()

        # QR-10: completed
        h10 = orch._event_bus.get_task_history("QR-10")
        assert any(e.type == EventType.TASK_COMPLETED for e in h10)

        # QR-20: failed (cancelled)
        h20 = orch._event_bus.get_task_history("QR-20")
        failed_20 = [e for e in h20 if e.type == EventType.TASK_FAILED]
        assert len(failed_20) == 1
        assert failed_20[0].data["cancelled"] is True

        # QR-30: failed (orphaned)
        h30 = orch._event_bus.get_task_history("QR-30")
        failed_30 = [e for e in h30 if e.type == EventType.TASK_FAILED]
        assert len(failed_30) == 1
        assert failed_30[0].data["orphaned"] is True

    async def test_task_not_in_tracker_treated_as_open(
        self,
    ) -> None:
        """If Tracker doesn't return the issue, treat as open/orphaned."""
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-999",
                data={},
                timestamp=1.0,
            )
        )

        # Tracker returns empty — issue not found
        orch._tracker.search.return_value = []

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-999")
        terminal = [e for e in history if e.type == EventType.TASK_FAILED]
        assert len(terminal) == 1
        assert terminal[0].data["orphaned"] is True

    async def test_needs_info_task_is_skipped(self) -> None:
        """Needs-info task should NOT get a terminal event."""
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-211",
                data={},
                timestamp=1.0,
            )
        )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-211",
                summary="Test",
                description="",
                components=[],
                tags=[],
                status="needsInfo",
            ),
        ]

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-211")
        terminal = [
            e
            for e in history
            if e.type
            in (
                EventType.TASK_COMPLETED,
                EventType.TASK_FAILED,
            )
        ]
        assert len(terminal) == 0

    @pytest.mark.parametrize(
        "status",
        ["needsInfo", "needInfo", "Need Info", "needs_info"],
    )
    async def test_needs_info_variant_statuses(self, status: str) -> None:
        """All needs-info status variants should be skipped."""
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-300",
                data={},
                timestamp=1.0,
            )
        )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-300",
                summary="",
                description="",
                components=[],
                tags=[],
                status=status,
            ),
        ]

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-300")
        terminal = [
            e
            for e in history
            if e.type
            in (
                EventType.TASK_COMPLETED,
                EventType.TASK_FAILED,
            )
        ]
        assert len(terminal) == 0

    async def test_review_task_with_pr_is_skipped(self) -> None:
        """Review task with PR in comments should NOT get terminal event."""
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-250",
                data={},
                timestamp=1.0,
            )
        )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-250",
                summary="Test",
                description="",
                components=[],
                tags=[],
                status="inReview",
            ),
        ]
        # PR exists in comments
        orch._tracker.get_comments.return_value = [
            {
                "id": 1,
                "text": "PR: https://github.com/org/repo/pull/42",
            },
        ]

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-250")
        terminal = [
            e
            for e in history
            if e.type
            in (
                EventType.TASK_COMPLETED,
                EventType.TASK_FAILED,
            )
        ]
        assert len(terminal) == 0

    async def test_review_task_without_pr_gets_orphaned(
        self,
    ) -> None:
        """Review task without PR in comments IS orphaned."""
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-260",
                data={},
                timestamp=1.0,
            )
        )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-260",
                summary="Test",
                description="",
                components=[],
                tags=[],
                status="inReview",
            ),
        ]
        # No PR in comments
        orch._tracker.get_comments.return_value = [
            {"id": 1, "text": "Some comment without PR link"},
        ]

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-260")
        terminal = [e for e in history if e.type == EventType.TASK_FAILED]
        assert len(terminal) == 1
        assert terminal[0].data["orphaned"] is True

    async def test_review_comment_fetch_fails_gets_orphaned(
        self,
    ) -> None:
        """If comment fetch fails for review task, fail-safe to orphaned."""
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-270",
                data={},
                timestamp=1.0,
            )
        )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-270",
                summary="Test",
                description="",
                components=[],
                tags=[],
                status="review",
            ),
        ]
        # Comment fetch fails
        orch._tracker.get_comments.side_effect = req.RequestException("API error")

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-270")
        terminal = [e for e in history if e.type == EventType.TASK_FAILED]
        assert len(terminal) == 1
        assert terminal[0].data["orphaned"] is True

    async def test_mixed_batch_with_needs_info_and_review(
        self,
    ) -> None:
        """Batch: resolved + needs-info + review+PR + review-no-PR + open."""
        orch = await self._make_orchestrator()
        keys = [
            "QR-1",
            "QR-2",
            "QR-3",
            "QR-4",
            "QR-5",
        ]
        for key in keys:
            await orch._event_bus.publish(
                Event(
                    type=EventType.TASK_STARTED,
                    task_key=key,
                    data={},
                    timestamp=1.0,
                )
            )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-1",
                summary="",
                description="",
                components=[],
                tags=[],
                status="resolved",
            ),
            TrackerIssue(
                key="QR-2",
                summary="",
                description="",
                components=[],
                tags=[],
                status="needsInfo",
            ),
            TrackerIssue(
                key="QR-3",
                summary="",
                description="",
                components=[],
                tags=[],
                status="inReview",
            ),
            TrackerIssue(
                key="QR-4",
                summary="",
                description="",
                components=[],
                tags=[],
                status="review",
            ),
            TrackerIssue(
                key="QR-5",
                summary="",
                description="",
                components=[],
                tags=[],
                status="open",
            ),
        ]

        def mock_get_comments(issue_key: str):
            if issue_key == "QR-3":
                return [
                    {
                        "id": 1,
                        "text": "https://github.com/o/r/pull/10",
                    },
                ]
            if issue_key == "QR-4":
                return [{"id": 1, "text": "No PR here"}]
            return []

        orch._tracker.get_comments.side_effect = mock_get_comments

        await orch._reconcile_orphaned_tasks()

        # QR-1: resolved → TASK_COMPLETED
        h1 = orch._event_bus.get_task_history("QR-1")
        assert any(e.type == EventType.TASK_COMPLETED for e in h1)

        # QR-2: needs-info → skipped (no terminal)
        h2 = orch._event_bus.get_task_history("QR-2")
        assert not any(e.type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED) for e in h2)

        # QR-3: review + PR → skipped (no terminal)
        h3 = orch._event_bus.get_task_history("QR-3")
        assert not any(e.type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED) for e in h3)

        # QR-4: review, no PR → TASK_FAILED(orphaned)
        h4 = orch._event_bus.get_task_history("QR-4")
        failed_4 = [e for e in h4 if e.type == EventType.TASK_FAILED]
        assert len(failed_4) == 1
        assert failed_4[0].data["orphaned"] is True

        # QR-5: open → TASK_FAILED(orphaned)
        h5 = orch._event_bus.get_task_history("QR-5")
        failed_5 = [e for e in h5 if e.type == EventType.TASK_FAILED]
        assert len(failed_5) == 1
        assert failed_5[0].data["orphaned"] is True

    async def test_open_task_still_gets_orphaned_regression(
        self,
    ) -> None:
        """Regression: open/inProgress must still produce TASK_FAILED."""
        orch = await self._make_orchestrator()
        await orch._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-400",
                data={},
                timestamp=1.0,
            )
        )

        orch._tracker.search.return_value = [
            TrackerIssue(
                key="QR-400",
                summary="",
                description="",
                components=[],
                tags=[],
                status="open",
            ),
        ]

        await orch._reconcile_orphaned_tasks()

        history = orch._event_bus.get_task_history("QR-400")
        terminal = [e for e in history if e.type == EventType.TASK_FAILED]
        assert len(terminal) == 1
        assert terminal[0].data["orphaned"] is True


# ------------------------------------------------------------------ #
# Status matchers
# ------------------------------------------------------------------ #


class TestStatusMatchers:
    @pytest.mark.parametrize(
        "status",
        [
            "inReview",
            "review",
            "testing",
            "На проверке",
            "ревью",
        ],
    )
    def test_is_review_status_positive(self, status: str) -> None:
        assert is_review_status(status) is True

    @pytest.mark.parametrize(
        "status",
        [
            "open",
            "inProgress",
            "resolved",
            "needsInfo",
            "cancelled",
        ],
    )
    def test_is_review_status_negative(self, status: str) -> None:
        assert is_review_status(status) is False

    @pytest.mark.parametrize(
        "status",
        [
            "needsInfo",
            "needInfo",
            "needs_info",
            "Need Info",
        ],
    )
    def test_is_needs_info_status_positive(self, status: str) -> None:
        assert is_needs_info_status(status) is True

    @pytest.mark.parametrize(
        "status",
        [
            "open",
            "inProgress",
            "resolved",
            "inReview",
            "cancelled",
        ],
    )
    def test_is_needs_info_status_negative(self, status: str) -> None:
        assert is_needs_info_status(status) is False


# ------------------------------------------------------------------ #
# find_pr_url_in_comments()
# ------------------------------------------------------------------ #


class TestFindPrUrlInComments:
    def test_pr_found_in_comments(self) -> None:
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            {"id": 1, "text": "Working on it"},
            {
                "id": 2,
                "text": "PR: https://github.com/org/repo/pull/99",
            },
        ]
        result = find_pr_url_in_comments(tracker, "QR-100")
        assert result == "https://github.com/org/repo/pull/99"

    def test_no_pr_in_comments(self) -> None:
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            {"id": 1, "text": "Just a regular comment"},
        ]
        result = find_pr_url_in_comments(tracker, "QR-100")
        assert result is None

    def test_api_error_returns_none(self) -> None:
        tracker = MagicMock()
        tracker.get_comments.side_effect = req.RequestException("API error")
        result = find_pr_url_in_comments(tracker, "QR-100")
        assert result is None

    def test_empty_comments_returns_none(self) -> None:
        tracker = MagicMock()
        tracker.get_comments.return_value = []
        result = find_pr_url_in_comments(tracker, "QR-100")
        assert result is None

    def test_returns_newest_pr(self) -> None:
        """When multiple PRs exist, returns the newest one."""
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            {
                "id": 1,
                "text": "https://github.com/org/repo/pull/10",
            },
            {
                "id": 2,
                "text": "https://github.com/org/repo/pull/20",
            },
        ]
        result = find_pr_url_in_comments(tracker, "QR-100")
        assert result == "https://github.com/org/repo/pull/20"
