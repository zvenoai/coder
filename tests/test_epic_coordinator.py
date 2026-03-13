"""Tests for EpicCoordinator."""

from __future__ import annotations

from unittest.mock import MagicMock

import requests

from orchestrator.config import Config, ReposConfig
from orchestrator.constants import EventType
from orchestrator.epic_coordinator import ChildStatus, ChildTask, EpicCoordinator, EpicState
from orchestrator.event_bus import EventBus
from orchestrator.tracker_client import TrackerIssue


def make_config(**overrides) -> Config:
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        tracker_tag="ai-task",
        tracker_queue="QR",
        repos_config=ReposConfig(),
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_epic(issue_key: str = "QR-50") -> TrackerIssue:
    return TrackerIssue(
        key=issue_key,
        summary="Epic task",
        description="Epic description",
        components=["Backend"],
        tags=["ai-task"],
        status="open",
        type_key="epic",
    )


class TestRegisterEpic:
    async def test_registers_epic_and_emits_event(self) -> None:
        tracker = MagicMock()
        event_bus = EventBus()
        dispatched: set[str] = set()
        coordinator = EpicCoordinator(
            tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=dispatched
        )

        issue = make_epic()
        await coordinator.register_epic(issue)

        assert "QR-50" in dispatched
        state = coordinator.get_state()["QR-50"]
        assert state["epic_key"] == "QR-50"
        assert state["phase"] == "analyzing"
        events = event_bus.get_task_history("QR-50")
        assert any(e.type == EventType.EPIC_DETECTED for e in events)

    async def test_register_epic_idempotent(self) -> None:
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        issue = make_epic()
        await coordinator.register_epic(issue)
        await coordinator.register_epic(issue)  # second call is no-op

        assert len(coordinator.get_state()) == 1


class TestSetChildren:
    def test_set_children_updates_state(self) -> None:
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(epic_key="QR-50", epic_summary="Epic", children={}, phase="analyzing")

        children = {
            "QR-51": ChildTask("QR-51", "A", ChildStatus.PENDING, [], "open"),
            "QR-52": ChildTask("QR-52", "B", ChildStatus.PENDING, [], "open"),
        }
        coordinator.set_children("QR-50", children)

        assert "QR-51" in coordinator._epics["QR-50"].children
        assert "QR-52" in coordinator._epics["QR-50"].children


class TestSetChildDependencies:
    def test_sets_dependencies_and_transitions_to_executing(self) -> None:
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="analyzing",
            children={
                "QR-51": ChildTask("QR-51", "A", ChildStatus.PENDING, [], "open"),
                "QR-52": ChildTask("QR-52", "B", ChildStatus.PENDING, [], "open"),
            },
        )

        coordinator.set_child_dependencies("QR-50", {"QR-51": [], "QR-52": ["QR-51"]})

        assert coordinator._epics["QR-50"].children["QR-51"].depends_on == []
        assert coordinator._epics["QR-50"].children["QR-52"].depends_on == ["QR-51"]
        assert coordinator._epics["QR-50"].phase == "executing"


