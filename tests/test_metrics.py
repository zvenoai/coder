"""Tests for Prometheus metrics collection and exposition."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.constants import EventType
from orchestrator.event_bus import Event, EventBus


class TestMetricsRegistry:
    """Unit tests for the MetricsRegistry primitives."""

    def test_counter_increment(self) -> None:
        """Counter starts at 0 and increments correctly."""
        from orchestrator.metrics import MetricsRegistry

        registry = MetricsRegistry()
        counter = registry.counter("test_counter", "Test counter help")

        assert counter.value() == 0
        counter.inc()
        assert counter.value() == 1
        counter.inc(5)
        assert counter.value() == 6

    def test_counter_increment_with_labels(self) -> None:
        """Counter with labels tracks values independently."""
        from orchestrator.metrics import MetricsRegistry

        registry = MetricsRegistry()
        counter = registry.counter(
            "test_counter_labels",
            "Test counter with labels",
            label_names=("status",),
        )

        counter.labels(status="completed").inc()
        counter.labels(status="completed").inc()
        counter.labels(status="failed").inc()

        assert counter.labels(status="completed").value() == 2
        assert counter.labels(status="failed").value() == 1

    def test_gauge_set(self) -> None:
        """Gauge can be set to any value."""
        from orchestrator.metrics import MetricsRegistry

        registry = MetricsRegistry()
        gauge = registry.gauge("test_gauge", "Test gauge help")

        assert gauge.value() == 0
        gauge.set(42)
        assert gauge.value() == 42
        gauge.set(10)
        assert gauge.value() == 10
        gauge.inc()
        assert gauge.value() == 11
        gauge.dec()
        assert gauge.value() == 10

    def test_histogram_observe(self) -> None:
        """Histogram records observations correctly."""
        from orchestrator.metrics import MetricsRegistry

        registry = MetricsRegistry()
        histogram = registry.histogram(
            "test_histogram",
            "Test histogram help",
            buckets=(1.0, 5.0, 10.0, float("inf")),
        )

        histogram.observe(0.5)
        histogram.observe(3.0)
        histogram.observe(7.0)
        histogram.observe(15.0)

        # Should produce _sum, _count, and bucket counts
        text = registry.render()
        assert "test_histogram_sum 25.5" in text  # 0.5 + 3.0 + 7.0 + 15.0
        assert "test_histogram_count 4" in text
        assert 'test_histogram_bucket{le="1.0"} 1' in text
        assert 'test_histogram_bucket{le="5.0"} 2' in text
        assert 'test_histogram_bucket{le="10.0"} 3' in text
        assert 'test_histogram_bucket{le="+Inf"} 4' in text

    def test_render_prometheus_text(self) -> None:
        """Full registry renders valid Prometheus text format."""
        from orchestrator.metrics import MetricsRegistry

        registry = MetricsRegistry()
        counter = registry.counter("requests_total", "Total HTTP requests")
        gauge = registry.gauge("active_connections", "Active connections")

        counter.inc(100)
        gauge.set(42)

        text = registry.render()

        # Check HELP lines
        assert "# HELP requests_total Total HTTP requests" in text
        assert "# HELP active_connections Active connections" in text

        # Check TYPE lines
        assert "# TYPE requests_total counter" in text
        assert "# TYPE active_connections gauge" in text

        # Check values
        assert "requests_total 100" in text or "requests_total 100.0" in text
        assert "active_connections 42" in text or "active_connections 42.0" in text

    def test_labeled_counter_does_not_emit_unlabeled_zero(self) -> None:
        """Labeled counter should not emit unlabeled zero when empty."""
        from orchestrator.metrics import MetricsRegistry

        registry = MetricsRegistry()
        # Create a counter with labels but don't increment it
        counter = registry.counter(
            "test_labeled_counter",
            "Test counter with labels",
            label_names=("status",),
        )

        text = registry.render()

        # Should not contain unlabeled zero (violates Prometheus format)
        assert "test_labeled_counter 0" not in text
        # Should contain HELP and TYPE
        assert "# HELP test_labeled_counter" in text
        assert "# TYPE test_labeled_counter counter" in text


class TestMetricsEndpoint:
    """Integration tests for the /metrics HTTP endpoint."""

    def test_metrics_endpoint_returns_prometheus_format(self) -> None:
        """GET /metrics returns valid Prometheus text format."""
        from orchestrator.web import app, configure

        state = {
            "dispatched": ["QR-1"],
            "active_tasks": ["agent-QR-1", "agent-QR-2"],
            "tracked_prs": {"QR-1": {}},
            "epics": {"QR-EPIC-1": {}},
        }
        configure(EventBus(), lambda: state)

        client = TestClient(app)
        resp = client.get("/metrics")

        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

        text = resp.text
        # Should contain metric definitions
        assert "# HELP coder_agents_running" in text
        assert "# TYPE coder_agents_running gauge" in text
        # Should reflect current state
        assert "coder_agents_running 2" in text  # 2 active tasks
        assert "coder_prs_tracked 1" in text  # 1 tracked PR
        assert "coder_epics_active 1" in text  # 1 epic

    def test_agents_running_gauge_updates_on_state_change(self) -> None:
        """Agents running gauge reflects current state from get_state()."""
        from orchestrator.web import app, configure

        state = {
            "dispatched": [],
            "active_tasks": ["agent-QR-1", "agent-QR-2", "agent-QR-3"],
            "tracked_prs": {},
            "epics": {},
        }
        configure(EventBus(), lambda: state)

        client = TestClient(app)
        resp = client.get("/metrics")

        assert resp.status_code == 200
        assert "coder_agents_running 3" in resp.text

    def test_tasks_total_counter_increments_on_completion(self) -> None:
        """Tasks total counter increments when tasks complete or fail."""
        from orchestrator.metrics import TASKS_TOTAL
        from orchestrator.web import app, configure

        # Read initial values
        initial_completed = TASKS_TOTAL.labels(status="completed").value()
        initial_failed = TASKS_TOTAL.labels(status="failed").value()

        event_bus = EventBus()
        state: dict[str, Any] = {
            "dispatched": [],
            "active_tasks": [],
            "tracked_prs": {},
            "epics": {},
        }
        configure(event_bus, lambda: state)

        # Manually increment counters (simulating what stats_collector would do)
        TASKS_TOTAL.labels(status="completed").inc()
        TASKS_TOTAL.labels(status="completed").inc()
        TASKS_TOTAL.labels(status="failed").inc()

        client = TestClient(app)
        resp = client.get("/metrics")

        assert resp.status_code == 200
        # Assert on increments, not absolute values
        assert TASKS_TOTAL.labels(status="completed").value() == initial_completed + 2
        assert TASKS_TOTAL.labels(status="failed").value() == initial_failed + 1

    def test_metrics_endpoint_accessible_without_auth(self) -> None:
        """Metrics endpoint does not require authentication."""
        from orchestrator.web import app, configure

        state: dict[str, Any] = {
            "dispatched": [],
            "active_tasks": [],
            "tracked_prs": {},
            "epics": {},
        }
        configure(EventBus(), lambda: state)

        client = TestClient(app)
        resp = client.get("/metrics")

        # Should succeed without any authentication headers
        assert resp.status_code == 200
        assert "# HELP" in resp.text


class TestStatsCollectorMetrics:
    """Test that StatsCollector updates metrics on events."""

    @pytest.fixture
    async def storage(self, tmp_path):
        """Create a SQLiteStorage with a temporary database."""
        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(str(tmp_path / "test_metrics_collector.db"))
        await db.open()
        yield db
        await db.close()

    @pytest.fixture
    def event_bus(self):
        return EventBus()

    @pytest.fixture
    async def collector(self, storage, event_bus):
        from orchestrator.stats_collector import StatsCollector

        c = StatsCollector(storage, event_bus)
        await c.start()
        yield c
        await c.stop()

    async def test_task_completed_increments_counter(self, collector, event_bus) -> None:
        """TASK_COMPLETED event increments tasks_total counter."""
        from orchestrator.metrics import TASKS_TOTAL

        initial = TASKS_TOTAL.labels(status="completed").value()

        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-TEST-1",
                data={"summary": "Test task"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-TEST-1",
                data={
                    "duration": 120.5,
                    "cost": 1.25,
                },
                timestamp=now + 120.5,
            )
        )

        # Wait for collector to process
        await collector.drain()

        assert TASKS_TOTAL.labels(status="completed").value() == initial + 1

    async def test_task_failed_increments_counter(self, collector, event_bus) -> None:
        """TASK_FAILED event increments tasks_total counter with status=failed."""
        from orchestrator.metrics import TASKS_TOTAL

        initial = TASKS_TOTAL.labels(status="failed").value()

        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-TEST-2",
                data={"summary": "Test task"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key="QR-TEST-2",
                data={
                    "error": "Something went wrong",
                    "duration": 60.0,
                    "cost": 0.5,
                },
                timestamp=now + 60.0,
            )
        )

        # Wait for collector to process
        await collector.drain()

        assert TASKS_TOTAL.labels(status="failed").value() == initial + 1

    async def test_compaction_triggered_increments_counter(self, collector, event_bus) -> None:
        """COMPACTION_TRIGGERED event increments compaction_total counter."""
        from orchestrator.metrics import COMPACTION_TOTAL

        initial = COMPACTION_TOTAL.value()

        await event_bus.publish(
            Event(
                type=EventType.COMPACTION_TRIGGERED,
                task_key="",
                data={"reason": "max cycles reached"},
                timestamp=time.time(),
            )
        )

        # Wait for collector to process
        await collector.drain()

        assert COMPACTION_TOTAL.value() == initial + 1

    async def test_task_completed_records_histogram_observations(self, collector, event_bus) -> None:
        """TASK_COMPLETED event records duration and cost in histograms."""
        from orchestrator.metrics import TASK_COST, TASK_DURATION

        initial_duration_count = TASK_DURATION._count
        initial_cost_count = TASK_COST._count

        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-TEST-HIST-1",
                data={"summary": "Histogram test task"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key="QR-TEST-HIST-1",
                data={
                    "duration": 150.0,
                    "cost": 2.5,
                },
                timestamp=now + 150.0,
            )
        )

        # Wait for collector to process
        await collector.drain()

        # Verify histograms received observations
        assert TASK_DURATION._count == initial_duration_count + 1
        assert TASK_COST._count == initial_cost_count + 1
        # Verify sum updated correctly
        assert TASK_DURATION._sum >= 150.0
        assert TASK_COST._sum >= 2.5

    async def test_pr_tracked_updates_metrics(self, collector, event_bus) -> None:
        """PR_TRACKED event increments tasks_total counter and records histograms."""
        from orchestrator.metrics import TASK_COST, TASK_DURATION, TASKS_TOTAL

        initial_completed = TASKS_TOTAL.labels(status="completed").value()
        initial_duration_count = TASK_DURATION._count
        initial_cost_count = TASK_COST._count

        now = time.time()
        await event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_key="QR-TEST-PR-1",
                data={"summary": "PR task"},
                timestamp=now,
            )
        )
        await event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key="QR-TEST-PR-1",
                data={
                    "pr_url": "https://github.com/test/repo/pull/1",
                    "duration": 180.0,
                    "cost": 3.0,
                },
                timestamp=now + 180.0,
            )
        )

        # Wait for collector to process
        await collector.drain()

        # Verify counter incremented (PR_TRACKED is a success)
        assert TASKS_TOTAL.labels(status="completed").value() == initial_completed + 1
        # Verify histograms received observations
        assert TASK_DURATION._count == initial_duration_count + 1
        assert TASK_COST._count == initial_cost_count + 1
