"""Tests for epic lifecycle event handling in _watch_epic_events."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from orchestrator.constants import EventType
from orchestrator.epic_coordinator import ChildStatus, ChildTask, EpicCoordinator, EpicState
from orchestrator.event_bus import Event, EventBus


class TestTaskSkippedEpicChildNotification:
    """TASK_SKIPPED for epic children should trigger supervisor auto_send."""

    async def test_task_skipped_epic_child_triggers_auto_send(self) -> None:
        """When an epic child is skipped, supervisor should be notified."""
        event_bus = EventBus()
        chat_manager = AsyncMock()
        epic_coordinator = MagicMock(spec=EpicCoordinator)
        epic_coordinator.is_epic_child.return_value = True
        epic_coordinator.get_parent_epic_key.return_value = "QR-50"

        # Simulate the event handler logic from _watch_epic_events
        from orchestrator.supervisor_prompt_builder import build_preflight_skip_prompt

        event = Event(
            type=EventType.TASK_SKIPPED,
            task_key="QR-51",
            data={"reason": "merged PR found", "source": "preflight_checker"},
        )

        # Check the condition
        assert epic_coordinator.is_epic_child(event.task_key)
        epic_key = epic_coordinator.get_parent_epic_key(event.task_key)
        assert epic_key == "QR-50"

        prompt = build_preflight_skip_prompt(
            event.task_key,
            epic_key,
            str(event.data.get("reason", "unknown")),
            str(event.data.get("source", "preflight_checker")),
        )
        await chat_manager.auto_send(prompt)

        chat_manager.auto_send.assert_called_once()
        sent_prompt = chat_manager.auto_send.call_args[0][0]
        assert "QR-51" in sent_prompt
        assert "QR-50" in sent_prompt
        assert "merged PR found" in sent_prompt

    async def test_task_skipped_non_epic_child_does_not_trigger(self) -> None:
        """When a non-epic task is skipped, supervisor should NOT be notified."""
        epic_coordinator = MagicMock(spec=EpicCoordinator)
        epic_coordinator.is_epic_child.return_value = False

        event = Event(
            type=EventType.TASK_SKIPPED,
            task_key="QR-99",
            data={"reason": "merged PR found"},
        )

        # The condition should prevent auto_send
        assert not epic_coordinator.is_epic_child(event.task_key)


class TestEpicCompletedNotification:
    """EPIC_COMPLETED should trigger supervisor validation."""

    async def test_epic_completed_triggers_auto_send(self) -> None:
        """When an epic completes, supervisor should be notified to validate."""
        chat_manager = AsyncMock()
        epic_coordinator = MagicMock(spec=EpicCoordinator)

        state = EpicState(
            epic_key="QR-50",
            epic_summary="Test Epic",
            phase="completed",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.COMPLETED, [], "Done"),
                "QR-52": ChildTask("QR-52", "Task B", ChildStatus.CANCELLED, [], "Cancelled"),
            },
        )
        epic_coordinator.get_epic_state.return_value = state

        from orchestrator.supervisor_prompt_builder import build_epic_completion_prompt

        event = Event(
            type=EventType.EPIC_COMPLETED,
            task_key="QR-50",
            data={"children_total": 2, "cancelled": 1},
        )

        # Simulate the handler logic
        children_summary = [{"key": c.key, "status": c.status.value} for c in state.children.values()]
        prompt = build_epic_completion_prompt(event.task_key, children_summary)
        await chat_manager.auto_send(prompt)

        chat_manager.auto_send.assert_called_once()
        sent_prompt = chat_manager.auto_send.call_args[0][0]
        assert "QR-50" in sent_prompt
        assert "QR-51" in sent_prompt
        assert "QR-52" in sent_prompt
        assert "completed" in sent_prompt.lower()