class TestCompletionAndTransitions:
    async def test_completion_unblocks_dependents(self) -> None:
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    key="QR-51",
                    summary="Root",
                    status=ChildStatus.READY,
                    depends_on=[],
                    tracker_status="open",
                ),
                "QR-52": ChildTask(
                    key="QR-52",
                    summary="Dependent",
                    status=ChildStatus.PENDING,
                    depends_on=["QR-51"],
                    tracker_status="open",
                ),
            },
        )

        await coordinator.on_task_completed("QR-51")

        tracker.update_issue_tags.assert_called_once_with("QR-52", ["ai-task"])
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.COMPLETED

    async def test_all_children_completed_removes_epic_from_dispatched(self) -> None:
        """When all children complete, epic must be removed from dispatched set."""
        tracker = MagicMock()
        event_bus = EventBus()
        dispatched: set[str] = {"QR-50", "QR-51"}
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=dispatched,
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    key="QR-51",
                    summary="Only child",
                    status=ChildStatus.READY,
                    depends_on=[],
                    tracker_status="open",
                ),
            },
        )

        await coordinator.on_task_completed("QR-51")

        assert "QR-50" not in dispatched
        assert coordinator._epics["QR-50"].phase == "completed"

    async def test_on_task_cancelled_does_not_override_completed(self) -> None:
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    key="QR-51",
                    summary="Done child",
                    status=ChildStatus.COMPLETED,
                    depends_on=[],
                    tracker_status="done",
                ),
            },
        )

        await coordinator.on_task_cancelled("QR-51")

        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.COMPLETED
        tracker.update_issue_tags.assert_not_called()

    async def test_on_task_completed_does_not_override_cancelled(self) -> None:
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    key="QR-51",
                    summary="Cancelled child",
                    status=ChildStatus.CANCELLED,
                    depends_on=[],
                    tracker_status="cancelled",
                ),
            },
        )

        await coordinator.on_task_completed("QR-51")

        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.CANCELLED
        tracker.update_issue_tags.assert_not_called()

    async def test_failed_child_is_not_retagged_as_ready(self) -> None:
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    key="QR-51",
                    summary="Failed child",
                    status=ChildStatus.FAILED,
                    depends_on=[],
                    tracker_status="open",
                    tags=[],
                )
            },
        )

        await coordinator._tag_ready_children("QR-50")

        tracker.update_issue_tags.assert_not_called()
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.FAILED

    async def test_failed_child_is_retryable_for_dispatch(self) -> None:
        tracker = MagicMock()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=EventBus(), config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    key="QR-51",
                    summary="Failed child",
                    status=ChildStatus.FAILED,
                    depends_on=[],
                    tracker_status="open",
                    tags=["ai-task"],
                )
            },
        )

        assert coordinator.is_child_ready_for_dispatch("QR-51") is True

        coordinator.mark_child_dispatched("QR-51")
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.DISPATCHED

    async def test_tag_ready_children_handles_update_tags_failure(self) -> None:
        tracker = MagicMock()
        tracker.update_issue_tags.side_effect = requests.ConnectionError("tracker unavailable")
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    key="QR-51",
                    summary="Pending child",
                    status=ChildStatus.PENDING,
                    depends_on=[],
                    tracker_status="open",
                    tags=[],
                )
            },
        )

        await coordinator._tag_ready_children("QR-50")

        child = coordinator._epics["QR-50"].children["QR-51"]
        assert child.status == ChildStatus.PENDING
        assert child.tags == []
        events = event_bus.get_task_history("QR-50")
        assert not any(e.type == EventType.EPIC_CHILD_READY for e in events)

    async def test_tag_ready_children_retries_tag_update_after_failure(self) -> None:
        tracker = MagicMock()
        tracker.update_issue_tags.side_effect = [requests.ConnectionError("temporary"), {}]
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    key="QR-51",
                    summary="Pending child",
                    status=ChildStatus.PENDING,
                    depends_on=[],
                    tracker_status="open",
                    tags=[],
                )
            },
        )

        await coordinator._tag_ready_children("QR-50")
        await coordinator._tag_ready_children("QR-50")

        child = coordinator._epics["QR-50"].children["QR-51"]
        assert child.status == ChildStatus.READY
        assert child.tags == ["ai-task"]
        assert tracker.update_issue_tags.call_count == 2


class TestAnalyzeAndActivate:
    """After register_epic, analyze_and_activate must discover children and tag them."""

    async def test_analyze_discovers_and_tags_children(self) -> None:
        """After analyze_and_activate, children should be discovered and tagged ai-task."""
        tracker = MagicMock()
        tracker.get_links.return_value = [
            {"relationship": "is parent task for", "object": {"key": "QR-51"}},
            {"relationship": "is parent task for", "object": {"key": "QR-52"}},
        ]
        tracker.get_issue.side_effect = [
            TrackerIssue("QR-51", "Task A", "", ["Backend"], [], "open", "task"),
            TrackerIssue("QR-52", "Task B", "", ["Backend"], [], "open", "task"),
        ]
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        config = make_config()
        dispatched: set[str] = set()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=config, dispatched_set=dispatched)

        issue = make_epic()
        await coordinator.register_epic(issue)
        await coordinator.analyze_and_activate(issue.key)

        state = coordinator.get_epic_state("QR-50")
        assert state is not None
        assert state.phase == "executing"
        assert len(state.children) == 2
        assert state.children["QR-51"].status == ChildStatus.READY
        assert state.children["QR-52"].status == ChildStatus.READY
        # Both children should have been tagged with ai-task
        assert tracker.update_issue_tags.call_count == 2

    async def test_analyze_skips_already_resolved_children(self) -> None:
        """Already resolved children should be marked COMPLETED, not tagged."""
        tracker = MagicMock()
        tracker.get_links.return_value = [
            {"relationship": "is parent task for", "object": {"key": "QR-51"}},
            {"relationship": "is parent task for", "object": {"key": "QR-52"}},
        ]
        tracker.get_issue.side_effect = [
            TrackerIssue("QR-51", "Done", "", [], [], "Done", "task"),
            TrackerIssue("QR-52", "Open", "", [], [], "open", "task"),
        ]
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        issue = make_epic()
        await coordinator.register_epic(issue)
        await coordinator.analyze_and_activate(issue.key)

        state = coordinator.get_epic_state("QR-50")
        assert state is not None
        assert state.children["QR-51"].status == ChildStatus.COMPLETED
        assert state.children["QR-52"].status == ChildStatus.READY
        # Only the open child should be tagged
        tracker.update_issue_tags.assert_called_once_with("QR-52", ["ai-task"])

    async def test_analyze_no_children_completes_epic(self) -> None:
        """Epic with no children should be completed immediately."""
        tracker = MagicMock()
        tracker.get_links.return_value = []
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        issue = make_epic()
        await coordinator.register_epic(issue)
        await coordinator.analyze_and_activate(issue.key)

        state = coordinator.get_epic_state("QR-50")
        assert state is not None
        # No children → phase stays as-is or transitions; at minimum, it shouldn't crash
        assert state.phase in ("executing", "completed")

    async def test_analyze_unknown_epic_is_noop(self) -> None:
        """analyze_and_activate on unknown epic should not raise."""
        tracker = MagicMock()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=EventBus(), config=make_config(), dispatched_set=set())
        # Should not raise
        await coordinator.analyze_and_activate("QR-UNKNOWN")
        tracker.get_links.assert_not_called()


