"""Tests for epic auto-decomposition (Feature 3)."""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestrator.constants import EventType
from orchestrator.epic_coordinator import (
    ChildStatus,
    ChildTask,
    EpicCoordinator,
)
from orchestrator.event_bus import Event, EventBus


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.tracker_tag = "ai-task"
    cfg.tracker_queue = "QR"
    return cfg


def _make_coordinator(
    event_bus: EventBus | None = None,
) -> EpicCoordinator:
    tracker = MagicMock()
    bus = event_bus or EventBus()
    cfg = _make_config()
    dispatched: set[str] = set()
    return EpicCoordinator(
        tracker=tracker,
        event_bus=bus,
        config=cfg,
        dispatched_set=dispatched,
    )


class TestDiscoverChildrenDecomposition:
    """Test discover_children emits NEEDS_DECOMPOSITION when no children."""

    async def test_no_children_triggers_decomposition(
        self,
    ) -> None:
        bus = EventBus()
        coord = _make_coordinator(bus)
        coord._tracker.get_links = MagicMock(return_value=[])

        # Register the epic first
        issue = MagicMock()
        issue.key = "QR-100"
        issue.summary = "Big feature"
        await coord.register_epic(issue)

        queue = bus.subscribe_global()
        await coord.discover_children("QR-100")

        # Drain events to find EPIC_NEEDS_DECOMPOSITION
        events: list[Event] = []
        while not queue.empty():
            events.append(queue.get_nowait())

        decomp_events = [e for e in events if e.type == EventType.EPIC_NEEDS_DECOMPOSITION]
        assert len(decomp_events) == 1
        assert decomp_events[0].task_key == "QR-100"

        state = coord.get_epic_state("QR-100")
        assert state is not None
        assert state.phase == "needs_decomposition"

    async def test_with_children_goes_to_awaiting_plan(
        self,
    ) -> None:
        bus = EventBus()
        coord = _make_coordinator(bus)

        child_issue = MagicMock()
        child_issue.key = "QR-101"
        child_issue.summary = "Subtask 1"
        child_issue.status = "open"
        child_issue.tags = []

        coord._tracker.get_links = MagicMock(
            return_value=[
                {
                    "relationship": "is parent task for",
                    "object": {"key": "QR-101"},
                }
            ]
        )
        coord._tracker.get_issue = MagicMock(return_value=child_issue)

        issue = MagicMock()
        issue.key = "QR-100"
        issue.summary = "Big feature"
        await coord.register_epic(issue)

        await coord.discover_children("QR-100")

        state = coord.get_epic_state("QR-100")
        assert state is not None
        assert state.phase == "awaiting_plan"
        assert "QR-101" in state.children


class TestRegisterChild:
    """Test register_child adds child to epic state."""

    async def test_register_child_success(self) -> None:
        coord = _make_coordinator()
        issue = MagicMock()
        issue.key = "QR-200"
        issue.summary = "Epic"
        await coord.register_epic(issue)

        child = ChildTask(
            key="QR-201",
            summary="New subtask",
            status=ChildStatus.PENDING,
            depends_on=[],
            tracker_status="open",
        )
        ok = coord.register_child("QR-200", child)
        assert ok is True

        state = coord.get_epic_state("QR-200")
        assert state is not None
        assert "QR-201" in state.children
        assert state.children["QR-201"].summary == "New subtask"

    def test_register_child_unknown_epic(self) -> None:
        coord = _make_coordinator()
        child = ChildTask(
            key="QR-301",
            summary="Orphan",
            status=ChildStatus.PENDING,
            depends_on=[],
            tracker_status="open",
        )
        ok = coord.register_child("QR-999", child)
        assert ok is False


class TestRediscoverChildren:
    """Test rediscover_children merges new children."""

    async def test_discovers_new_children(self) -> None:
        coord = _make_coordinator()
        issue = MagicMock()
        issue.key = "QR-400"
        issue.summary = "Epic"
        await coord.register_epic(issue)

        # Pre-populate with one child
        existing = ChildTask(
            key="QR-401",
            summary="Existing",
            status=ChildStatus.COMPLETED,
            depends_on=[],
            tracker_status="closed",
        )
        coord.register_child("QR-400", existing)

        # Mock fetch_children to return existing + new
        child1 = MagicMock()
        child1.key = "QR-401"
        child1.summary = "Existing"
        child1.status = "closed"
        child1.tags = []

        child2 = MagicMock()
        child2.key = "QR-402"
        child2.summary = "New child"
        child2.status = "open"
        child2.tags = []

        coord.fetch_children = MagicMock(  # type: ignore[assignment]
            return_value=[child1, child2]
        )

        new_count = await coord.rediscover_children("QR-400")
        assert new_count == 1

        state = coord.get_epic_state("QR-400")
        assert state is not None
        assert "QR-402" in state.children
        # Existing child keeps its COMPLETED status
        assert state.children["QR-401"].status == ChildStatus.COMPLETED

    async def test_no_new_children(self) -> None:
        coord = _make_coordinator()
        issue = MagicMock()
        issue.key = "QR-500"
        issue.summary = "Epic"
        await coord.register_epic(issue)

        coord.fetch_children = MagicMock(  # type: ignore[assignment]
            return_value=[]
        )
        new_count = await coord.rediscover_children("QR-500")
        assert new_count == 0

    async def test_unknown_epic(self) -> None:
        coord = _make_coordinator()
        coord.fetch_children = MagicMock(  # type: ignore[assignment]
            return_value=[]
        )
        new_count = await coord.rediscover_children("QR-999")
        assert new_count == 0


class TestDecomposePromptBuilder:
    """Test the decomposition prompt builder."""

    def test_build_epic_decompose_prompt(self) -> None:
        from orchestrator.supervisor_prompt_builder import (
            build_epic_decompose_prompt,
        )

        prompt = build_epic_decompose_prompt(
            "QR-100",
            "Big feature",
            "Implement auth system with OAuth2 support.",
        )
        assert "QR-100" in prompt
        assert "Big feature" in prompt
        assert "no children" in prompt
        assert "epic_create_child" in prompt
        assert "epic_set_plan" in prompt
        assert "OAuth2" in prompt

    def test_truncates_long_description(self) -> None:
        from orchestrator.supervisor_prompt_builder import (
            build_epic_decompose_prompt,
        )

        long_desc = "x" * 3000
        prompt = build_epic_decompose_prompt("QR-100", "Epic", long_desc)
        assert "truncated" in prompt
