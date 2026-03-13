"""Tests for EpicCoordinator SQLite persistence."""

from __future__ import annotations

import pytest

from orchestrator.stats_models import EpicChildRecord, EpicStateRecord


@pytest.fixture
async def storage(tmp_path):
    """Create a SQLiteStorage with a temporary database."""
    from orchestrator.sqlite_storage import SQLiteStorage

    db = SQLiteStorage(str(tmp_path / "test_epic.db"))
    await db.open()
    yield db
    await db.close()


class TestEpicStorageCRUD:
    """Low-level storage methods for epic states and children."""

    async def test_persist_and_load_epic_round_trip(self, storage) -> None:
        """Epic state survives write → close → reopen → load."""
        record = EpicStateRecord(
            epic_key="QR-100",
            epic_summary="Build auth system",
            phase="executing",
            created_at=1_000_000.0,
        )
        await storage.upsert_epic_state(record)

        states = await storage.load_epic_states()
        assert len(states) == 1
        s = states[0]
        assert s.epic_key == "QR-100"
        assert s.epic_summary == "Build auth system"
        assert s.phase == "executing"
        assert s.created_at == 1_000_000.0

    async def test_persist_children_with_json_fields(self, storage) -> None:
        """Children with depends_on and tags survive round-trip through JSON."""
        await storage.upsert_epic_state(
            EpicStateRecord(epic_key="QR-100", epic_summary="Epic", phase="executing", created_at=1.0)
        )
        child = EpicChildRecord(
            child_key="QR-101",
            summary="Setup DB",
            status="pending",
            depends_on=["QR-102", "QR-103"],
            tracker_status="open",
            last_comment_id=42,
            tags=["ai-task", "backend"],
        )
        await storage.upsert_epic_child("QR-100", child)

        children = await storage.load_epic_children("QR-100")
        assert len(children) == 1
        c = children[0]
        assert c.child_key == "QR-101"
        assert c.depends_on == ["QR-102", "QR-103"]
        assert c.tags == ["ai-task", "backend"]
        assert c.last_comment_id == 42
        assert c.tracker_status == "open"

    async def test_child_status_transitions_persisted(self, storage) -> None:
        """Updating a child's status via upsert changes the stored value."""
        await storage.upsert_epic_state(
            EpicStateRecord(epic_key="QR-100", epic_summary="Epic", phase="executing", created_at=1.0)
        )
        child = EpicChildRecord(
            child_key="QR-101",
            summary="Task",
            status="pending",
            depends_on=[],
            tracker_status="open",
            last_comment_id=0,
            tags=[],
        )
        await storage.upsert_epic_child("QR-100", child)

        # Update status
        child_updated = EpicChildRecord(
            child_key="QR-101",
            summary="Task",
            status="completed",
            depends_on=[],
            tracker_status="closed",
            last_comment_id=5,
            tags=["ai-task"],
        )
        await storage.upsert_epic_child("QR-100", child_updated)

        children = await storage.load_epic_children("QR-100")
        assert len(children) == 1
        assert children[0].status == "completed"
        assert children[0].tracker_status == "closed"
        assert children[0].last_comment_id == 5

    async def test_delete_completed_epic(self, storage) -> None:
        """Deleting an epic removes both state and all children."""
        await storage.upsert_epic_state(
            EpicStateRecord(epic_key="QR-100", epic_summary="Epic", phase="completed", created_at=1.0)
        )
        await storage.upsert_epic_child(
            "QR-100",
            EpicChildRecord(
                child_key="QR-101",
                summary="T",
                status="completed",
                depends_on=[],
                tracker_status="",
                last_comment_id=0,
                tags=[],
            ),
        )

        await storage.delete_epic("QR-100")

        states = await storage.load_epic_states()
        assert len(states) == 0
        children = await storage.load_epic_children("QR-100")
        assert len(children) == 0

    async def test_load_empty_db(self, storage) -> None:
        """Loading from empty tables returns empty lists."""
        states = await storage.load_epic_states()
        assert states == []
        children = await storage.load_epic_children("QR-999")
        assert children == []

    async def test_upsert_epic_state_updates(self, storage) -> None:
        """Upserting with same key updates fields."""
        await storage.upsert_epic_state(
            EpicStateRecord(epic_key="QR-100", epic_summary="Old", phase="analyzing", created_at=1.0)
        )
        await storage.upsert_epic_state(
            EpicStateRecord(epic_key="QR-100", epic_summary="New", phase="executing", created_at=1.0)
        )

        states = await storage.load_epic_states()
        assert len(states) == 1
        assert states[0].epic_summary == "New"
        assert states[0].phase == "executing"