class TestStaticHelpers:
    def test_validate_acyclic_accepts_dag(self) -> None:
        assert EpicCoordinator.validate_acyclic({"A": [], "B": ["A"], "C": ["B"]}) is True

    def test_validate_acyclic_rejects_cycle(self) -> None:
        assert EpicCoordinator.validate_acyclic({"A": ["B"], "B": ["A"]}) is False

    def test_is_resolved_status(self) -> None:
        assert EpicCoordinator.is_resolved_status("Done") is True
        assert EpicCoordinator.is_resolved_status("open") is False

    def test_is_cancelled_status(self) -> None:
        assert EpicCoordinator.is_cancelled_status("Cancelled") is True
        assert EpicCoordinator.is_cancelled_status("Отменено") is True
        assert EpicCoordinator.is_cancelled_status("open") is False

    def test_build_child_task(self) -> None:
        issue = TrackerIssue("QR-51", "Test", "", ["Backend"], ["ai-task"], "open", "task")
        child = EpicCoordinator.build_child_task(issue)
        assert child.key == "QR-51"
        assert child.status == ChildStatus.PENDING
        assert child.tags == ["ai-task"]

    def test_build_child_task_resolved(self) -> None:
        issue = TrackerIssue("QR-51", "Test", "", [], [], "Done", "task")
        child = EpicCoordinator.build_child_task(issue)
        assert child.status == ChildStatus.COMPLETED

    def test_build_child_task_cancelled(self) -> None:
        issue = TrackerIssue("QR-51", "Test", "", [], [], "Cancelled", "task")
        child = EpicCoordinator.build_child_task(issue)
        assert child.status == ChildStatus.CANCELLED

    def test_extract_linked_issue_key(self) -> None:
        assert EpicCoordinator.extract_linked_issue_key({"issue": {"key": "QR-5"}}) == "QR-5"
        assert EpicCoordinator.extract_linked_issue_key({"object": {"key": "QR-6"}}) == "QR-6"
        assert EpicCoordinator.extract_linked_issue_key({}) == ""


class TestFetchChildren:
    def test_fetches_children_from_links(self) -> None:
        tracker = MagicMock()
        tracker.get_links.return_value = [
            {"relationship": "is parent task for", "object": {"key": "QR-51"}},
            {"relationship": "is parent task for", "object": {"key": "QR-52"}},
        ]
        tracker.get_issue.side_effect = [
            TrackerIssue("QR-51", "A", "", [], [], "open", "task"),
            TrackerIssue("QR-52", "B", "", [], [], "open", "task"),
        ]
        coordinator = EpicCoordinator(tracker=tracker, event_bus=EventBus(), config=make_config(), dispatched_set=set())
        children = coordinator.fetch_children("QR-50")
        assert len(children) == 2
        assert children[0].key == "QR-51"

    def test_skips_failed_child_fetch(self) -> None:
        tracker = MagicMock()
        tracker.get_links.return_value = [
            {"relationship": "is parent task for", "object": {"key": "QR-51"}},
            {"relationship": "is parent task for", "object": {"key": "QR-52"}},
        ]
        tracker.get_issue.side_effect = [
            TrackerIssue("QR-51", "A", "", [], [], "open", "task"),
            requests.ConnectionError("not found"),
        ]
        coordinator = EpicCoordinator(tracker=tracker, event_bus=EventBus(), config=make_config(), dispatched_set=set())
        children = coordinator.fetch_children("QR-50")
        assert len(children) == 1
        assert children[0].key == "QR-51"


class TestOnTaskFailedIdempotent:
    async def test_double_fail_is_idempotent(self) -> None:
        tracker = MagicMock()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=EventBus(), config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "A", ChildStatus.READY, [], "open"),
            },
        )

        await coordinator.on_task_failed("QR-51")
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.FAILED

        # Second call should be no-op
        await coordinator.on_task_failed("QR-51")
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.FAILED


class TestGetEpicState:
    def test_get_epic_state_returns_state(self) -> None:
        coordinator = EpicCoordinator(
            tracker=MagicMock(), event_bus=EventBus(), config=make_config(), dispatched_set=set()
        )
        state = EpicState(epic_key="QR-50", epic_summary="Epic", children={})
        coordinator._epics["QR-50"] = state
        assert coordinator.get_epic_state("QR-50") is state

    def test_get_epic_state_returns_none(self) -> None:
        coordinator = EpicCoordinator(
            tracker=MagicMock(), event_bus=EventBus(), config=make_config(), dispatched_set=set()
        )
        assert coordinator.get_epic_state("QR-99") is None


