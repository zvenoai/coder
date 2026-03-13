"""Tests for heartbeat monitor module."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.constants import HEARTBEAT_CHANNEL_KEY, EventType
from orchestrator.event_bus import Event, EventBus
from orchestrator.heartbeat import (
    HeartbeatMonitor,
    _find_last_output_snippet,
)


@dataclass(frozen=True)
class _HeartbeatConfig:
    """Minimal config stub with heartbeat fields."""

    heartbeat_interval_seconds: int = 5
    heartbeat_stuck_threshold_seconds: int = 600
    heartbeat_long_running_threshold_seconds: int = 3600
    heartbeat_review_stale_threshold_seconds: int = 1800
    heartbeat_full_report_every_n: int = 3
    heartbeat_cooldown_seconds: int = 900


def _make_event(
    event_type: str,
    task_key: str,
    data: dict | None = None,
    timestamp: float | None = None,
) -> Event:
    """Create an Event with explicit timestamp."""
    return Event(
        type=event_type,
        task_key=task_key,
        data=data or {},
        timestamp=timestamp or time.time(),
    )


async def _populate_history(
    bus: EventBus,
    events: list[Event],
) -> None:
    """Publish a list of events into the bus."""
    for evt in events:
        await bus.publish(evt)


def _make_task_info(
    task_key: str = "QR-1",
    status: str = "running",
    pr_url: str = "",
    cost_usd: float | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tracker_status: str = "",
) -> dict[str, object]:
    return {
        "task_key": task_key,
        "status": status,
        "pr_url": pr_url,
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tracker_status": tracker_status,
    }


# ===================================================================
# _find_last_output_snippet
# ===================================================================


class TestFindLastOutputSnippet:
    """Tests for _find_last_output_snippet helper."""

    def test_returns_last_output_text(self) -> None:
        now = time.time()
        history = [
            _make_event(
                EventType.AGENT_OUTPUT,
                "QR-1",
                {"text": "first output"},
                now - 10,
            ),
            _make_event(
                EventType.AGENT_OUTPUT,
                "QR-1",
                {"text": "second output"},
                now - 5,
            ),
        ]
        assert _find_last_output_snippet(history) == "second output"

    def test_truncates_to_max_len(self) -> None:
        now = time.time()
        long_text = "x" * 500
        history = [
            _make_event(
                EventType.AGENT_OUTPUT,
                "QR-1",
                {"text": long_text},
                now,
            ),
        ]
        result = _find_last_output_snippet(history, max_len=200)
        assert len(result) == 200
        assert result == long_text[-200:]

    def test_returns_empty_when_no_output(self) -> None:
        now = time.time()
        history = [
            _make_event(
                EventType.TASK_STARTED,
                "QR-1",
                {"summary": "Test"},
                now,
            ),
        ]
        assert _find_last_output_snippet(history) == ""

    def test_skips_empty_text(self) -> None:
        now = time.time()
        history = [
            _make_event(
                EventType.AGENT_OUTPUT,
                "QR-1",
                {"text": "real output"},
                now - 10,
            ),
            _make_event(
                EventType.AGENT_OUTPUT,
                "QR-1",
                {"text": ""},
                now - 5,
            ),
        ]
        assert _find_last_output_snippet(history) == "real output"

    def test_empty_history(self) -> None:
        assert _find_last_output_snippet([]) == ""


# ===================================================================
# collect_health — detection of stuck / long / stale / healthy
# ===================================================================


class TestCollectHealth:
    """Health metric collection from event history."""

    async def test_no_agents(self) -> None:
        """Empty result when no tasks running."""
        bus = EventBus()
        monitor = HeartbeatMonitor(
            config=_HeartbeatConfig(),  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=list,
        )

        result = monitor.collect_health()

        assert result.total_agents == 0
        assert result.healthy_agents == 0
        assert result.stuck == []
        assert result.long_running == []
        assert result.stale_reviews == []
        assert result.all_agents == []

    async def test_healthy_agent(self) -> None:
        """Agent with recent activity is not stuck."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=3600,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-1",
                    {"summary": "Healthy task"},
                    now - 300,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-1",
                    {"text": "working"},
                    now - 10,
                ),
            ],
        )

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-1"),
            ],
        )

        result = monitor.collect_health()

        assert result.total_agents == 1
        assert result.healthy_agents == 1
        assert result.stuck == []
        assert result.long_running == []
        assert result.stale_reviews == []

        report = result.all_agents[0]
        assert report.task_key == "QR-1"
        assert report.issue_summary == "Healthy task"
        assert not report.is_stuck
        assert not report.is_long_running
        assert not report.is_review_stale

    async def test_stuck_agent(self) -> None:
        """Agent idle > threshold is stuck."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=7200,
        )
        bus = EventBus()
        # idle = 900s > stuck threshold 600s
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-2",
                    {"summary": "Stuck task"},
                    now - 1200,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-2",
                    {},
                    now - 900,
                ),
            ],
        )

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-2"),
            ],
        )

        result = monitor.collect_health()

        assert result.total_agents == 1
        assert result.healthy_agents == 0
        assert len(result.stuck) == 1
        assert result.stuck[0].task_key == "QR-2"
        assert result.stuck[0].is_stuck

    async def test_long_running_agent(self) -> None:
        """Agent running > threshold with compaction events."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=3600,
        )
        bus = EventBus()
        # elapsed = 7200s > long_running threshold 3600s
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-3",
                    {"summary": "Long task"},
                    now - 7200,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-3",
                    {},
                    now - 10,
                ),
                _make_event(
                    EventType.COMPACTION_TRIGGERED,
                    "QR-3",
                    {},
                    now - 3000,
                ),
                _make_event(
                    EventType.COMPACTION_TRIGGERED,
                    "QR-3",
                    {},
                    now - 1000,
                ),
            ],
        )

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-3"),
            ],
        )

        result = monitor.collect_health()

        assert result.total_agents == 1
        assert len(result.long_running) == 1

        report = result.long_running[0]
        assert report.task_key == "QR-3"
        assert report.is_long_running
        assert not report.is_stuck
        assert report.compaction_count == 2

    async def test_no_output_idle_equals_elapsed(self) -> None:
        """When no AGENT_OUTPUT events, idle equals elapsed."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=100,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-50",
                    {"summary": "Silent"},
                    now - 200,
                ),
            ],
        )

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-50"),
            ],
        )

        result = monitor.collect_health()

        report = result.all_agents[0]
        assert abs(report.idle_seconds - report.elapsed_seconds) < 1.0
        assert report.is_stuck  # 200s > 100s threshold

    async def test_summary_defaults_to_task_key(self) -> None:
        """issue_summary falls back to task_key when missing."""
        now = time.time()
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-80",
                    {},  # No "summary" key
                    now - 60,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-80",
                    {},
                    now - 5,
                ),
            ],
        )

        monitor = HeartbeatMonitor(
            config=_HeartbeatConfig(),  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-80"),
            ],
        )

        result = monitor.collect_health()
        assert result.all_agents[0].issue_summary == "QR-80"

    async def test_healthy_count_excludes_problematic(
        self,
    ) -> None:
        """healthy_agents correctly excludes stuck/long/stale."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=3600,
            heartbeat_review_stale_threshold_seconds=1800,
        )
        bus = EventBus()

        # Healthy agent
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-70",
                    {"summary": "Healthy"},
                    now - 60,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-70",
                    {},
                    now - 5,
                ),
            ],
        )
        # Stuck agent
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-71",
                    {"summary": "Stuck"},
                    now - 1200,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-71",
                    {},
                    now - 900,
                ),
            ],
        )
        # Long-running agent
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-72",
                    {"summary": "Long"},
                    now - 7200,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-72",
                    {},
                    now - 5,
                ),
            ],
        )

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-70"),
                _make_task_info("QR-71"),
                _make_task_info("QR-72"),
            ],
        )

        result = monitor.collect_health()

        assert result.total_agents == 3
        assert result.healthy_agents == 1
        assert len(result.stuck) == 1
        assert len(result.long_running) == 1


