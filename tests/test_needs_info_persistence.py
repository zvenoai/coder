"""Tests for NeedsInfoMonitor SQLite persistence."""

from __future__ import annotations

import pytest

from orchestrator.stats_models import NeedsInfoTrackingRecord


@pytest.fixture
async def storage(tmp_path):
    """Create a SQLiteStorage with a temporary database."""
    from orchestrator.sqlite_storage import SQLiteStorage

    db = SQLiteStorage(str(tmp_path / "test_needs_info.db"))
    await db.open()
    yield db
    await db.close()


class TestNeedsInfoTrackingStorageCRUD:
    """Low-level storage methods for needs-info tracking."""

    async def test_last_seen_comment_survives_restart(self, storage) -> None:
        """last_seen_comment_id persists through write → load cycle."""
        record = NeedsInfoTrackingRecord(
            issue_key="QR-20",
            last_seen_comment_id=42,
            issue_summary="Need clarification",
            tracked_at=1_000_000.0,
        )
        await storage.upsert_needs_info_tracking(record)

        loaded = await storage.load_needs_info_tracking()
        assert len(loaded) == 1
        assert loaded[0].issue_key == "QR-20"
        assert loaded[0].last_seen_comment_id == 42
        assert loaded[0].issue_summary == "Need clarification"

    async def test_upsert_updates_comment_id(self, storage) -> None:
        """Upserting same issue_key updates last_seen_comment_id."""
        record1 = NeedsInfoTrackingRecord(
            issue_key="QR-21",
            last_seen_comment_id=10,
            issue_summary="Task",
            tracked_at=1.0,
        )
        await storage.upsert_needs_info_tracking(record1)

        record2 = NeedsInfoTrackingRecord(
            issue_key="QR-21",
            last_seen_comment_id=25,
            issue_summary="Task",
            tracked_at=2.0,
        )
        await storage.upsert_needs_info_tracking(record2)

        loaded = await storage.load_needs_info_tracking()
        assert len(loaded) == 1
        assert loaded[0].last_seen_comment_id == 25

    async def test_cleanup_on_completion(self, storage) -> None:
        """Deleting removes tracking data when task completes."""
        record = NeedsInfoTrackingRecord(
            issue_key="QR-22",
            last_seen_comment_id=5,
            issue_summary="Done",
            tracked_at=1.0,
        )
        await storage.upsert_needs_info_tracking(record)
        await storage.delete_needs_info_tracking("QR-22")

        loaded = await storage.load_needs_info_tracking()
        assert len(loaded) == 0

    async def test_load_empty_db(self, storage) -> None:
        """Loading from empty table returns empty list."""
        loaded = await storage.load_needs_info_tracking()
        assert loaded == []

    async def test_multiple_issues_tracked(self, storage) -> None:
        """Multiple issues tracked independently."""
        for key, comment_id in [("QR-30", 10), ("QR-31", 20), ("QR-32", 30)]:
            await storage.upsert_needs_info_tracking(
                NeedsInfoTrackingRecord(
                    issue_key=key,
                    last_seen_comment_id=comment_id,
                    issue_summary=f"Task {key}",
                    tracked_at=1.0,
                )
            )

        loaded = await storage.load_needs_info_tracking()
        assert len(loaded) == 3
        by_key = {r.issue_key: r for r in loaded}
        assert by_key["QR-30"].last_seen_comment_id == 10
        assert by_key["QR-31"].last_seen_comment_id == 20
        assert by_key["QR-32"].last_seen_comment_id == 30


class TestNeedsInfoTrackingSessionId:
    """session_id field roundtrip through SQLite."""

    async def test_needs_info_tracking_session_id_roundtrip(self, storage) -> None:
        """session_id persists through write → load cycle."""
        record = NeedsInfoTrackingRecord(
            issue_key="QR-NI-SES-1",
            last_seen_comment_id=50,
            issue_summary="Resume NI test",
            tracked_at=1_000_000.0,
            session_id="ses-ni-abc",
        )
        await storage.upsert_needs_info_tracking(record)

        loaded = await storage.load_needs_info_tracking()
        assert len(loaded) == 1
        assert loaded[0].session_id == "ses-ni-abc"

    async def test_needs_info_tracking_session_id_null(self, storage) -> None:
        """session_id defaults to None for records without it."""
        record = NeedsInfoTrackingRecord(
            issue_key="QR-NI-SES-2",
            last_seen_comment_id=10,
            issue_summary="No session",
            tracked_at=1.0,
        )
        await storage.upsert_needs_info_tracking(record)

        loaded = await storage.load_needs_info_tracking()
        assert len(loaded) == 1
        assert loaded[0].session_id is None

    async def test_needs_info_tracking_session_id_updated_on_upsert(self, storage) -> None:
        """session_id updates when upserting existing record."""
        record = NeedsInfoTrackingRecord(
            issue_key="QR-NI-SES-3",
            last_seen_comment_id=5,
            issue_summary="Update",
            tracked_at=1.0,
            session_id="ses-old",
        )
        await storage.upsert_needs_info_tracking(record)

        record.session_id = "ses-new"
        record.tracked_at = 2.0
        await storage.upsert_needs_info_tracking(record)

        loaded = await storage.load_needs_info_tracking()
        assert len(loaded) == 1
        assert loaded[0].session_id == "ses-new"