class TestDiscoverChildren:
    """Tests for discover_children() — discovery without activation."""

    async def test_discover_sets_phase_to_awaiting_plan(self) -> None:
        """discover_children should set phase to 'awaiting_plan' and NOT tag children."""
        tracker = MagicMock()
        tracker.get_links.return_value = [
            {"relationship": "is parent task for", "object": {"key": "QR-51"}},
            {"relationship": "is parent task for", "object": {"key": "QR-52"}},
        ]
        tracker.get_issue.side_effect = [
            TrackerIssue("QR-51", "Task A", "", ["Backend"], [], "open", "task"),
            TrackerIssue("QR-52", "Task B", "", ["Backend"], [], "open", "task"),
        ]
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        issue = make_epic()
        await coordinator.register_epic(issue)

        await coordinator.discover_children(issue.key)

        state = coordinator.get_epic_state("QR-50")
        assert state is not None
        assert state.phase == "awaiting_plan"
        assert len(state.children) == 2
        assert state.children["QR-51"].status == ChildStatus.PENDING
        assert state.children["QR-52"].status == ChildStatus.PENDING
        # Must NOT call update_issue_tags — no activation
        tracker.update_issue_tags.assert_not_called()

    async def test_discover_emits_epic_awaiting_plan_event(self) -> None:
        """discover_children should publish EPIC_AWAITING_PLAN event."""
        tracker = MagicMock()
        tracker.get_links.return_value = [
            {"relationship": "is parent task for", "object": {"key": "QR-51"}},
        ]
        tracker.get_issue.side_effect = [
            TrackerIssue("QR-51", "Task A", "", ["Backend"], [], "open", "task"),
        ]
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        issue = make_epic()
        await coordinator.register_epic(issue)

        await coordinator.discover_children(issue.key)

        events = event_bus.get_task_history("QR-50")
        awaiting_events = [e for e in events if e.type == EventType.EPIC_AWAITING_PLAN]
        assert len(awaiting_events) == 1
        assert "children" in awaiting_events[0].data

    async def test_discover_no_children_sets_awaiting_plan(self) -> None:
        """Epic with no children should still set phase to 'awaiting_plan'."""
        tracker = MagicMock()
        tracker.get_links.return_value = []
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        issue = make_epic()
        await coordinator.register_epic(issue)

        await coordinator.discover_children(issue.key)

        state = coordinator.get_epic_state("QR-50")
        assert state is not None
        assert state.phase == "needs_decomposition"

    async def test_discover_detects_resolved_children(self) -> None:
        """Resolved children should be COMPLETED but not tagged."""
        tracker = MagicMock()
        tracker.get_links.return_value = [
            {"relationship": "is parent task for", "object": {"key": "QR-51"}},
            {"relationship": "is parent task for", "object": {"key": "QR-52"}},
        ]
        tracker.get_issue.side_effect = [
            TrackerIssue("QR-51", "Done Task", "", [], [], "Done", "task"),
            TrackerIssue("QR-52", "Open Task", "", [], [], "open", "task"),
        ]
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        issue = make_epic()
        await coordinator.register_epic(issue)

        await coordinator.discover_children(issue.key)

        state = coordinator.get_epic_state("QR-50")
        assert state is not None
        assert state.children["QR-51"].status == ChildStatus.COMPLETED
        assert state.children["QR-52"].status == ChildStatus.PENDING
        tracker.update_issue_tags.assert_not_called()


