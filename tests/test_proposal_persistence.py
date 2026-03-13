"""Tests for ProposalManager SQLite persistence."""

from __future__ import annotations

import pytest

from orchestrator.stats_models import ProposalRecord


@pytest.fixture
async def storage(tmp_path):
    """Create a SQLiteStorage with a temporary database."""
    from orchestrator.sqlite_storage import SQLiteStorage

    db = SQLiteStorage(str(tmp_path / "test_proposal.db"))
    await db.open()
    yield db
    await db.close()


class TestProposalStorageCRUD:
    """Low-level storage methods for proposals with extended fields."""

    async def test_persist_all_fields_round_trip(self, storage) -> None:
        """All proposal fields including description, component, tracker_issue_key persist."""
        record = ProposalRecord(
            proposal_id="prop-abc",
            source_task_key="QR-50",
            summary="Add caching",
            category="performance",
            status="pending",
            created_at=1_000_000.0,
            description="We should add Redis caching for API responses",
            component="backend",
        )
        await storage.upsert_proposal(record)

        loaded = await storage.load_proposals()
        assert len(loaded) == 1
        p = loaded[0]
        assert p["proposal_id"] == "prop-abc"
        assert p["summary"] == "Add caching"
        assert p["description"] == "We should add Redis caching for API responses"
        assert p["component"] == "backend"
        assert p["tracker_issue_key"] is None
        assert p["status"] == "pending"

    async def test_load_pending_proposals(self, storage) -> None:
        """Can filter pending proposals from loaded data."""
        for i, status in enumerate(["pending", "approved", "pending", "rejected"]):
            record = ProposalRecord(
                proposal_id=f"prop-{i}",
                source_task_key=f"QR-{i}",
                summary=f"Proposal {i}",
                category="tooling",
                status=status,
                created_at=1_000_000.0 + i,
            )
            record.description = f"desc-{i}"
            record.component = "backend"
            await storage.upsert_proposal(record)

        loaded = await storage.load_proposals()
        pending = [p for p in loaded if p["status"] == "pending"]
        assert len(pending) == 2

    async def test_status_changes_persisted(self, storage) -> None:
        """Updating a proposal status via upsert changes the stored value."""
        record = ProposalRecord(
            proposal_id="prop-status",
            source_task_key="QR-60",
            summary="Test",
            category="testing",
            status="pending",
            created_at=1_000_000.0,
        )
        record.description = "desc"
        record.component = "backend"
        await storage.upsert_proposal(record)

        # Update status to approved with tracker key
        record_updated = ProposalRecord(
            proposal_id="prop-status",
            source_task_key="QR-60",
            summary="Test",
            category="testing",
            status="approved",
            created_at=1_000_000.0,
            resolved_at=1_000_100.0,
            description="desc",
            component="backend",
            tracker_issue_key="QR-99",
        )
        await storage.upsert_proposal(record_updated)

        loaded = await storage.load_proposals()
        assert len(loaded) == 1
        assert loaded[0]["status"] == "approved"
        assert loaded[0]["tracker_issue_key"] == "QR-99"
        assert loaded[0]["resolved_at"] == 1_000_100.0


class TestProposalManagerPersistence:
    """Integration: ProposalManager + SQLite storage."""

    async def test_process_proposals_persists(self, storage) -> None:
        """process_proposals writes to storage."""
        from unittest.mock import AsyncMock, MagicMock

        from orchestrator.proposal_manager import ProposalManager

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()
        config = MagicMock()
        tracker = MagicMock()

        mgr = ProposalManager(tracker=tracker, event_bus=event_bus, config=config, storage=storage)

        await mgr.process_proposals(
            "QR-70",
            [
                {
                    "summary": "Add tests for X",
                    "description": "We need unit tests",
                    "component": "backend",
                    "category": "testing",
                }
            ],
        )
        await mgr.drain_background_tasks()

        loaded = await storage.load_proposals()
        assert len(loaded) == 1
        assert loaded[0]["summary"] == "Add tests for X"
        assert loaded[0]["description"] == "We need unit tests"
        assert loaded[0]["component"] == "backend"

    async def test_load_restores_proposals(self, storage) -> None:
        """load() restores in-memory state from DB."""
        from unittest.mock import MagicMock

        from orchestrator.proposal_manager import ProposalManager

        # Seed DB
        record = ProposalRecord(
            proposal_id="prop-load",
            source_task_key="QR-80",
            summary="Cached proposal",
            category="infrastructure",
            status="pending",
            created_at=1_000_000.0,
        )
        record.description = "From previous run"
        record.component = "devops"
        await storage.upsert_proposal(record)

        event_bus = MagicMock()
        config = MagicMock()
        tracker = MagicMock()

        mgr = ProposalManager(tracker=tracker, event_bus=event_bus, config=config, storage=storage)
        await mgr.load()

        all_proposals = mgr.get_all()
        assert "prop-load" in all_proposals
        p = all_proposals["prop-load"]
        assert p.summary == "Cached proposal"
        assert p.description == "From previous run"
        assert p.component == "devops"
        assert p.status == "pending"

    async def test_disable_storage_continues_in_memory(self, storage) -> None:
        """After disable_storage(), manager still works in-memory."""
        from unittest.mock import AsyncMock, MagicMock

        from orchestrator.proposal_manager import ProposalManager

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()
        config = MagicMock()
        tracker = MagicMock()

        mgr = ProposalManager(tracker=tracker, event_bus=event_bus, config=config, storage=storage)
        mgr.disable_storage()

        await mgr.process_proposals(
            "QR-90", [{"summary": "Test", "description": "d", "component": "backend", "category": "tooling"}]
        )

        assert len(mgr.get_all()) == 1
        # Nothing in DB
        loaded = await storage.load_proposals()
        assert len(loaded) == 0
