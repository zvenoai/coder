"""EventBus subscriber that persists statistics to Storage."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from orchestrator.constants import EventType
from orchestrator.metrics import COMPACTION_TOTAL, TASK_COST, TASK_DURATION, TASKS_TOTAL
from orchestrator.recovery import ErrorCategory, classify_error
from orchestrator.stats_models import ErrorLogEntry, ProposalRecord, SupervisorRun, TaskRun

if TYPE_CHECKING:
    from orchestrator.event_bus import Event, EventBus
    from orchestrator.storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class _PendingTask:
    """Tracks in-flight task data for correlation between start and completion events."""

    started_at: float = 0.0
    model: str = "unknown"
    needs_info: bool = False


@dataclass
class _PendingSupervisor:
    """Tracks in-flight supervisor data."""

    started_at: float = 0.0
    trigger_keys: list[str] = field(default_factory=list)


class StatsCollector:
    """Subscribes to EventBus and records statistics to Storage.

    Maintains _pending maps to correlate TASK_STARTED/MODEL_SELECTED
    with subsequent TASK_COMPLETED/TASK_FAILED events.
    """

    def __init__(self, storage: Storage, event_bus: EventBus) -> None:
        self._db = storage
        self._bus = event_bus
        self._queue: asyncio.Queue[Event] | None = None
        self._task: asyncio.Task | None = None
        self._pending_tasks: dict[str, _PendingTask] = {}
        self._pending_supervisor: _PendingSupervisor | None = None

    async def start(self) -> None:
        """Subscribe to the event bus and start processing."""
        self._queue = self._bus.subscribe_global()
        self._task = asyncio.create_task(self._run(), name="stats-collector")

    async def stop(self) -> None:
        """Stop processing and unsubscribe."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._queue:
            self._bus.unsubscribe_global(self._queue)
            self._queue = None

    async def drain(self) -> None:
        """Wait until all currently queued events have been processed by _run. Used in tests."""
        if not self._queue:
            return
        await self._queue.join()

    async def _run(self) -> None:
        """Main event processing loop."""
        if self._queue is None:
            raise RuntimeError("_run called before start()")
        while True:
            event = await self._queue.get()
            try:
                await self._handle(event)
            except Exception:
                logger.exception("Error handling event %s for %s", event.type, event.task_key)
            finally:
                self._queue.task_done()

    async def _handle(self, event: Event) -> None:
        """Route event to appropriate handler."""
        etype = event.type
        if etype == EventType.TASK_STARTED:
            self._on_task_started(event)
        elif etype == EventType.MODEL_SELECTED:
            self._on_model_selected(event)
        elif etype == EventType.TASK_COMPLETED:
            await self._on_task_completed(event)
        elif etype == EventType.TASK_FAILED:
            await self._on_task_failed(event)
        elif etype == EventType.NEEDS_INFO:
            self._on_needs_info(event)
        elif etype == EventType.PR_TRACKED:
            await self._on_pr_tracked(event)
        elif etype == EventType.PR_MERGED:
            await self._on_pr_merged(event)
        elif etype == EventType.REVIEW_SENT:
            await self._on_review_sent(event)
        elif etype == EventType.PIPELINE_FAILED:
            await self._on_pipeline_failed(event)
        elif etype == EventType.SUPERVISOR_STARTED:
            self._on_supervisor_started(event)
        elif etype == EventType.SUPERVISOR_COMPLETED:
            await self._on_supervisor_completed(event)
        elif etype == EventType.SUPERVISOR_FAILED:
            await self._on_supervisor_failed(event)
        elif etype == EventType.TASK_PROPOSED:
            await self._on_task_proposed(event)
        elif etype == EventType.PROPOSAL_APPROVED:
            await self._on_proposal_resolved(event, "approved")
        elif etype == EventType.PROPOSAL_REJECTED:
            await self._on_proposal_resolved(event, "rejected")
        elif etype == EventType.TASK_VERIFIED:
            await self._on_task_verified(event)
        elif etype == EventType.VERIFICATION_FAILED:
            await self._on_verification_failed(event)
        elif etype == EventType.COMPACTION_TRIGGERED:
            self._on_compaction_triggered(event)

    # ---- Task handlers ----

    def _on_task_started(self, event: Event) -> None:
        self._pending_tasks[event.task_key] = _PendingTask(started_at=event.timestamp)

    def _on_model_selected(self, event: Event) -> None:
        pending = self._pending_tasks.get(event.task_key)
        if pending:
            pending.model = event.data.get("model", "unknown")
        else:
            self._pending_tasks[event.task_key] = _PendingTask(model=event.data.get("model", "unknown"))

    def _on_needs_info(self, event: Event) -> None:
        pending = self._pending_tasks.get(event.task_key)
        if pending:
            pending.needs_info = True
        else:
            self._pending_tasks[event.task_key] = _PendingTask(needs_info=True)

    async def _on_task_completed(self, event: Event) -> None:
        pending = self._pending_tasks.pop(event.task_key, _PendingTask())
        data = event.data
        model = data.get("model") or pending.model
        started = pending.started_at or event.timestamp

        # Update Prometheus metrics
        TASKS_TOTAL.labels(status="completed").inc()
        duration = data.get("duration") or 0.0
        cost = data.get("cost") or 0.0
        if duration > 0:
            TASK_DURATION.observe(duration)
        if cost > 0:
            TASK_COST.observe(cost)

        await self._db.record_task_run(
            TaskRun(
                task_key=event.task_key,
                model=model,
                cost_usd=cost,
                duration_seconds=duration,
                success=True,
                error_category=None,
                pr_url=data.get("pr_url"),
                needs_info=pending.needs_info,
                resumed=bool(data.get("resumed")),
                started_at=started,
                finished_at=event.timestamp,
                session_id=data.get("session_id"),
            )
        )

    async def _on_task_failed(self, event: Event) -> None:
        pending = self._pending_tasks.pop(event.task_key, _PendingTask())
        data = event.data
        model = data.get("model") or pending.model
        started = pending.started_at or event.timestamp
        error_text = data.get("error", "unknown")
        error_category = ErrorCategory.CANCELLED.value if data.get("cancelled") else classify_error(error_text).value

        # Update Prometheus metrics
        TASKS_TOTAL.labels(status="failed").inc()
        duration = data.get("duration") or 0.0
        cost = data.get("cost") or 0.0
        if duration > 0:
            TASK_DURATION.observe(duration)
        if cost > 0:
            TASK_COST.observe(cost)

        await self._db.record_task_run(
            TaskRun(
                task_key=event.task_key,
                model=model,
                cost_usd=cost,
                duration_seconds=duration,
                success=False,
                error_category=error_category,
                pr_url=None,
                needs_info=pending.needs_info,
                resumed=False,
                started_at=started,
                finished_at=event.timestamp,
                session_id=data.get("session_id"),
            )
        )

        await self._db.record_error(
            ErrorLogEntry(
                task_key=event.task_key,
                error_category=error_category,
                error_message=error_text[:500],
                retryable=bool(data.get("retryable")),
                timestamp=event.timestamp,
            )
        )

    # ---- PR handlers ----

    async def _on_pr_tracked(self, event: Event) -> None:
        pending = self._pending_tasks.pop(event.task_key, _PendingTask())
        data = event.data
        model = data.get("model") or pending.model
        started = pending.started_at or event.timestamp

        # Update Prometheus metrics (PR_TRACKED is a success path)
        TASKS_TOTAL.labels(status="completed").inc()
        duration = data.get("duration") or 0.0
        cost = data.get("cost") or 0.0
        if duration > 0:
            TASK_DURATION.observe(duration)
        if cost > 0:
            TASK_COST.observe(cost)

        await self._db.record_task_run(
            TaskRun(
                task_key=event.task_key,
                model=model,
                cost_usd=cost,
                duration_seconds=duration,
                success=True,
                error_category=None,
                pr_url=data.get("pr_url"),
                needs_info=pending.needs_info,
                resumed=bool(data.get("resumed")),
                started_at=started,
                finished_at=event.timestamp,
                session_id=data.get("session_id"),
            )
        )

        await self._db.record_pr_tracked(
            event.task_key,
            data.get("pr_url", ""),
            event.timestamp,
        )

    async def _on_pr_merged(self, event: Event) -> None:
        data = event.data or {}
        pr_url = data.get("pr_url")
        if not pr_url:
            logger.error(
                "PR_MERGED event for %s missing pr_url in data - cannot update pr_lifecycle",
                event.task_key,
            )
            return
        await self._db.record_pr_merged(
            event.task_key,
            pr_url,
            event.timestamp,
        )

    async def _on_review_sent(self, event: Event) -> None:
        await self._db.increment_review_iterations(event.task_key)

    async def _on_pipeline_failed(self, event: Event) -> None:
        await self._db.increment_ci_failures(event.task_key)

    # ---- Verification handlers ----

    async def _on_task_verified(self, event: Event) -> None:
        """Record successful post-merge verification."""
        await self._db.record_pr_verified(
            event.task_key,
            event.timestamp,
        )

    async def _on_verification_failed(self, event: Event) -> None:
        """Record failed post-merge verification."""
        await self._db.record_error(
            ErrorLogEntry(
                task_key=event.task_key,
                error_category="verification_failed",
                error_message=event.data.get(
                    "summary",
                    "Verification failed",
                )[:500],
                retryable=False,
                timestamp=event.timestamp,
            )
        )

    # ---- Supervisor handlers ----

    def _on_supervisor_started(self, event: Event) -> None:
        self._pending_supervisor = _PendingSupervisor(
            started_at=event.timestamp,
            trigger_keys=event.data.get("triggers", []),
        )

    async def _on_supervisor_completed(self, event: Event) -> None:
        pending = self._pending_supervisor or _PendingSupervisor()
        self._pending_supervisor = None
        data = event.data

        await self._db.record_supervisor_run(
            SupervisorRun(
                trigger_task_keys=pending.trigger_keys,
                cost_usd=data.get("cost") or 0.0,
                duration_seconds=event.timestamp - pending.started_at if pending.started_at else 0.0,
                success=True,
                tasks_created=data.get("tasks_created", []),
                started_at=pending.started_at or event.timestamp,
                finished_at=event.timestamp,
            )
        )

    async def _on_supervisor_failed(self, event: Event) -> None:
        pending = self._pending_supervisor or _PendingSupervisor()
        self._pending_supervisor = None

        await self._db.record_supervisor_run(
            SupervisorRun(
                trigger_task_keys=pending.trigger_keys,
                cost_usd=event.data.get("cost") or 0.0,
                duration_seconds=event.timestamp - pending.started_at if pending.started_at else 0.0,
                success=False,
                tasks_created=[],
                started_at=pending.started_at or event.timestamp,
                finished_at=event.timestamp,
            )
        )

    # ---- Proposal handlers ----

    async def _on_task_proposed(self, event: Event) -> None:
        data = event.data
        await self._db.upsert_proposal(
            ProposalRecord(
                proposal_id=data.get("proposal_id", ""),
                source_task_key=event.task_key,
                summary=data.get("summary", ""),
                category=data.get("category", "improvement"),
                status="pending",
                created_at=event.timestamp,
            )
        )

    async def _on_proposal_resolved(self, event: Event, status: str) -> None:
        proposal_id = event.data.get("proposal_id", "")
        if proposal_id:
            await self._db.resolve_proposal(proposal_id, status, event.timestamp)

    # ---- Compaction handler ----

    def _on_compaction_triggered(self, event: Event) -> None:
        """Record compaction event in metrics."""
        COMPACTION_TOTAL.inc()