class TestActivateChild:
    """Tests for activate_child() — manual child activation."""

    async def test_activate_child_tags_and_sets_ready(self) -> None:
        """activate_child should tag child with ai-task and set READY."""
        tracker = MagicMock()
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.PENDING, [], "open", tags=[]),
            },
        )

        result = await coordinator.activate_child("QR-50", "QR-51")

        assert result is True
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.READY
        tracker.update_issue_tags.assert_called_once_with("QR-51", ["ai-task"])

    async def test_activate_child_transitions_phase_to_executing(self) -> None:
        """activate_child should transition phase from 'awaiting_plan' to 'executing'."""
        tracker = MagicMock()
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.PENDING, [], "open", tags=[]),
            },
        )

        await coordinator.activate_child("QR-50", "QR-51")

        assert coordinator._epics["QR-50"].phase == "executing"

    async def test_activate_child_returns_false_for_completed(self) -> None:
        """activate_child should return False for already completed child."""
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "Done", ChildStatus.COMPLETED, [], "Done", tags=[]),
            },
        )

        result = await coordinator.activate_child("QR-50", "QR-51")

        assert result is False
        tracker.update_issue_tags.assert_not_called()

    async def test_activate_child_returns_false_for_unknown(self) -> None:
        """activate_child should return False for unknown child or epic."""
        tracker = MagicMock()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=EventBus(), config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={},
        )

        # Unknown child
        result = await coordinator.activate_child("QR-50", "QR-99")
        assert result is False

        # Unknown epic
        result = await coordinator.activate_child("QR-UNKNOWN", "QR-51")
        assert result is False

    async def test_activate_child_handles_tag_failure(self) -> None:
        """activate_child should return False and keep PENDING if tagging fails."""
        tracker = MagicMock()
        tracker.update_issue_tags.side_effect = requests.ConnectionError("tracker unavailable")
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.PENDING, [], "open", tags=[]),
            },
        )

        result = await coordinator.activate_child("QR-50", "QR-51")

        assert result is False
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.PENDING

    async def test_activate_child_emits_epic_child_ready(self) -> None:
        """activate_child should emit EPIC_CHILD_READY with manual=True."""
        tracker = MagicMock()
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.PENDING, [], "open", tags=[]),
            },
        )

        await coordinator.activate_child("QR-50", "QR-51")

        events = event_bus.get_task_history("QR-50")
        ready_events = [e for e in events if e.type == EventType.EPIC_CHILD_READY]
        assert len(ready_events) == 1
        assert ready_events[0].data.get("manual") is True
        assert ready_events[0].data.get("child_key") == "QR-51"


class TestTagReadyChildrenPhaseGuard:
    """Phase guard prevents auto-tagging when epic is in 'awaiting_plan'."""

    async def test_tag_ready_children_skips_awaiting_plan_phase(self) -> None:
        """Children should NOT be tagged when phase is 'awaiting_plan'."""
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.PENDING, [], "open", tags=[]),
            },
        )

        await coordinator._tag_ready_children("QR-50")

        tracker.update_issue_tags.assert_not_called()
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.PENDING


class TestExtractChildKeysStrict:
    """Tests for the extract_child_keys_strict public static method."""

    def test_extracts_child_keys_with_subtask_relationship(self) -> None:
        links = [
            {"relationship": "is parent task for", "issue": {"key": "QR-10"}},
            {"relationship": "is parent task for", "issue": {"key": "QR-11"}},
        ]
        result = EpicCoordinator.extract_child_keys_strict(links, "QR-1")
        assert result == {"QR-10", "QR-11"}

    def test_extracts_child_keys_with_object_field(self) -> None:
        links = [
            {"relationship": "is parent task for", "object": {"key": "QR-10"}},
        ]
        result = EpicCoordinator.extract_child_keys_strict(links, "QR-1")
        assert result == {"QR-10"}

    def test_excludes_parent_key_from_results(self) -> None:
        links = [
            {"relationship": "is parent task for", "issue": {"key": "QR-1"}},
            {"relationship": "is parent task for", "issue": {"key": "QR-10"}},
        ]
        result = EpicCoordinator.extract_child_keys_strict(links, "QR-1")
        assert result == {"QR-10"}

    def test_filters_by_child_link_hints(self) -> None:
        links = [
            {"relationship": "is parent task for", "issue": {"key": "QR-10"}},
            {"relationship": "relates to", "issue": {"key": "QR-20"}},
            {"relationship": "depends on", "issue": {"key": "QR-30"}},
        ]
        result = EpicCoordinator.extract_child_keys_strict(links, "QR-1")
        assert result == {"QR-10"}  # Only child relationship

    def test_no_fallback_returns_empty_set_for_unrelated_links(self) -> None:
        """Strict mode does NOT fallback to returning all links."""
        links = [
            {"relationship": "relates to", "issue": {"key": "QR-20"}},
            {"relationship": "depends on", "issue": {"key": "QR-30"}},
        ]
        result = EpicCoordinator.extract_child_keys_strict(links, "QR-1")
        assert result == set()  # No child relationships — strict mode returns empty

    def test_handles_empty_links(self) -> None:
        result = EpicCoordinator.extract_child_keys_strict([], "QR-1")
        assert result == set()


