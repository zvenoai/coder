"""Async pub/sub event bus for agent and orchestrator events."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from orchestrator.constants import TERMINAL_EVENT_TYPES, EventType

if TYPE_CHECKING:
    from orchestrator.storage import Storage

logger = logging.getLogger(__name__)

# Ephemeral events that should not be persisted to SQLite
_EPHEMERAL_TYPES = frozenset(
    {
        EventType.AGENT_OUTPUT,
        EventType.AGENT_RESULT,
        EventType.USER_MESSAGE,
        EventType.SUPERVISOR_CHAT_USER,
        EventType.SUPERVISOR_CHAT_CHUNK,
        EventType.SUPERVISOR_CHAT_DONE,
        EventType.SUPERVISOR_CHAT_ERROR,
        EventType.SUPERVISOR_CHAT_THINKING,
        EventType.SUPERVISOR_CHAT_TOOL_USE,
        EventType.ORCHESTRATOR_DECISION,
        EventType.AGENT_MESSAGE_SENT,
        EventType.AGENT_MESSAGE_REPLIED,
        EventType.HEARTBEAT,
    }
)


@dataclass
class Event:
    """An event emitted by the orchestrator or agents."""

    type: str
    task_key: str
    data: dict
    timestamp: float = field(default_factory=time.time)


class EventBus:
    """In-process async pub/sub for agent and orchestrator events.

    Keeps a per-task history ring buffer so new subscribers can replay
    past events (e.g. when a user opens a task terminal after the agent
    has already been running for a while).
    """

    MAX_HISTORY_PER_TASK = 500

    def __init__(self) -> None:
        self._task_subscribers: dict[str, list[asyncio.Queue[Event]]] = {}
        self._global_subscribers: list[asyncio.Queue[Event]] = []
        self._task_history: dict[str, deque[Event]] = {}
        self._storage: Storage | None = None
        self._write_queue: asyncio.Queue[tuple[str, str, str, float]] | None = None
        self._writer_task: asyncio.Task | None = None
        self._stopping = False

    def set_storage(self, storage: Storage) -> None:
        """Set the storage backend for event persistence."""
        self._storage = storage

    async def start(self) -> None:
        """Start the background writer task for persistence."""
        if self._storage is None:
            return
        self._stopping = False
        self._write_queue = asyncio.Queue()
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        """Stop the background writer task and flush remaining events."""
        if self._writer_task is None:
            return

        # Signal writer loop to stop gracefully
        self._stopping = True

        # Wait for writer to process current batch and exit
        try:
            await asyncio.wait_for(self._writer_task, timeout=5.0)
        except asyncio.CancelledError:
            # Task was already cancelled (e.g., by disable_storage())
            pass
        except TimeoutError:
            logger.warning("Writer task did not stop gracefully, cancelling")
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        # Drain and flush any remaining events in queue
        if self._write_queue and self._storage:
            batch = []
            while not self._write_queue.empty():
                try:
                    batch.append(self._write_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if batch:
                try:
                    await self._storage.save_events_batch(batch)
                except Exception as e:
                    logger.error("Failed to flush remaining events: %s", e)

    async def load(self) -> None:
        """Load events from storage into in-memory history."""
        if self._storage is None:
            return

        try:
            events = await self._storage.load_recent_events()
            for evt_dict in events:
                event = Event(
                    type=evt_dict["type"],
                    task_key=evt_dict["task_key"],
                    data=json.loads(evt_dict["data"]),
                    timestamp=evt_dict["timestamp"],
                )
                # Add to history without publishing to subscribers
                history = self._task_history.setdefault(event.task_key, deque(maxlen=self.MAX_HISTORY_PER_TASK))
                history.append(event)
        except Exception as e:
            logger.error("Failed to load events from storage: %s", e)

    async def cleanup_old_events(self) -> None:
        """Delete events older than 7 days."""
        if self._storage is None:
            return
        cutoff = time.time() - 7 * 86400
        try:
            deleted = await self._storage.delete_old_events(cutoff)
            if deleted > 0:
                logger.info("Cleaned up %d old events", deleted)
        except Exception as e:
            logger.error("Failed to cleanup old events: %s", e)

    def disable_storage(self) -> None:
        """Disable event persistence (called on storage failure)."""
        self._storage = None
        # Clear write queue to prevent unbounded growth
        self._write_queue = None
        # Cancel writer task to prevent hot error loop
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()

    async def _writer_loop(self) -> None:
        """Background task that batches and writes events to storage."""
        while not self._stopping:
            try:
                # Check if queue still exists (may be cleared by disable_storage)
                if self._write_queue is None:
                    break

                # Wait for first event with timeout to check _stopping flag
                try:
                    first = await asyncio.wait_for(self._write_queue.get(), timeout=0.5)
                except TimeoutError:
                    continue

                batch = [first]

                # Drain up to 99 more events
                for _ in range(99):
                    try:
                        batch.append(self._write_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Write batch to storage
                if self._storage:
                    try:
                        await self._storage.save_events_batch(batch)
                    except Exception as e:
                        logger.error("Failed to persist event batch: %s", e)
                        self.disable_storage()
                        break

                # Mark tasks as done
                for _ in batch:
                    self._write_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in event writer loop: %s", e)

    async def publish(self, event: Event) -> None:
        """Publish to both task-specific and global subscribers."""
        # Store in per-task history
        history = self._task_history.setdefault(event.task_key, deque(maxlen=self.MAX_HISTORY_PER_TASK))
        history.append(event)

        # Persist non-ephemeral events (only if storage is active)
        if self._storage is not None and self._write_queue is not None and event.type not in _EPHEMERAL_TYPES:
            try:
                self._write_queue.put_nowait((event.type, event.task_key, json.dumps(event.data), event.timestamp))
            except asyncio.QueueFull:
                logger.warning("Event write queue full, dropping event")

        for q in self._task_subscribers.get(event.task_key, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropped event for task %s: subscriber queue full", event.task_key)
        for q in self._global_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropped event for global subscriber: queue full")

    def get_task_history(self, task_key: str) -> list[Event]:
        """Return stored events for a task (oldest first)."""
        history = self._task_history.get(task_key)
        if not history:
            return []
        return list(history)

    def get_global_history(self) -> list[Event]:
        """Return all events from all tasks, sorted by timestamp."""
        all_events: list[Event] = []
        for history in self._task_history.values():
            all_events.extend(history)
        all_events.sort(key=lambda e: e.timestamp)
        return all_events

    def get_orphaned_tasks(self) -> list[str]:
        """Return task keys whose most recent run has no terminal event.

        A task is orphaned when the last ``task_started`` event in its
        history is not followed by any event from
        ``TERMINAL_EVENT_TYPES``.  This happens when the process dies
        mid-execution and the agent session is lost.

        Note: must be called **before** any new tasks are dispatched
        (i.e. at startup), otherwise actively running tasks would be
        incorrectly flagged as orphaned.
        """
        orphaned: list[str] = []
        for task_key, history in self._task_history.items():
            # Find the last task_started event
            last_started_idx: int | None = None
            for idx in range(len(history) - 1, -1, -1):
                if history[idx].type == EventType.TASK_STARTED:
                    last_started_idx = idx
                    break
            if last_started_idx is None:
                continue
            # Check if any event after it is terminal
            has_terminal = any(
                history[i].type in TERMINAL_EVENT_TYPES for i in range(last_started_idx + 1, len(history))
            )
            if not has_terminal:
                orphaned.append(task_key)
        return orphaned

    def subscribe_task(self, task_key: str) -> asyncio.Queue[Event]:
        """Subscribe to events for a specific task."""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._task_subscribers.setdefault(task_key, []).append(q)
        return q

    def subscribe_global(self) -> asyncio.Queue[Event]:
        """Subscribe to all events."""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._global_subscribers.append(q)
        return q

    def unsubscribe_task(self, task_key: str, q: asyncio.Queue[Event]) -> None:
        """Remove a task-specific subscriber."""
        subs = self._task_subscribers.get(task_key)
        if subs:
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                del self._task_subscribers[task_key]

    def unsubscribe_global(self, q: asyncio.Queue[Event]) -> None:
        """Remove a global subscriber."""
        try:
            self._global_subscribers.remove(q)
        except ValueError:
            pass
