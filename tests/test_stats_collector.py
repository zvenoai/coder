"""Tests for StatsCollector — EventBus → Storage bridge."""

from __future__ import annotations

import time

import pytest

from orchestrator.constants import EventType
from orchestrator.event_bus import Event, EventBus


@pytest.fixture
async def storage(tmp_path):
    """Create a SQLiteStorage with a temporary database."""
    from orchestrator.sqlite_storage import SQLiteStorage

    db = SQLiteStorage(str(tmp_path / "test_collector.db"))
    await db.open()
    yield db
    await db.close()


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
async def collector(storage, event_bus):
    from orchestrator.stats_collector import StatsCollector

    c = StatsCollector(storage, event_bus)
    await c.start()
    yield c
    await c.stop()


class TestTaskLifecycle:
    async def test_task_started_then_completed(self, collector, event_bus, storage) -> None:
        """TASK_STARTED + TASK_COMPLETED should produce a task_run record."""
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-1",
                data={"summary": "Test task"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-1",
                data={"cost": 0.5, "duration": 120.0, "model": "sonnet"},
                timestamp=now + 120,
            )
        )
        # Allow collector to process
        await collector.drain()

        rows = await storage.execute_readonly("SELECT * FROM task_runs WHERE task_key = 'QR-1'")
        assert len(rows) == 1
        assert rows[0]["success"] == 1
        assert rows[0]["cost_usd"] == 0.5
        assert rows[0]["model"] == "sonnet"

    async def test_task_started_then_failed(self, collector, event_bus, storage) -> None:
        """TASK_STARTED + TASK_FAILED should produce a failed task_run + error_log."""
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-2",
                data={"summary": "Failing task"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-2",
                data={"error": "timeout", "retryable": True, "cost": 0.3, "duration": 60.0, "model": "opus"},
                timestamp=now + 60,
            )
        )
        await collector.drain()

        runs = await storage.execute_readonly("SELECT * FROM task_runs WHERE task_key = 'QR-2'")
        assert len(runs) == 1
        assert runs[0]["success"] == 0
        assert runs[0]["model"] == "opus"
        assert runs[0]["cost_usd"] == 0.3

        errors = await storage.execute_readonly("SELECT * FROM error_log WHERE task_key = 'QR-2'")
        assert len(errors) == 1
        assert errors[0]["retryable"] == 1


class TestModelCorrelation:
    async def test_model_selected_event_used_as_fallback(self, collector, event_bus, storage) -> None:
        """MODEL_SELECTED should be used when TASK_COMPLETED has no model field."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-3", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.MODEL_SELECTED,
                task_key="QR-3",
                data={"model": "claude-opus-4-6"},
                timestamp=now + 1,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-3",
                data={"cost": 1.0, "duration": 30.0},
                timestamp=now + 30,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT model FROM task_runs WHERE task_key = 'QR-3'")
        assert rows[0]["model"] == "claude-opus-4-6"

    async def test_model_in_completed_overrides_selected(self, collector, event_bus, storage) -> None:
        """model in TASK_COMPLETED data should take priority over MODEL_SELECTED."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-4", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.MODEL_SELECTED,
                task_key="QR-4",
                data={"model": "old-model"},
                timestamp=now + 1,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-4",
                data={"cost": 1.0, "duration": 30.0, "model": "new-model"},
                timestamp=now + 30,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT model FROM task_runs WHERE task_key = 'QR-4'")
        assert rows[0]["model"] == "new-model"


class TestNoStartEvent:
    async def test_completed_without_start_uses_defaults(self, collector, event_bus, storage) -> None:
        """TASK_COMPLETED without prior TASK_STARTED should still record."""
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-5",
                data={"cost": 0.1, "duration": 10.0, "model": "sonnet"},
                timestamp=now,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT * FROM task_runs WHERE task_key = 'QR-5'")
        assert len(rows) == 1
        assert rows[0]["model"] == "sonnet"