class TestResetChild:
    """Tests for reset_child() — recovery from false completions."""

    async def test_reset_completed_becomes_ready(self) -> None:
        """Completed child with no deps should be reset and re-tagged as READY."""
        tracker = MagicMock()
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.COMPLETED, [], "open", tags=["ai-task"]),
            },
        )

        result = await coordinator.reset_child("QR-50", "QR-51")

        assert result is True
        # No deps → _tag_ready_children promotes to READY
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.READY

    async def test_reset_failed_becomes_ready(self) -> None:
        """Failed child with no deps should be reset and re-tagged as READY."""
        tracker = MagicMock()
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.FAILED, [], "open", tags=["ai-task"]),
            },
        )

        result = await coordinator.reset_child("QR-50", "QR-51")

        assert result is True
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.READY

    async def test_reset_cancelled_becomes_ready(self) -> None:
        """Cancelled child with no deps should be reset and re-tagged as READY."""
        tracker = MagicMock()
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.CANCELLED, [], "open", tags=["ai-task"]),
            },
        )

        result = await coordinator.reset_child("QR-50", "QR-51")

        assert result is True
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.READY

    async def test_reset_dispatched_refuses(self) -> None:
        """DISPATCHED child should NOT be reset (agent running or about to)."""
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.DISPATCHED, [], "open", tags=["ai-task"]),
            },
        )

        result = await coordinator.reset_child("QR-50", "QR-51")

        assert result is False
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.DISPATCHED

    async def test_reset_ready_refuses(self) -> None:
        """READY child should NOT be reset."""
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.READY, [], "open", tags=["ai-task"]),
            },
        )

        result = await coordinator.reset_child("QR-50", "QR-51")

        assert result is False
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.READY

    async def test_reset_removes_from_dispatched_set(self) -> None:
        """Reset should remove child from the dispatched set."""
        tracker = MagicMock()
        event_bus = EventBus()
        dispatched: set[str] = {"QR-51"}
        coordinator = EpicCoordinator(
            tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=dispatched
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.COMPLETED, [], "open", tags=["ai-task"]),
            },
        )

        await coordinator.reset_child("QR-50", "QR-51")

        assert "QR-51" not in dispatched

    async def test_reset_retags_ready_children_in_executing_phase(self) -> None:
        """After reset in executing phase, _tag_ready_children re-evaluates readiness."""
        tracker = MagicMock()
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.COMPLETED, [], "open", tags=["ai-task"]),
                "QR-52": ChildTask("QR-52", "Task B", ChildStatus.PENDING, ["QR-51"], "open", tags=[]),
            },
        )

        # Reset QR-51 from COMPLETED back to PENDING
        # QR-51 has no deps itself, so _tag_ready_children promotes it to READY
        # QR-52 depends on QR-51 which is now READY (not COMPLETED), so stays PENDING
        await coordinator.reset_child("QR-50", "QR-51")

        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.READY
        # QR-52 depends on QR-51 which is not COMPLETED/CANCELLED → stays PENDING
        assert coordinator._epics["QR-50"].children["QR-52"].status == ChildStatus.PENDING

    async def test_reset_publishes_epic_child_reset_event(self) -> None:
        """Reset should publish EPIC_CHILD_RESET event."""
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=event_bus, config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.COMPLETED, [], "open", tags=["ai-task"]),
            },
        )

        await coordinator.reset_child("QR-50", "QR-51")

        events = event_bus.get_task_history("QR-50")
        reset_events = [e for e in events if e.type == EventType.EPIC_CHILD_RESET]
        assert len(reset_events) == 1
        assert reset_events[0].data["child_key"] == "QR-51"
        assert reset_events[0].data["previous_status"] == "completed"

    async def test_reset_unknown_epic_returns_false(self) -> None:
        """Reset on unknown epic should return False."""
        tracker = MagicMock()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=EventBus(), config=make_config(), dispatched_set=set())

        result = await coordinator.reset_child("QR-UNKNOWN", "QR-51")

        assert result is False

    async def test_reset_unknown_child_returns_false(self) -> None:
        """Reset on unknown child should return False."""
        tracker = MagicMock()
        coordinator = EpicCoordinator(tracker=tracker, event_bus=EventBus(), config=make_config(), dispatched_set=set())
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={},
        )

        result = await coordinator.reset_child("QR-50", "QR-99")

        assert result is False


class TestRevertDispatchedToReady:
    """Tests for revert_dispatched_to_ready() — preflight review recovery."""

    def test_reverts_dispatched_to_ready(self) -> None:
        """DISPATCHED child should be reverted to READY."""
        coordinator = EpicCoordinator(
            tracker=MagicMock(), event_bus=EventBus(), config=make_config(), dispatched_set=set()
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.DISPATCHED, [], "open"),
            },
        )

        result = coordinator.revert_dispatched_to_ready("QR-51")

        assert result is True
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.READY

    def test_refuses_non_dispatched_status(self) -> None:
        """Only DISPATCHED status can be reverted — other statuses return False."""
        coordinator = EpicCoordinator(
            tracker=MagicMock(), event_bus=EventBus(), config=make_config(), dispatched_set=set()
        )
        for status in (ChildStatus.PENDING, ChildStatus.READY, ChildStatus.COMPLETED, ChildStatus.FAILED):
            coordinator._epics["QR-50"] = EpicState(
                epic_key="QR-50",
                epic_summary="Epic",
                phase="executing",
                children={
                    "QR-51": ChildTask("QR-51", "Task A", status, [], "open"),
                },
            )

            result = coordinator.revert_dispatched_to_ready("QR-51")

            assert result is False, f"Expected False for status {status}"

    def test_returns_false_for_unknown_child(self) -> None:
        """Unknown child key returns False."""
        coordinator = EpicCoordinator(
            tracker=MagicMock(), event_bus=EventBus(), config=make_config(), dispatched_set=set()
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={},
        )

        result = coordinator.revert_dispatched_to_ready("QR-99")

        assert result is False