class TestEpicCoordinatorPersistence:
    """Integration: EpicCoordinator + SQLite storage."""

    async def test_register_epic_persists(self, storage) -> None:
        """register_epic writes to storage."""
        from unittest.mock import AsyncMock, MagicMock

        from orchestrator.epic_coordinator import EpicCoordinator

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()
        config = MagicMock()
        dispatched: set[str] = set()

        tracker = MagicMock()
        coord = EpicCoordinator(
            tracker=tracker, event_bus=event_bus, config=config, dispatched_set=dispatched, storage=storage
        )

        issue = MagicMock()
        issue.key = "QR-200"
        issue.summary = "Test Epic"

        await coord.register_epic(issue)
        # Drain background tasks
        await coord.drain_background_tasks()

        states = await storage.load_epic_states()
        assert len(states) == 1
        assert states[0].epic_key == "QR-200"
        assert states[0].phase == "analyzing"

    async def test_disable_storage_continues_in_memory(self, storage) -> None:
        """After disable_storage(), coordinator still works in-memory."""
        from unittest.mock import AsyncMock, MagicMock

        from orchestrator.epic_coordinator import EpicCoordinator

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()
        config = MagicMock()
        dispatched: set[str] = set()
        tracker = MagicMock()

        coord = EpicCoordinator(
            tracker=tracker, event_bus=event_bus, config=config, dispatched_set=dispatched, storage=storage
        )

        coord.disable_storage()

        issue = MagicMock()
        issue.key = "QR-300"
        issue.summary = "No storage"

        await coord.register_epic(issue)
        # Should still work in-memory
        assert "QR-300" in coord._epics

        # Nothing in DB
        states = await storage.load_epic_states()
        assert len(states) == 0

    async def test_load_restores_epics(self, storage) -> None:
        """load() restores in-memory state from DB."""
        from unittest.mock import MagicMock

        from orchestrator.epic_coordinator import ChildStatus, EpicCoordinator

        # Seed DB
        await storage.upsert_epic_state(
            EpicStateRecord(epic_key="QR-400", epic_summary="Persisted Epic", phase="executing", created_at=1.0)
        )
        await storage.upsert_epic_child(
            "QR-400",
            EpicChildRecord(
                child_key="QR-401",
                summary="Child 1",
                status="ready",
                depends_on=["QR-402"],
                tracker_status="open",
                last_comment_id=10,
                tags=["ai-task"],
            ),
        )

        event_bus = MagicMock()
        config = MagicMock()
        dispatched: set[str] = set()
        tracker = MagicMock()

        coord = EpicCoordinator(
            tracker=tracker, event_bus=event_bus, config=config, dispatched_set=dispatched, storage=storage
        )
        await coord.load()

        assert "QR-400" in coord._epics
        assert "QR-400" in dispatched  # Epic blocked from re-dispatch
        state = coord._epics["QR-400"]
        assert state.phase == "executing"
        assert state.epic_summary == "Persisted Epic"
        assert "QR-401" in state.children
        child = state.children["QR-401"]
        assert child.status == ChildStatus.READY
        assert child.depends_on == ["QR-402"]
        assert child.tags == ["ai-task"]

    async def test_task_completed_persists(self, storage) -> None:
        """on_task_completed updates child status in storage."""
        from unittest.mock import AsyncMock, MagicMock

        from orchestrator.epic_coordinator import ChildStatus, ChildTask, EpicCoordinator, EpicState

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()
        config = MagicMock()
        config.tracker_tag = "ai-task"
        dispatched: set[str] = set()
        tracker = MagicMock()

        coord = EpicCoordinator(
            tracker=tracker, event_bus=event_bus, config=config, dispatched_set=dispatched, storage=storage
        )

        # Set up in-memory state
        coord._epics["QR-500"] = EpicState(
            epic_key="QR-500",
            epic_summary="Epic",
            children={
                "QR-501": ChildTask(
                    key="QR-501",
                    summary="T1",
                    status=ChildStatus.DISPATCHED,
                    depends_on=[],
                    tracker_status="open",
                    tags=[],
                ),
                "QR-502": ChildTask(
                    key="QR-502",
                    summary="T2",
                    status=ChildStatus.PENDING,
                    depends_on=["QR-501"],
                    tracker_status="open",
                    tags=[],
                ),
            },
            phase="executing",
            created_at=1.0,
        )

        await coord.on_task_completed("QR-501")
        await coord.drain_background_tasks()

        children = await storage.load_epic_children("QR-500")
        child_map = {c.child_key: c for c in children}
        assert child_map["QR-501"].status == "completed"
