"""Heartbeat monitor — periodic health checks for running agents."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from orchestrator.constants import EventType
from orchestrator.event_bus import Event
from orchestrator.metrics import HEARTBEAT_STUCK

if TYPE_CHECKING:
    from orchestrator.config import Config
    from orchestrator.event_bus import EventBus
    from orchestrator.supervisor_chat import SupervisorChatManager

logger = logging.getLogger(__name__)


@dataclass
class AgentHealthReport:
    """Health metrics for a single running agent."""

    task_key: str
    status: str
    issue_summary: str
    elapsed_seconds: float
    idle_seconds: float
    compaction_count: int
    pr_url: str
    is_stuck: bool
    is_long_running: bool
    is_review_stale: bool
    cost_usd: float | None = None
    last_output_snippet: str = ""
    tracker_status: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class HeartbeatResult:
    """Aggregated health report across all agents."""

    total_agents: int
    healthy_agents: int
    stuck: list[AgentHealthReport] = field(default_factory=list)
    long_running: list[AgentHealthReport] = field(default_factory=list)
    stale_reviews: list[AgentHealthReport] = field(default_factory=list)
    all_agents: list[AgentHealthReport] = field(default_factory=list)


class HeartbeatMonitor:
    """Periodically collects agent health metrics and notifies supervisor.

    Attributes:
        beat_count: Number of heartbeats completed since start.
    """

    def __init__(
        self,
        config: Config,
        event_bus: EventBus,
        list_running_tasks_callback: Callable[[], list[dict[str, object]]],
        chat_manager: SupervisorChatManager | None = None,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._list_running_tasks = list_running_tasks_callback
        self._chat_manager = chat_manager
        self._last_alert_times: dict[str, float] = {}
        self.beat_count: int = 0

    async def run(self, shutdown: asyncio.Event) -> None:
        """Main heartbeat loop.

        Runs until *shutdown* is set. Each iteration collects health
        metrics and optionally notifies the supervisor.
        """
        interval = self._config.heartbeat_interval_seconds
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
                break  # shutdown signalled
            except TimeoutError:
                pass

            try:
                result = self.collect_health()
                self.beat_count += 1
                await self._publish_heartbeat_event(result)
                await self._evaluate_and_notify(result)
            except Exception:
                logger.warning("Heartbeat cycle failed", exc_info=True)

    def collect_health(self) -> HeartbeatResult:
        """Collect health metrics from EventBus history.

        Pure data collection — no LLM calls.
        """
        now = time.time()
        tasks = self._list_running_tasks()
        reports: list[AgentHealthReport] = []

        for task_info in tasks:
            task_key = str(task_info.get("task_key", ""))
            status = str(task_info.get("status", "unknown"))
            pr_url = str(task_info.get("pr_url", ""))
            history = self._event_bus.get_task_history(task_key)

            # Extract metrics from event history
            started_at = _find_started_at(history)
            last_activity_at = _find_last_activity(history)
            compaction_count = _count_compactions(history)
            has_review_event = _has_review_event(history)
            issue_summary = _find_summary(history, task_key)

            elapsed = max(0.0, now - started_at) if started_at else 0.0
            idle = max(0.0, now - last_activity_at) if last_activity_at else elapsed

            stuck_threshold = self._config.heartbeat_stuck_threshold_seconds
            long_threshold = self._config.heartbeat_long_running_threshold_seconds
            stale_threshold = self._config.heartbeat_review_stale_threshold_seconds

            is_stuck = idle > stuck_threshold
            is_long = elapsed > long_threshold
            is_stale = status == "in_review" and not has_review_event and idle > stale_threshold

            raw_cost = task_info.get("cost_usd")
            cost_usd: float | None = (
                float(raw_cost)  # type: ignore[arg-type]
                if raw_cost is not None
                else None
            )
            tracker_status = str(
                task_info.get("tracker_status", ""),
            )
            raw_in = task_info.get("input_tokens", 0)
            raw_out = task_info.get("output_tokens", 0)
            input_tok = (
                int(raw_in)  # type: ignore[call-overload]
                if raw_in is not None
                else 0
            )
            output_tok = (
                int(raw_out)  # type: ignore[call-overload]
                if raw_out is not None
                else 0
            )
            last_snippet = _find_last_output_snippet(
                history,
            )

            reports.append(
                AgentHealthReport(
                    task_key=task_key,
                    status=status,
                    issue_summary=issue_summary,
                    elapsed_seconds=elapsed,
                    idle_seconds=idle,
                    compaction_count=compaction_count,
                    pr_url=pr_url,
                    is_stuck=is_stuck,
                    is_long_running=is_long,
                    is_review_stale=is_stale,
                    cost_usd=cost_usd,
                    last_output_snippet=last_snippet,
                    tracker_status=tracker_status,
                    input_tokens=input_tok,
                    output_tokens=output_tok,
                )
            )

        stuck = [r for r in reports if r.is_stuck]
        long_running = [r for r in reports if r.is_long_running]
        stale = [r for r in reports if r.is_review_stale]
        healthy = len(reports) - len({r.task_key for r in stuck + long_running + stale})

        # Update Prometheus gauge for stuck tasks
        HEARTBEAT_STUCK.set(len(stuck))

        return HeartbeatResult(
            total_agents=len(reports),
            healthy_agents=healthy,
            stuck=stuck,
            long_running=long_running,
            stale_reviews=stale,
            all_agents=reports,
        )

    async def _publish_heartbeat_event(self, result: HeartbeatResult) -> None:
        """Publish ephemeral HEARTBEAT event for the dashboard."""
        await self._event_bus.publish(
            Event(
                type=EventType.HEARTBEAT,
                task_key="__heartbeat__",
                data={
                    "total": result.total_agents,
                    "healthy": result.healthy_agents,
                    "stuck": len(result.stuck),
                    "long_running": len(result.long_running),
                    "stale_reviews": len(result.stale_reviews),
                    "beat": self.beat_count,
                },
            )
        )

    async def _evaluate_and_notify(self, result: HeartbeatResult) -> None:
        """Decide whether to notify supervisor based on results."""
        if self._chat_manager is None:
            return

        now = time.monotonic()
        cooldown = self._config.heartbeat_cooldown_seconds
        every_n = self._config.heartbeat_full_report_every_n

        # Determine if this is a full report beat
        is_full = every_n > 0 and self.beat_count % every_n == 0

        # Collect tasks needing alert (respecting cooldown).
        # Snapshot cooldown state BEFORE iterating so a task appearing
        # in multiple categories (stuck + long_running) isn't
        # suppressed in later loops by its own update.
        alert_stuck: list[AgentHealthReport] = []
        alert_long: list[AgentHealthReport] = []
        alert_stale: list[AgentHealthReport] = []
        alerted_keys: set[str] = set()

        def _check(r: AgentHealthReport) -> bool:
            last = self._last_alert_times.get(r.task_key)
            return last is None or now - last >= cooldown

        for r in result.stuck:
            if _check(r):
                alert_stuck.append(r)
                alerted_keys.add(r.task_key)

        for r in result.long_running:
            if _check(r):
                alert_long.append(r)
                alerted_keys.add(r.task_key)

        for r in result.stale_reviews:
            if _check(r):
                alert_stale.append(r)
                alerted_keys.add(r.task_key)

        # Update cooldown timestamps after all categories processed.
        for key in alerted_keys:
            self._last_alert_times[key] = now

        has_problems = bool(alert_stuck or alert_long or alert_stale)

        if not has_problems and not is_full:
            return

        # Clean up stale cooldown entries
        active_keys = {r.task_key for r in result.all_agents}
        stale_keys = [k for k in self._last_alert_times if k not in active_keys]
        for k in stale_keys:
            del self._last_alert_times[k]

        from orchestrator.supervisor_prompt_builder import (
            build_heartbeat_prompt,
        )

        prompt = build_heartbeat_prompt(
            result=result,
            stuck=alert_stuck,
            long_running=alert_long,
            stale_reviews=alert_stale,
            is_full_report=is_full,
        )
        try:
            await self._chat_manager.auto_send(prompt)
        except Exception:
            logger.warning(
                "Failed to send heartbeat to supervisor",
                exc_info=True,
            )


# ------------------------------------------------------------------
# Event history helpers (module-level, per style guide)
# ------------------------------------------------------------------


def _find_started_at(
    history: list[Any],
) -> float | None:
    """Find TASK_STARTED timestamp (scan from end)."""
    for event in reversed(history):
        if event.type == EventType.TASK_STARTED:
            return event.timestamp
    return None


def _find_last_activity(
    history: list[Any],
) -> float | None:
    """Find most recent AGENT_OUTPUT timestamp."""
    for event in reversed(history):
        if event.type == EventType.AGENT_OUTPUT:
            return event.timestamp
    return None


def _count_compactions(history: list[Any]) -> int:
    """Count COMPACTION_TRIGGERED events."""
    return sum(1 for e in history if e.type == EventType.COMPACTION_TRIGGERED)


def _has_review_event(history: list[Any]) -> bool:
    """Check if REVIEW_SENT or PIPELINE_FAILED exists."""
    return any(
        e.type
        in (
            EventType.REVIEW_SENT,
            EventType.PIPELINE_FAILED,
        )
        for e in history
    )


def _find_summary(history: list[Any], default: str) -> str:
    """Extract issue summary from TASK_STARTED event."""
    for event in history:
        if event.type == EventType.TASK_STARTED:
            return str(event.data.get("summary", default))
    return default


def _find_last_output_snippet(
    history: list[Any],
    max_len: int = 200,
) -> str:
    """Last *max_len* chars from most recent AGENT_OUTPUT."""
    for event in reversed(history):
        if event.type == EventType.AGENT_OUTPUT:
            text = str(event.data.get("text", ""))
            if text:
                return text[-max_len:]
    return ""