class TestNoneCost:
    async def test_none_cost_defaults_to_zero(self, collector, event_bus, storage) -> None:
        """None cost in event data should default to 0.0."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-6", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-6",
                data={"cost": None, "duration": None, "model": "sonnet"},
                timestamp=now + 10,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly(
            "SELECT cost_usd, duration_seconds FROM task_runs WHERE task_key = 'QR-6'"
        )
        assert rows[0]["cost_usd"] == 0.0
        assert rows[0]["duration_seconds"] == 0.0


class TestPREvents:
    async def test_pr_tracked_records_task_run(self, collector, event_bus, storage) -> None:
        """PR_TRACKED must also record a successful task_run (bugbot: tasks with PRs were never counted)."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-PR", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.MODEL_SELECTED,
                task_key="QR-PR",
                data={"model": "sonnet"},
                timestamp=now + 1,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-PR",
                data={"pr_url": "https://github.com/org/repo/pull/99", "cost": 1.5, "duration": 200.0},
                timestamp=now + 200,
            )
        )
        await collector.drain()

        # Must have a task_run for the PR-creating task
        runs = await storage.execute_readonly("SELECT * FROM task_runs WHERE task_key = 'QR-PR'")
        assert len(runs) == 1
        assert runs[0]["success"] == 1
        assert runs[0]["pr_url"] == "https://github.com/org/repo/pull/99"
        assert runs[0]["cost_usd"] == 1.5
        assert runs[0]["model"] == "sonnet"

    async def test_pr_tracked_cleans_pending(self, collector, event_bus, storage) -> None:
        """PR_TRACKED must pop _pending_tasks to avoid memory leak."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-LEAK", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-LEAK",
                data={"pr_url": "https://github.com/org/repo/pull/100", "cost": 0.5, "duration": 60.0},
                timestamp=now + 60,
            )
        )
        await collector.drain()

        # _pending_tasks should not contain QR-LEAK anymore
        assert "QR-LEAK" not in collector._pending_tasks

    async def test_pr_tracked_records_lifecycle(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-7",
                data={"pr_url": "https://github.com/org/repo/pull/1"},
                timestamp=now,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT * FROM pr_lifecycle WHERE task_key = 'QR-7'")
        assert len(rows) == 1
        assert rows[0]["pr_url"] == "https://github.com/org/repo/pull/1"

    async def test_pr_merged_updates_lifecycle(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-8",
                data={"pr_url": "https://github.com/org/repo/pull/2"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.PR_MERGED,
                task_key="QR-8",
                data={"pr_url": "https://github.com/org/repo/pull/2"},
                timestamp=now + 3600,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT merged_at FROM pr_lifecycle WHERE task_key = 'QR-8'")
        assert rows[0]["merged_at"] is not None

    async def test_pr_merged_without_pr_url_logs_error(self, collector, event_bus, storage, caplog) -> None:
        """PR_MERGED event without pr_url should log error and not update database."""
        import logging

        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-NOURL",
                data={"pr_url": "https://github.com/org/repo/pull/10"},
                timestamp=now,
            )
        )
        await collector.drain()

        # Publish PR_MERGED without pr_url
        caplog.set_level(logging.ERROR)
        await event_bus.publish(
            Event(
                type=EventType.PR_MERGED,
                task_key="QR-NOURL",
                data={},  # Missing pr_url
                timestamp=now + 3600,
            )
        )
        await collector.drain()

        # Verify error was logged
        assert any(
            "missing pr_url" in record.message.lower() for record in caplog.records if record.levelname == "ERROR"
        )

        # Verify PR was NOT marked as merged
        rows = await storage.execute_readonly("SELECT merged_at FROM pr_lifecycle WHERE task_key = 'QR-NOURL'")
        assert rows[0]["merged_at"] is None

    async def test_review_sent_increments(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-9",
                data={"pr_url": "https://github.com/org/repo/pull/3"},
                timestamp=now,
            )
        )
        await event_bus.publish(Event(type=EventType.REVIEW_SENT, task_key="QR-9", data={}, timestamp=now + 100))
        await collector.drain()

        rows = await storage.execute_readonly("SELECT review_iterations FROM pr_lifecycle WHERE task_key = 'QR-9'")
        assert rows[0]["review_iterations"] == 1

    async def test_pipeline_failed_increments(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-10",
                data={"pr_url": "https://github.com/org/repo/pull/4"},
                timestamp=now,
            )
        )
        await event_bus.publish(Event(type=EventType.PIPELINE_FAILED, task_key="QR-10", data={}, timestamp=now + 100))
        await collector.drain()

        rows = await storage.execute_readonly("SELECT ci_failures FROM pr_lifecycle WHERE task_key = 'QR-10'")
        assert rows[0]["ci_failures"] == 1


class TestSupervisorEvents:
    async def test_supervisor_completed(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.SUPERVISOR_STARTED,
                task_key="supervisor",
                data={"triggers": ["QR-1", "QR-2"]},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.SUPERVISOR_COMPLETED,
                task_key="supervisor",
                data={"cost": 0.8, "tasks_created": ["QR-10"]},
                timestamp=now + 45,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT * FROM supervisor_runs")
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == 0.8
        assert rows[0]["success"] == 1

    async def test_supervisor_failed(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.SUPERVISOR_STARTED,
                task_key="supervisor",
                data={"triggers": ["QR-1"]},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.SUPERVISOR_FAILED,
                task_key="supervisor",
                data={"error": "SDK crashed"},
                timestamp=now + 10,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT * FROM supervisor_runs")
        assert len(rows) == 1
        assert rows[0]["success"] == 0

    async def test_supervisor_failed_records_cost_from_event(self, collector, event_bus, storage) -> None:
        """SUPERVISOR_FAILED should read cost from event data, not hardcode 0.0."""
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.SUPERVISOR_STARTED,
                task_key="supervisor",
                data={"triggers": ["QR-1"]},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.SUPERVISOR_FAILED,
                task_key="supervisor",
                data={"error": "SDK crashed", "cost": 0.42},
                timestamp=now + 10,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT * FROM supervisor_runs")
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert rows[0]["cost_usd"] == 0.42


class TestNeedsInfoTracking:
    async def test_needs_info_flag_set_on_completed(self, collector, event_bus, storage) -> None:
        """Task that goes through needs-info should have needs_info=True in the record."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-NI", data={}, timestamp=now))
        await event_bus.publish(
            Event(type=EventType.NEEDS_INFO, task_key="QR-NI", data={"text": "What version?"}, timestamp=now + 10)
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-NI",
                data={"cost": 0.5, "duration": 60.0, "model": "sonnet"},
                timestamp=now + 60,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT needs_info FROM task_runs WHERE task_key = 'QR-NI'")
        assert rows[0]["needs_info"] == 1

    async def test_no_needs_info_flag_when_not_triggered(self, collector, event_bus, storage) -> None:
        """Task without needs-info should have needs_info=False."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-NONI", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-NONI",
                data={"cost": 0.3, "duration": 30.0, "model": "sonnet"},
                timestamp=now + 30,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT needs_info FROM task_runs WHERE task_key = 'QR-NONI'")
        assert rows[0]["needs_info"] == 0

    async def test_needs_info_flag_set_on_failed(self, collector, event_bus, storage) -> None:
        """Failed task that went through needs-info should also have needs_info=True."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-NIF", data={}, timestamp=now))
        await event_bus.publish(
            Event(type=EventType.NEEDS_INFO, task_key="QR-NIF", data={"text": "Which branch?"}, timestamp=now + 5)
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-NIF",
                data={"error": "timeout", "retryable": True, "cost": 0.2, "duration": 20.0, "model": "opus"},
                timestamp=now + 20,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT needs_info FROM task_runs WHERE task_key = 'QR-NIF'")
        assert rows[0]["needs_info"] == 1


class TestModelFallbackToUnknown:
    async def test_model_defaults_to_unknown(self, collector, event_bus, storage) -> None:
        """When neither MODEL_SELECTED nor model in data, model should be 'unknown'."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-UNK", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-UNK",
                data={"cost": 0.1, "duration": 10.0},
                timestamp=now + 10,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT model FROM task_runs WHERE task_key = 'QR-UNK'")
        assert rows[0]["model"] == "unknown"


class TestErrorClassificationUsesRecovery:
    async def test_error_category_uses_recovery_classify(self, collector, event_bus, storage) -> None:
        """Error classification should use recovery.classify_error, not a local copy."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-ERR", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-ERR",
                data={"error": "connection refused network error 502", "retryable": True},
                timestamp=now + 5,
            )
        )
        await collector.drain()

        # recovery.classify_error classifies "connection" + "502" as TRANSIENT
        rows = await storage.execute_readonly("SELECT error_category FROM task_runs WHERE task_key = 'QR-ERR'")
        assert rows[0]["error_category"] == "transient"

        errors = await storage.execute_readonly("SELECT error_category FROM error_log WHERE task_key = 'QR-ERR'")
        assert errors[0]["error_category"] == "transient"