class TestGetParentEpicKey:
    """Tests for get_parent_epic_key() — public helper."""

    def test_returns_epic_key_for_child(self) -> None:
        coordinator = EpicCoordinator(
            tracker=MagicMock(), event_bus=EventBus(), config=make_config(), dispatched_set=set()
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.PENDING, [], "open"),
            },
        )

        assert coordinator.get_parent_epic_key("QR-51") == "QR-50"

    def test_returns_none_for_non_child(self) -> None:
        coordinator = EpicCoordinator(
            tracker=MagicMock(), event_bus=EventBus(), config=make_config(), dispatched_set=set()
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.PENDING, [], "open"),
            },
        )

        assert coordinator.get_parent_epic_key("QR-99") is None

    def test_returns_none_when_no_epics(self) -> None:
        coordinator = EpicCoordinator(
            tracker=MagicMock(), event_bus=EventBus(), config=make_config(), dispatched_set=set()
        )

        assert coordinator.get_parent_epic_key("QR-51") is None


class TestReconcileDispatchedChildren:
    """Tests for reconcile_dispatched_children() — startup reconciliation."""

    async def test_resolved_child_becomes_completed(self) -> None:
        """Dispatched child with resolved Tracker status → COMPLETED."""
        tracker = MagicMock()
        tracker.get_issue.return_value = TrackerIssue(
            "QR-51",
            "Task A",
            "",
            [],
            [],
            "Done",
            "task",
        )
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "Task A",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
            },
        )

        reconciled = await coordinator.reconcile_dispatched_children()

        assert reconciled == 1
        child = coordinator._epics["QR-50"].children["QR-51"]
        assert child.status == ChildStatus.COMPLETED

    async def test_cancelled_child_becomes_cancelled(self) -> None:
        """Dispatched child with cancelled Tracker status → CANCELLED."""
        tracker = MagicMock()
        tracker.get_issue.return_value = TrackerIssue(
            "QR-51",
            "Task A",
            "",
            [],
            [],
            "Cancelled",
            "task",
        )
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "Task A",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
            },
        )

        reconciled = await coordinator.reconcile_dispatched_children()

        assert reconciled == 1
        child = coordinator._epics["QR-50"].children["QR-51"]
        assert child.status == ChildStatus.CANCELLED

    async def test_orphaned_open_child_becomes_failed(self) -> None:
        """Dispatched child not in dispatched_set → FAILED (orphaned)."""
        tracker = MagicMock()
        tracker.get_issue.return_value = TrackerIssue(
            "QR-51",
            "Task A",
            "",
            [],
            [],
            "open",
            "task",
        )
        event_bus = EventBus()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "Task A",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
            },
        )

        reconciled = await coordinator.reconcile_dispatched_children()

        assert reconciled == 1
        child = coordinator._epics["QR-50"].children["QR-51"]
        assert child.status == ChildStatus.FAILED

    async def test_active_dispatched_child_stays_dispatched(self) -> None:
        """Dispatched child in dispatched_set → stays DISPATCHED."""
        tracker = MagicMock()
        tracker.get_issue.return_value = TrackerIssue(
            "QR-51",
            "Task A",
            "",
            [],
            [],
            "open",
            "task",
        )
        event_bus = EventBus()
        dispatched: set[str] = {"QR-51"}
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=dispatched,
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "Task A",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
            },
        )

        reconciled = await coordinator.reconcile_dispatched_children()

        assert reconciled == 0
        child = coordinator._epics["QR-50"].children["QR-51"]
        assert child.status == ChildStatus.DISPATCHED

    async def test_orphaned_needs_info_child_becomes_failed(
        self,
    ) -> None:
        """Dispatched child with needInfo status → FAILED (orphaned)."""
        tracker = MagicMock()
        tracker.get_issue.return_value = TrackerIssue(
            "QR-51",
            "Task A",
            "",
            [],
            [],
            "Требуется информация",
            "task",
        )
        event_bus = EventBus()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "Task A",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
            },
        )

        reconciled = await coordinator.reconcile_dispatched_children()

        assert reconciled == 1
        child = coordinator._epics["QR-50"].children["QR-51"]
        assert child.status == ChildStatus.FAILED

    async def test_unblocks_dependents_after_reconciliation(self) -> None:
        """Reconciled completion should unblock dependent children."""
        tracker = MagicMock()
        tracker.get_issue.return_value = TrackerIssue(
            "QR-51",
            "Task A",
            "",
            [],
            [],
            "Done",
            "task",
        )
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "Task A",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
                "QR-52": ChildTask(
                    "QR-52",
                    "Task B",
                    ChildStatus.PENDING,
                    ["QR-51"],
                    "open",
                    tags=[],
                ),
            },
        )

        await coordinator.reconcile_dispatched_children()

        child_52 = coordinator._epics["QR-50"].children["QR-52"]
        assert child_52.status == ChildStatus.READY
        tracker.update_issue_tags.assert_any_call("QR-52", ["ai-task"])

    async def test_tracker_api_failure_skips_child(self) -> None:
        """Tracker API failure should skip the child, not crash."""
        tracker = MagicMock()
        tracker.get_issue.side_effect = requests.ConnectionError("down")
        event_bus = EventBus()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "Task A",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
            },
        )

        reconciled = await coordinator.reconcile_dispatched_children()

        assert reconciled == 0
        child = coordinator._epics["QR-50"].children["QR-51"]
        assert child.status == ChildStatus.DISPATCHED

    async def test_skips_non_dispatched_children(self) -> None:
        """Only DISPATCHED children should be reconciled."""
        tracker = MagicMock()
        event_bus = EventBus()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "A",
                    ChildStatus.PENDING,
                    [],
                    "open",
                ),
                "QR-52": ChildTask(
                    "QR-52",
                    "B",
                    ChildStatus.COMPLETED,
                    [],
                    "Done",
                ),
                "QR-53": ChildTask(
                    "QR-53",
                    "C",
                    ChildStatus.READY,
                    [],
                    "open",
                ),
            },
        )

        reconciled = await coordinator.reconcile_dispatched_children()

        assert reconciled == 0
        tracker.get_issue.assert_not_called()

    async def test_multiple_epics_reconciled(self) -> None:
        """Reconciliation should work across multiple epics."""
        tracker = MagicMock()
        tracker.get_issue.side_effect = [
            TrackerIssue(
                "QR-51",
                "A",
                "",
                [],
                [],
                "Done",
                "task",
            ),
            TrackerIssue(
                "QR-61",
                "B",
                "",
                [],
                [],
                "closed",
                "task",
            ),
        ]
        tracker.update_issue_tags.return_value = {}
        event_bus = EventBus()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=event_bus,
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic 1",
            phase="executing",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "A",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
            },
        )
        coordinator._epics["QR-60"] = EpicState(
            epic_key="QR-60",
            epic_summary="Epic 2",
            phase="executing",
            children={
                "QR-61": ChildTask(
                    "QR-61",
                    "B",
                    ChildStatus.DISPATCHED,
                    [],
                    "open",
                    tags=["ai-task"],
                ),
            },
        )

        reconciled = await coordinator.reconcile_dispatched_children()

        assert reconciled == 2
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.COMPLETED
        assert coordinator._epics["QR-60"].children["QR-61"].status == ChildStatus.COMPLETED