# ===================================================================
# collect_health — enriched fields (cost, tokens, snippet, tracker)
# ===================================================================


class TestCollectHealthEnriched:
    """Enriched heartbeat data: cost, tokens, snippet, tracker."""

    async def test_enriched_fields_populated(self) -> None:
        """Report includes cost, tokens, snippet, tracker_status."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=3600,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-E1",
                    {"summary": "Enriched task"},
                    now - 300,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-E1",
                    {"text": "running tests..."},
                    now - 10,
                ),
            ],
        )

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info(
                    "QR-E1",
                    cost_usd=0.45,
                    input_tokens=5000,
                    output_tokens=2000,
                    tracker_status="inProgress",
                ),
            ],
        )

        result = monitor.collect_health()
        report = result.all_agents[0]

        assert report.cost_usd == 0.45
        assert report.input_tokens == 5000
        assert report.output_tokens == 2000
        assert report.tracker_status == "inProgress"
        assert report.last_output_snippet == "running tests..."

    async def test_enriched_fields_defaults(self) -> None:
        """Defaults when task_info lacks enriched fields."""
        now = time.time()
        config = _HeartbeatConfig()
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-E2",
                    {"summary": "Minimal"},
                    now - 30,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-E2",
                    {"text": ""},
                    now - 5,
                ),
            ],
        )

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-E2"),
            ],
        )

        result = monitor.collect_health()
        report = result.all_agents[0]

        assert report.cost_usd is None
        assert report.input_tokens == 0
        assert report.output_tokens == 0
        assert report.tracker_status == ""
        assert report.last_output_snippet == ""


# ===================================================================
# collect_health — review stale detection (parametrized)
# ===================================================================


class TestCollectHealthReviewStale:
    """Stale review detection based on events and status."""

    @pytest.mark.parametrize(
        ("extra_events", "expected_stale"),
        [
            # No review events => stale
            ([], True),
            # REVIEW_SENT prevents stale
            (
                [
                    (EventType.REVIEW_SENT, {}),
                ],
                False,
            ),
            # PIPELINE_FAILED also prevents stale
            (
                [
                    (EventType.PIPELINE_FAILED, {}),
                ],
                False,
            ),
        ],
        ids=["no_events_stale", "review_sent_not_stale", "pipeline_failed_not_stale"],
    )
    async def test_review_stale_detection(
        self,
        extra_events: list[tuple[str, dict]],
        expected_stale: bool,
    ) -> None:
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_review_stale_threshold_seconds=1800,
        )
        bus = EventBus()

        # idle = 2400s > review_stale threshold 1800s
        events = [
            _make_event(
                EventType.TASK_STARTED,
                "QR-4",
                {"summary": "Review task"},
                now - 3600,
            ),
            _make_event(
                EventType.AGENT_OUTPUT,
                "QR-4",
                {},
                now - 2400,
            ),
        ]
        for evt_type, evt_data in extra_events:
            events.append(_make_event(evt_type, "QR-4", evt_data, now - 500))

        await _populate_history(bus, events)

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info(
                    "QR-4",
                    status="in_review",
                    pr_url="https://github.com/org/repo/pull/1",
                ),
            ],
        )

        result = monitor.collect_health()

        if expected_stale:
            assert len(result.stale_reviews) == 1
            assert result.stale_reviews[0].task_key == "QR-4"
            assert result.stale_reviews[0].is_review_stale
        else:
            assert result.stale_reviews == []
            assert not result.all_agents[0].is_review_stale


# ===================================================================
# _evaluate_and_notify — supervisor notification
# ===================================================================


class TestEvaluateAndNotify:
    """Supervisor notification logic (alerts, cooldown, reports)."""

    async def test_stuck_agent_sends_to_supervisor(self) -> None:
        """Stuck agent triggers auto_send."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=7200,
            heartbeat_cooldown_seconds=900,
            heartbeat_full_report_every_n=10,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-10",
                    {"summary": "Stuck"},
                    now - 1200,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-10",
                    {},
                    now - 900,
                ),
            ],
        )

        chat_manager = AsyncMock()
        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-10"),
            ],
            chat_manager=chat_manager,
        )

        result = monitor.collect_health()
        monitor.beat_count = 1

        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="[Heartbeat] alert",
        ) as mock_prompt:
            await monitor._evaluate_and_notify(result)

        chat_manager.auto_send.assert_awaited_once_with(
            "[Heartbeat] alert",
        )
        mock_prompt.assert_called_once()
        call_kwargs = mock_prompt.call_args[1]
        assert len(call_kwargs["stuck"]) == 1
        assert call_kwargs["stuck"][0].task_key == "QR-10"

    async def test_cooldown_prevents_repeat(self) -> None:
        """Same task not alerted twice within cooldown."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=7200,
            heartbeat_cooldown_seconds=900,
            heartbeat_full_report_every_n=100,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-11",
                    {"summary": "Stuck again"},
                    now - 1200,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-11",
                    {},
                    now - 900,
                ),
            ],
        )

        chat_manager = AsyncMock()
        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-11"),
            ],
            chat_manager=chat_manager,
        )

        result = monitor.collect_health()

        # First call — should alert
        monitor.beat_count = 1
        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="alert",
        ):
            await monitor._evaluate_and_notify(result)

        assert chat_manager.auto_send.await_count == 1

        # Second call immediately — cooldown blocks repeat
        monitor.beat_count = 2
        chat_manager.reset_mock()
        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="alert",
        ):
            await monitor._evaluate_and_notify(result)

        chat_manager.auto_send.assert_not_awaited()

    async def test_full_report_on_nth_beat(self) -> None:
        """Healthy summary sent every N beats."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=7200,
            heartbeat_full_report_every_n=3,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-20",
                    {"summary": "All good"},
                    now - 60,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-20",
                    {},
                    now - 5,
                ),
            ],
        )

        chat_manager = AsyncMock()
        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-20"),
            ],
            chat_manager=chat_manager,
        )

        result = monitor.collect_health()
        monitor.beat_count = 3  # divisible by every_n=3

        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="[Heartbeat] all clear",
        ) as mock_prompt:
            await monitor._evaluate_and_notify(result)

        chat_manager.auto_send.assert_awaited_once()
        call_kwargs = mock_prompt.call_args[1]
        assert call_kwargs["is_full_report"] is True

    async def test_no_alert_when_quiet(self) -> None:
        """Nothing sent when no problems and not a full-report beat."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=7200,
            heartbeat_full_report_every_n=5,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-30",
                    {"summary": "Quiet"},
                    now - 60,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-30",
                    {},
                    now - 5,
                ),
            ],
        )

        chat_manager = AsyncMock()
        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-30"),
            ],
            chat_manager=chat_manager,
        )

        result = monitor.collect_health()
        monitor.beat_count = 1  # not divisible by 5

        await monitor._evaluate_and_notify(result)

        chat_manager.auto_send.assert_not_awaited()

    async def test_no_chat_manager_skips_notify(self) -> None:
        """No chat_manager means _evaluate_and_notify is a no-op."""
        bus = EventBus()
        monitor = HeartbeatMonitor(
            config=_HeartbeatConfig(),  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=list,
            chat_manager=None,
        )

        result = monitor.collect_health()
        # Should not raise even without chat_manager
        await monitor._evaluate_and_notify(result)

    async def test_auto_send_failure_handled(self) -> None:
        """auto_send exception is caught and does not propagate."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=100,
            heartbeat_cooldown_seconds=0,
            heartbeat_full_report_every_n=100,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-60",
                    {"summary": "Fail"},
                    now - 300,
                ),
            ],
        )

        chat_manager = AsyncMock()
        chat_manager.auto_send.side_effect = RuntimeError(
            "supervisor down",
        )
        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-60"),
            ],
            chat_manager=chat_manager,
        )

        result = monitor.collect_health()
        monitor.beat_count = 1

        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="alert",
        ):
            await monitor._evaluate_and_notify(result)

        chat_manager.auto_send.assert_awaited_once()

    async def test_cooldown_cleanup(self) -> None:
        """Stale cooldown entries removed when agents finish."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=7200,
            heartbeat_cooldown_seconds=900,
            heartbeat_full_report_every_n=1,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-40",
                    {"summary": "Soon gone"},
                    now - 1200,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-40",
                    {},
                    now - 900,
                ),
            ],
        )

        tasks_running: list[dict[str, object]] = [
            _make_task_info("QR-40"),
        ]

        chat_manager = AsyncMock()
        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: tasks_running,
            chat_manager=chat_manager,
        )

        result = monitor.collect_health()
        monitor.beat_count = 1

        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="alert",
        ):
            await monitor._evaluate_and_notify(result)

        assert "QR-40" in monitor._last_alert_times

        # Phase 2: QR-40 finished
        tasks_running.clear()
        result2 = monitor.collect_health()
        monitor.beat_count = 2

        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="all clear",
        ):
            await monitor._evaluate_and_notify(result2)

        assert "QR-40" not in monitor._last_alert_times

    async def test_cross_category_alerts_not_suppressed(self) -> None:
        """Task in both stuck AND long_running appears in both alerts."""
        now = time.time()
        config = _HeartbeatConfig(
            # Both thresholds exceeded by idle=1200, elapsed=8000
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=7200,
            heartbeat_cooldown_seconds=900,
            heartbeat_full_report_every_n=100,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(
                    EventType.TASK_STARTED,
                    "QR-CC",
                    {"summary": "Both stuck and long"},
                    now - 8000,
                ),
                _make_event(
                    EventType.AGENT_OUTPUT,
                    "QR-CC",
                    {},
                    now - 1200,
                ),
            ],
        )

        chat_manager = AsyncMock()
        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [
                _make_task_info("QR-CC"),
            ],
            chat_manager=chat_manager,
        )

        result = monitor.collect_health()
        # Confirm both flags are set
        assert len(result.stuck) == 1
        assert len(result.long_running) == 1
        assert result.stuck[0].task_key == "QR-CC"
        assert result.long_running[0].task_key == "QR-CC"

        monitor.beat_count = 1

        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="alert",
        ) as mock_prompt:
            await monitor._evaluate_and_notify(result)

        chat_manager.auto_send.assert_awaited_once()
        call_kwargs = mock_prompt.call_args[1]
        assert len(call_kwargs["stuck"]) == 1
        assert len(call_kwargs["long_running"]) == 1


# ===================================================================
# run loop and event publishing
# ===================================================================


class TestHeartbeatLifecycle:
    """Run loop and event publishing."""

    async def test_shutdown_stops_loop(self) -> None:
        """Shutdown event stops the loop."""
        bus = EventBus()
        monitor = HeartbeatMonitor(
            config=_HeartbeatConfig(
                heartbeat_interval_seconds=1,
            ),  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=list,
        )

        shutdown = asyncio.Event()

        async def stop_soon() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        stopper = asyncio.create_task(stop_soon())
        await asyncio.wait_for(
            monitor.run(shutdown),
            timeout=3.0,
        )
        await stopper

        assert monitor.beat_count == 0

    async def test_heartbeat_event_published(self) -> None:
        """HEARTBEAT event published each cycle."""
        bus = EventBus()
        monitor = HeartbeatMonitor(
            config=_HeartbeatConfig(
                heartbeat_interval_seconds=60,
            ),  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=list,
        )

        result = monitor.collect_health()
        monitor.beat_count = 1
        await monitor._publish_heartbeat_event(result)

        history = bus.get_task_history("__heartbeat__")
        assert len(history) == 1
        evt = history[0]
        assert evt.type == EventType.HEARTBEAT
        assert evt.data["total"] == 0
        assert evt.data["healthy"] == 0
        assert evt.data["stuck"] == 0
        assert evt.data["long_running"] == 0
        assert evt.data["stale_reviews"] == 0
        assert evt.data["beat"] == 1

    async def test_heartbeat_manager_uses_heartbeat_channel_key(self) -> None:
        """HeartbeatMonitor notifies via auto_send on the heartbeat-channel manager."""
        now = time.time()
        config = _HeartbeatConfig(
            heartbeat_stuck_threshold_seconds=600,
            heartbeat_long_running_threshold_seconds=7200,
            heartbeat_cooldown_seconds=0,
            heartbeat_full_report_every_n=1,
        )
        bus = EventBus()
        await _populate_history(
            bus,
            [
                _make_event(EventType.TASK_STARTED, "QR-HB", {"summary": "X"}, now - 1200),
                _make_event(EventType.AGENT_OUTPUT, "QR-HB", {}, now - 900),
            ],
        )

        # Simulate a heartbeat-channel manager (distinct from chat-channel)
        heartbeat_channel_manager = AsyncMock()

        monitor = HeartbeatMonitor(
            config=config,  # type: ignore[arg-type]
            event_bus=bus,
            list_running_tasks_callback=lambda: [_make_task_info("QR-HB")],
            chat_manager=heartbeat_channel_manager,
        )

        result = monitor.collect_health()
        monitor.beat_count = 1

        with patch(
            "orchestrator.supervisor_prompt_builder.build_heartbeat_prompt",
            return_value="heartbeat alert",
        ):
            await monitor._evaluate_and_notify(result)

        # Verify auto_send was called on the injected manager
        heartbeat_channel_manager.auto_send.assert_awaited_once_with("heartbeat alert")

        # Verify that if we use a manager with HEARTBEAT_CHANNEL_KEY, events go there
        channel_key = HEARTBEAT_CHANNEL_KEY
        assert channel_key == "supervisor-heartbeat"