class TestProposalEvents:
    async def test_task_proposed_records_proposal(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_PROPOSED,
                task_key="QR-P1",
                data={"proposal_id": "prop-1", "summary": "Add caching", "category": "performance"},
                timestamp=now,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT * FROM proposals WHERE proposal_id = 'prop-1'")
        assert len(rows) == 1
        assert rows[0]["source_task_key"] == "QR-P1"
        assert rows[0]["summary"] == "Add caching"
        assert rows[0]["status"] == "pending"

    async def test_proposal_approved_resolves(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_PROPOSED,
                task_key="QR-P2",
                data={"proposal_id": "prop-2", "summary": "Refactor X", "category": "quality"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.PROPOSAL_APPROVED,
                task_key="QR-P2",
                data={"proposal_id": "prop-2"},
                timestamp=now + 60,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT status, resolved_at FROM proposals WHERE proposal_id = 'prop-2'")
        assert rows[0]["status"] == "approved"
        assert rows[0]["resolved_at"] is not None

    async def test_proposal_rejected_resolves(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_PROPOSED,
                task_key="QR-P3",
                data={"proposal_id": "prop-3", "summary": "Bad idea", "category": "other"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.PROPOSAL_REJECTED,
                task_key="QR-P3",
                data={"proposal_id": "prop-3"},
                timestamp=now + 30,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT status FROM proposals WHERE proposal_id = 'prop-3'")
        assert rows[0]["status"] == "rejected"


class TestLifecycleInvariants:
    """Every task dispatch path must produce exactly one task_run record."""

    async def test_success_with_pr_records_one_task_run(self, collector, event_bus, storage) -> None:
        """TASK_STARTED → MODEL_SELECTED → PR_TRACKED → exactly 1 task_run."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-INV1", data={}, timestamp=now))
        await event_bus.publish(
            Event(type=EventType.MODEL_SELECTED, task_key="QR-INV1", data={"model": "sonnet"}, timestamp=now + 1)
        )
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-INV1",
                data={"pr_url": "https://github.com/org/repo/pull/1", "cost": 1.0, "duration": 120.0},
                timestamp=now + 120,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT COUNT(*) as cnt FROM task_runs WHERE task_key = 'QR-INV1'")
        assert rows[0]["cnt"] == 1
        # Also verify pr_lifecycle recorded
        pr_rows = await storage.execute_readonly("SELECT COUNT(*) as cnt FROM pr_lifecycle WHERE task_key = 'QR-INV1'")
        assert pr_rows[0]["cnt"] == 1

    async def test_success_without_pr_records_one_task_run(self, collector, event_bus, storage) -> None:
        """TASK_STARTED → TASK_COMPLETED → exactly 1 task_run."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-INV2", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-INV2",
                data={"cost": 0.5, "duration": 60.0, "model": "sonnet"},
                timestamp=now + 60,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT COUNT(*) as cnt FROM task_runs WHERE task_key = 'QR-INV2'")
        assert rows[0]["cnt"] == 1

    async def test_failure_records_one_task_run_and_one_error(self, collector, event_bus, storage) -> None:
        """TASK_STARTED → TASK_FAILED → exactly 1 task_run + 1 error_log."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-INV3", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-INV3",
                data={"error": "timeout", "retryable": True, "cost": 0.3, "duration": 20.0},
                timestamp=now + 20,
            )
        )
        await collector.drain()

        runs = await storage.execute_readonly("SELECT COUNT(*) as cnt FROM task_runs WHERE task_key = 'QR-INV3'")
        assert runs[0]["cnt"] == 1
        errors = await storage.execute_readonly("SELECT COUNT(*) as cnt FROM error_log WHERE task_key = 'QR-INV3'")
        assert errors[0]["cnt"] == 1

    async def test_needs_info_then_pr_records_one_task_run(self, collector, event_bus, storage) -> None:
        """TASK_STARTED → NEEDS_INFO → PR_TRACKED → exactly 1 task_run."""
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-INV4", data={}, timestamp=now))
        await event_bus.publish(
            Event(type=EventType.NEEDS_INFO, task_key="QR-INV4", data={"text": "Which version?"}, timestamp=now + 5)
        )
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-INV4",
                data={"pr_url": "https://github.com/org/repo/pull/2", "cost": 0.8, "duration": 90.0},
                timestamp=now + 90,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT COUNT(*) as cnt FROM task_runs WHERE task_key = 'QR-INV4'")
        assert rows[0]["cnt"] == 1
        # Verify needs_info flag was set
        detail = await storage.execute_readonly("SELECT needs_info FROM task_runs WHERE task_key = 'QR-INV4'")
        assert detail[0]["needs_info"] == 1

    async def test_no_double_counting_pr_and_completed(self, collector, event_bus, storage) -> None:
        """PR_TRACKED + TASK_COMPLETED for same key → 2 task_runs (2 separate runs)."""
        now = time.time()
        # First run — creates PR
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-INV5", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-INV5",
                data={"pr_url": "https://github.com/org/repo/pull/3", "cost": 1.0, "duration": 100.0},
                timestamp=now + 100,
            )
        )
        # Second run — retry completes without PR
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-INV5", data={}, timestamp=now + 200))
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-INV5",
                data={"cost": 0.5, "duration": 50.0, "model": "sonnet"},
                timestamp=now + 250,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT COUNT(*) as cnt FROM task_runs WHERE task_key = 'QR-INV5'")
        assert rows[0]["cnt"] == 2


class TestIgnoredEvents:
    async def test_agent_output_ignored(self, collector, event_bus, storage) -> None:
        """Events like AGENT_OUTPUT should be silently ignored."""
        await event_bus.publish(
            Event(
                type=EventType.AGENT_OUTPUT,
                task_key="QR-11",
                data={"text": "hello"},
            )
        )
        await collector.drain()

        # No task_runs should be created
        rows = await storage.execute_readonly("SELECT COUNT(*) as cnt FROM task_runs")
        assert rows[0]["cnt"] == 0


class TestSessionIdPassthrough:
    """session_id from event data must be persisted in task_runs."""

    async def test_pr_tracked_persists_session_id(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-SID1", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-SID1",
                data={
                    "pr_url": "https://github.com/org/repo/pull/99",
                    "cost": 1.0,
                    "duration": 100.0,
                    "session_id": "sess-abc",
                },
                timestamp=now + 100,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT session_id FROM task_runs WHERE task_key = 'QR-SID1'")
        assert rows[0]["session_id"] == "sess-abc"

    async def test_task_completed_persists_session_id(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-SID2", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-SID2",
                data={"cost": 0.5, "duration": 60.0, "model": "sonnet", "session_id": "sess-def"},
                timestamp=now + 60,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT session_id FROM task_runs WHERE task_key = 'QR-SID2'")
        assert rows[0]["session_id"] == "sess-def"

    async def test_task_failed_persists_session_id(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-SID3", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-SID3",
                data={"error": "timeout", "retryable": True, "session_id": "sess-ghi"},
                timestamp=now + 10,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT session_id FROM task_runs WHERE task_key = 'QR-SID3'")
        assert rows[0]["session_id"] == "sess-ghi"

    async def test_no_session_id_stores_null(self, collector, event_bus, storage) -> None:
        now = time.time()
        await event_bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-SID4", data={}, timestamp=now))
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-SID4",
                data={"cost": 0.1, "duration": 5.0, "model": "sonnet"},
                timestamp=now + 5,
            )
        )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT session_id FROM task_runs WHERE task_key = 'QR-SID4'")
        assert rows[0]["session_id"] is None


class TestDuplicateProposal:
    async def test_duplicate_task_proposed_does_not_crash(self, collector, event_bus, storage) -> None:
        """Two TASK_PROPOSED events with the same proposal_id must not crash (upsert)."""
        now = time.time()
        for i in range(2):
            await event_bus.publish(
                Event(
                    type=EventType.TASK_PROPOSED,
                    task_key="QR-DUP",
                    data={"proposal_id": "dup-prop", "summary": f"Proposal v{i}", "category": "tooling"},
                    timestamp=now + i,
                )
            )
        await collector.drain()

        rows = await storage.execute_readonly("SELECT * FROM proposals WHERE proposal_id = 'dup-prop'")
        assert len(rows) == 1
        # upsert should keep the latest summary
        assert rows[0]["summary"] == "Proposal v1"


class TestCancelledClassification:
    """cancelled=True in TASK_FAILED data → error_category='cancelled'."""

    async def test_cancelled_flag_overrides_classify_error(
        self,
        collector,
        event_bus,
        storage,
    ) -> None:
        """cancelled=True → 'cancelled', ignoring error text."""
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-CAN1",
                data={},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-CAN1",
                data={
                    "error": "rate limit exceeded",
                    "cancelled": True,
                    "cost": 0.1,
                    "duration": 5.0,
                },
                timestamp=now + 5,
            )
        )
        await collector.drain()

        runs = await storage.execute_readonly("SELECT error_category FROM task_runs WHERE task_key = 'QR-CAN1'")
        assert runs[0]["error_category"] == "cancelled"

        errors = await storage.execute_readonly("SELECT error_category FROM error_log WHERE task_key = 'QR-CAN1'")
        assert errors[0]["error_category"] == "cancelled"

    async def test_without_cancelled_uses_classify_error(
        self,
        collector,
        event_bus,
        storage,
    ) -> None:
        """No cancelled flag → classify_error determines category."""
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-CAN2",
                data={},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-CAN2",
                data={
                    "error": "rate limit exceeded",
                    "cost": 0.1,
                    "duration": 5.0,
                },
                timestamp=now + 5,
            )
        )
        await collector.drain()

        runs = await storage.execute_readonly("SELECT error_category FROM task_runs WHERE task_key = 'QR-CAN2'")
        # "rate limit" → classified as rate_limit
        assert runs[0]["error_category"] == "rate_limit"

    async def test_cancelled_false_uses_classify_error(
        self,
        collector,
        event_bus,
        storage,
    ) -> None:
        """cancelled=False → classify_error determines category."""
        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-CAN3",
                data={},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-CAN3",
                data={
                    "error": "unknown error xyz",
                    "cancelled": False,
                    "cost": 0.1,
                    "duration": 5.0,
                },
                timestamp=now + 5,
            )
        )
        await collector.drain()

        runs = await storage.execute_readonly("SELECT error_category FROM task_runs WHERE task_key = 'QR-CAN3'")
        assert runs[0]["error_category"] == "permanent"