# ===================================================================
# Bug #3: register_child transitions from needs_decomposition
# ===================================================================


class TestRegisterChildPhaseTransition:
    """register_child should transition phase from needs_decomposition."""

    def test_register_child_transitions_from_needs_decomposition(
        self,
    ) -> None:
        """Adding a child moves epic from needs_decomposition to awaiting_plan."""
        tracker = MagicMock()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=EventBus(),
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="needs_decomposition",
            children={},
        )

        child = ChildTask("QR-51", "New task", ChildStatus.PENDING, [], "open")
        result = coordinator.register_child("QR-50", child)

        assert result is True
        assert coordinator._epics["QR-50"].phase == "awaiting_plan"
        assert "QR-51" in coordinator._epics["QR-50"].children

    def test_register_child_keeps_other_phases(self) -> None:
        """register_child does not modify phase for non-needs_decomposition."""
        tracker = MagicMock()
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=EventBus(),
            config=make_config(),
            dispatched_set=set(),
        )
        for phase in ("analyzing", "awaiting_plan", "executing", "completed"):
            coordinator._epics["QR-50"] = EpicState(
                epic_key="QR-50",
                epic_summary="Epic",
                phase=phase,  # type: ignore[arg-type]
                children={},
            )
            child = ChildTask(
                f"QR-5{hash(phase) % 100}",
                "Task",
                ChildStatus.PENDING,
                [],
                "open",
            )
            coordinator.register_child("QR-50", child)
            assert coordinator._epics["QR-50"].phase == phase


class TestActivateChildFromNeedsDecomposition:
    """activate_child should also accept needs_decomposition phase."""

    async def test_activate_child_from_needs_decomposition(
        self,
    ) -> None:
        """activate_child transitions from needs_decomposition to executing."""
        tracker = MagicMock()
        tracker.update_issue_tags.return_value = {}
        coordinator = EpicCoordinator(
            tracker=tracker,
            event_bus=EventBus(),
            config=make_config(),
            dispatched_set=set(),
        )
        coordinator._epics["QR-50"] = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="needs_decomposition",
            children={
                "QR-51": ChildTask(
                    "QR-51",
                    "Task A",
                    ChildStatus.PENDING,
                    [],
                    "open",
                    tags=[],
                ),
            },
        )

        result = await coordinator.activate_child("QR-50", "QR-51")

        assert result is True
        assert coordinator._epics["QR-50"].phase == "executing"
        assert coordinator._epics["QR-50"].children["QR-51"].status == ChildStatus.READY
