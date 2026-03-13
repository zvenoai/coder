"""Tests for event_bus module."""

import asyncio
import logging
import time

import pytest

from orchestrator.constants import EventType
from orchestrator.event_bus import Event, EventBus


class TestEvent:
    def test_creates_with_defaults(self) -> None:
        event = Event(type="test", task_key="QR-1", data={"msg": "hello"})
        assert event.type == "test"
        assert event.task_key == "QR-1"
        assert event.data == {"msg": "hello"}
        assert event.timestamp > 0


class TestEventBusPublish:
    async def test_publish_to_task_subscriber(self) -> None:
        bus = EventBus()
        queue = bus.subscribe_task("QR-1")
        event = Event(type="agent_output", task_key="QR-1", data={"text": "hi"})
        await bus.publish(event)
        received = queue.get_nowait()
        assert received is event

    async def test_publish_to_global_subscriber(self) -> None:
        bus = EventBus()
        queue = bus.subscribe_global()
        event = Event(type="task_started", task_key="QR-1", data={})
        await bus.publish(event)
        received = queue.get_nowait()
        assert received is event

    async def test_publish_reaches_both_task_and_global(self) -> None:
        bus = EventBus()
        task_q = bus.subscribe_task("QR-1")
        global_q = bus.subscribe_global()
        event = Event(type="agent_output", task_key="QR-1", data={"text": "x"})
        await bus.publish(event)
        assert task_q.get_nowait() is event
        assert global_q.get_nowait() is event

    async def test_task_subscriber_only_gets_matching_events(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe_task("QR-1")
        q2 = bus.subscribe_task("QR-2")
        event = Event(type="agent_output", task_key="QR-1", data={})
        await bus.publish(event)
        assert q1.get_nowait() is event
        assert q2.empty()

    async def test_multiple_task_subscribers(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe_task("QR-1")
        q2 = bus.subscribe_task("QR-1")
        event = Event(type="agent_output", task_key="QR-1", data={})
        await bus.publish(event)
        assert q1.get_nowait() is event
        assert q2.get_nowait() is event

    async def test_publish_drops_when_queue_full(self) -> None:
        bus = EventBus()
        queue = bus.subscribe_task("QR-1")
        # Fill the queue
        for i in range(1000):
            await bus.publish(Event(type="output", task_key="QR-1", data={"i": i}))
        # This should not raise — it drops silently
        await bus.publish(Event(type="output", task_key="QR-1", data={"overflow": True}))
        assert queue.qsize() == 1000

    async def test_publish_logs_warning_when_task_queue_full(self, caplog: pytest.LogCaptureFixture) -> None:
        bus = EventBus()
        bus.subscribe_task("QR-1")
        for i in range(1000):
            await bus.publish(Event(type="output", task_key="QR-1", data={"i": i}))
        with caplog.at_level(logging.WARNING, logger="orchestrator.event_bus"):
            await bus.publish(Event(type="output", task_key="QR-1", data={"overflow": True}))
        assert any("QR-1" in r.message for r in caplog.records)

    async def test_publish_logs_warning_when_global_queue_full(self, caplog: pytest.LogCaptureFixture) -> None:
        bus = EventBus()
        bus.subscribe_global()
        for i in range(1000):
            await bus.publish(Event(type="output", task_key="QR-1", data={"i": i}))
        with caplog.at_level(logging.WARNING, logger="orchestrator.event_bus"):
            await bus.publish(Event(type="output", task_key="QR-1", data={"overflow": True}))
        assert any("global" in r.message.lower() for r in caplog.records)

    async def test_publish_with_no_subscribers(self) -> None:
        bus = EventBus()
        # Should not raise
        await bus.publish(Event(type="test", task_key="QR-1", data={}))

    async def test_global_subscriber_gets_all_task_events(self) -> None:
        """Global subscriber receives events from all tasks."""
        bus = EventBus()
        global_q = bus.subscribe_global()
        e1 = Event(type="agent_output", task_key="QR-1", data={"text": "a"})
        e2 = Event(type="agent_output", task_key="QR-2", data={"text": "b"})
        e3 = Event(type="agent_output", task_key="QR-3", data={"text": "c"})
        await bus.publish(e1)
        await bus.publish(e2)
        await bus.publish(e3)
        assert global_q.get_nowait() is e1
        assert global_q.get_nowait() is e2
        assert global_q.get_nowait() is e3

    async def test_task_isolation_multiple_tasks(self) -> None:
        """Events for different tasks are strictly isolated."""
        bus = EventBus()
        q1 = bus.subscribe_task("QR-1")
        q2 = bus.subscribe_task("QR-2")
        q3 = bus.subscribe_task("QR-3")

        await bus.publish(Event(type="output", task_key="QR-1", data={"n": 1}))
        await bus.publish(Event(type="output", task_key="QR-2", data={"n": 2}))
        await bus.publish(Event(type="output", task_key="QR-3", data={"n": 3}))
        await bus.publish(Event(type="output", task_key="QR-1", data={"n": 4}))

        # QR-1 gets events 1 and 4
        assert q1.qsize() == 2
        assert q1.get_nowait().data["n"] == 1
        assert q1.get_nowait().data["n"] == 4

        # QR-2 gets only event 2
        assert q2.qsize() == 1
        assert q2.get_nowait().data["n"] == 2

        # QR-3 gets only event 3
        assert q3.qsize() == 1
        assert q3.get_nowait().data["n"] == 3


class TestEventBusUnsubscribe:
    async def test_unsubscribe_task(self) -> None:
        bus = EventBus()
        queue = bus.subscribe_task("QR-1")
        bus.unsubscribe_task("QR-1", queue)
        await bus.publish(Event(type="test", task_key="QR-1", data={}))
        assert queue.empty()

    async def test_unsubscribe_global(self) -> None:
        bus = EventBus()
        queue = bus.subscribe_global()
        bus.unsubscribe_global(queue)
        await bus.publish(Event(type="test", task_key="QR-1", data={}))
        assert queue.empty()

    def test_unsubscribe_nonexistent_task(self) -> None:
        bus = EventBus()
        q: asyncio.Queue = asyncio.Queue()
        # Should not raise
        bus.unsubscribe_task("NOPE", q)

    def test_unsubscribe_nonexistent_global(self) -> None:
        bus = EventBus()
        q: asyncio.Queue = asyncio.Queue()
        # Should not raise
        bus.unsubscribe_global(q)

    async def test_unsubscribe_cleans_up_empty_task_list(self) -> None:
        bus = EventBus()
        queue = bus.subscribe_task("QR-1")
        assert "QR-1" in bus._task_subscribers
        bus.unsubscribe_task("QR-1", queue)
        assert "QR-1" not in bus._task_subscribers


class TestEventBusHistory:
    async def test_history_stored_per_task(self) -> None:
        bus = EventBus()
        await bus.publish(Event(type="output", task_key="QR-1", data={"n": 1}))
        await bus.publish(Event(type="output", task_key="QR-1", data={"n": 2}))
        await bus.publish(Event(type="output", task_key="QR-2", data={"n": 3}))

        h1 = bus.get_task_history("QR-1")
        assert len(h1) == 2
        assert h1[0].data["n"] == 1
        assert h1[1].data["n"] == 2

        h2 = bus.get_task_history("QR-2")
        assert len(h2) == 1
        assert h2[0].data["n"] == 3

    async def test_history_empty_for_unknown_task(self) -> None:
        bus = EventBus()
        assert bus.get_task_history("QR-999") == []

    async def test_history_capped_at_max(self) -> None:
        bus = EventBus()
        for i in range(bus.MAX_HISTORY_PER_TASK + 100):
            await bus.publish(Event(type="output", task_key="QR-1", data={"i": i}))

        history = bus.get_task_history("QR-1")
        assert len(history) == bus.MAX_HISTORY_PER_TASK
        # Oldest events should be dropped, newest retained
        assert history[0].data["i"] == 100
        assert history[-1].data["i"] == bus.MAX_HISTORY_PER_TASK + 99

    async def test_history_returns_copy(self) -> None:
        """Returned list should be a copy, not the internal deque."""
        bus = EventBus()
        await bus.publish(Event(type="output", task_key="QR-1", data={}))
        h1 = bus.get_task_history("QR-1")
        h1.clear()
        # Internal history should be unaffected
        assert len(bus.get_task_history("QR-1")) == 1

    async def test_history_available_without_subscribers(self) -> None:
        """Events are stored in history even if no subscribers exist."""
        bus = EventBus()
        await bus.publish(Event(type="output", task_key="QR-1", data={"val": "x"}))
        assert len(bus.get_task_history("QR-1")) == 1


class TestGlobalHistory:
    async def test_returns_all_events_sorted(self) -> None:
        bus = EventBus()
        e1 = Event(type="started", task_key="QR-1", data={}, timestamp=100.0)
        e2 = Event(type="output", task_key="QR-2", data={}, timestamp=200.0)
        e3 = Event(type="output", task_key="QR-1", data={}, timestamp=150.0)
        await bus.publish(e1)
        await bus.publish(e2)
        await bus.publish(e3)

        history = bus.get_global_history()
        assert len(history) == 3
        assert history[0].timestamp == 100.0
        assert history[1].timestamp == 150.0
        assert history[2].timestamp == 200.0

    async def test_empty_when_no_events(self) -> None:
        bus = EventBus()
        assert bus.get_global_history() == []

    async def test_includes_events_from_all_tasks(self) -> None:
        bus = EventBus()
        await bus.publish(Event(type="a", task_key="QR-1", data={}))
        await bus.publish(Event(type="b", task_key="QR-2", data={}))
        await bus.publish(Event(type="c", task_key="QR-3", data={}))

        history = bus.get_global_history()
        task_keys = {e.task_key for e in history}
        assert task_keys == {"QR-1", "QR-2", "QR-3"}


class TestEventBusPersistence:
    @pytest.fixture
    async def storage(self, tmp_path):
        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(str(tmp_path / "test_events.db"))
        await db.open()
        yield db
        await db.close()

    async def test_persisted_events_survive_restart(self, storage):
        """Structural events should be loadable after EventBus restart."""
        bus1 = EventBus()
        bus1.set_storage(storage)
        await bus1.start()
        await bus1.publish(Event(type=EventType.TASK_STARTED, task_key="QR-1", data={"model": "sonnet"}))
        await bus1.stop()

        bus2 = EventBus()
        bus2.set_storage(storage)
        await bus2.load()
        history = bus2.get_task_history("QR-1")
        assert len(history) == 1
        assert history[0].type == EventType.TASK_STARTED

    async def test_ephemeral_events_not_persisted(self, storage):
        """AGENT_OUTPUT, AGENT_RESULT, USER_MESSAGE should not be in SQLite."""
        bus = EventBus()
        bus.set_storage(storage)
        await bus.start()
        await bus.publish(Event(type=EventType.AGENT_OUTPUT, task_key="QR-1", data={"text": "hi"}))
        await bus.publish(Event(type=EventType.AGENT_RESULT, task_key="QR-1", data={}))
        await bus.publish(Event(type=EventType.USER_MESSAGE, task_key="QR-1", data={"text": "hello"}))
        await bus.stop()

        events = await storage.load_events_for_task("QR-1")
        assert len(events) == 0

    async def test_structural_events_persisted(self, storage):
        """Non-ephemeral events should be saved to SQLite."""
        bus = EventBus()
        bus.set_storage(storage)
        await bus.start()
        await bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-1", data={}))
        await bus.publish(Event(type=EventType.TASK_COMPLETED, task_key="QR-1", data={"cost": 0.5}))
        await bus.stop()

        events = await storage.load_events_for_task("QR-1")
        assert len(events) == 2

    async def test_load_populates_global_history(self, storage):
        """load() should populate _task_history for dashboard queries."""
        # Write events directly to storage
        await storage.save_events_batch(
            [
                ("task_started", "QR-1", "{}", 100.0),
                ("task_completed", "QR-2", '{"cost": 0.1}', 200.0),
            ]
        )

        bus = EventBus()
        bus.set_storage(storage)
        await bus.load()

        global_history = bus.get_global_history()
        assert len(global_history) == 2
        assert global_history[0].task_key == "QR-1"
        assert global_history[1].task_key == "QR-2"

    async def test_load_does_not_publish_to_subscribers(self, storage):
        """load() should fill history but NOT notify subscribers."""
        await storage.save_events_batch([("task_started", "QR-1", "{}", 100.0)])

        bus = EventBus()
        bus.set_storage(storage)
        q = bus.subscribe_global()
        await bus.load()

        assert q.empty()

    async def test_works_without_storage(self):
        """EventBus should work normally when no storage is set."""
        bus = EventBus()
        await bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-1", data={}))
        assert len(bus.get_task_history("QR-1")) == 1

    async def test_in_memory_still_includes_ephemeral(self):
        """In-memory history should include ephemeral events even though they're not persisted."""
        bus = EventBus()
        await bus.publish(Event(type=EventType.AGENT_OUTPUT, task_key="QR-1", data={"text": "x"}))
        assert len(bus.get_task_history("QR-1")) == 1

    async def test_cleanup_old_events(self, storage):
        """cleanup_old_events() should delete events older than 7 days."""
        old_ts = time.time() - 8 * 86400  # 8 days ago
        recent_ts = time.time() - 1 * 86400  # 1 day ago
        await storage.save_events_batch(
            [
                ("task_started", "QR-old", "{}", old_ts),
                ("task_started", "QR-new", "{}", recent_ts),
            ]
        )

        bus = EventBus()
        bus.set_storage(storage)
        await bus.cleanup_old_events()

        events = await storage.load_recent_events()
        assert len(events) == 1
        assert events[0]["task_key"] == "QR-new"

    async def test_disable_storage(self, storage):
        """disable_storage() should stop persisting events."""
        bus = EventBus()
        bus.set_storage(storage)
        await bus.start()
        bus.disable_storage()
        await bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-1", data={}))
        await bus.stop()

        events = await storage.load_events_for_task("QR-1")
        assert len(events) == 0

    async def test_graceful_shutdown_flushes_pending_events(self, storage):
        """stop() should wait for writer_loop to finish processing current batch."""
        bus = EventBus()
        bus.set_storage(storage)
        await bus.start()

        # Publish events
        for i in range(5):
            await bus.publish(Event(type=EventType.TASK_STARTED, task_key=f"QR-{i}", data={}))

        # Give writer a moment to start processing
        await asyncio.sleep(0.1)

        # Stop should flush remaining events
        await bus.stop()

        # All events should be persisted
        events = await storage.load_recent_events()
        assert len(events) == 5

    async def test_storage_failure_stops_queue_growth(self, storage):
        """After storage failure, publish() should stop enqueuing events to prevent unbounded growth."""
        bus = EventBus()
        bus.set_storage(storage)
        await bus.start()

        # Close storage to simulate failure
        await storage.close()

        # Publish an event - this should trigger storage failure in writer_loop
        await bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-1", data={}))
        await asyncio.sleep(0.1)  # Let writer_loop detect the failure

        # Get initial queue size
        initial_size = bus._write_queue.qsize() if bus._write_queue else 0

        # Publish more events - these should NOT be enqueued after storage failure
        for i in range(10):
            await bus.publish(Event(type=EventType.TASK_STARTED, task_key=f"QR-{i}", data={}))

        # Queue should not grow unbounded
        final_size = bus._write_queue.qsize() if bus._write_queue else 0
        assert final_size == initial_size or bus._write_queue is None, "Queue should not grow after storage failure"

    async def test_writer_restarts_after_stop(self, storage):
        """After stop() and start(), writer task should work again and _stopping flag should be reset."""
        bus = EventBus()
        bus.set_storage(storage)

        # First cycle: start, publish, stop
        await bus.start()
        stopping: bool = bus._stopping
        assert not stopping, "Writer should start with _stopping=False"
        await bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-1", data={}))
        await bus.stop()
        stopping = bus._stopping
        assert stopping, "After stop(), _stopping should be True"

        # Second cycle: start again
        await bus.start()
        stopping = bus._stopping
        assert not stopping, "After restart, _stopping should be reset to False"
        assert bus._writer_task is not None and not bus._writer_task.done(), "Writer task should be running"

        await bus.publish(Event(type=EventType.TASK_STARTED, task_key="QR-2", data={}))
        await bus.stop()

        # Both events should be persisted
        events = await storage.load_recent_events()
        assert len(events) == 2
        assert events[0]["task_key"] == "QR-1"
        assert events[1]["task_key"] == "QR-2"

    async def test_cleanup_before_load_prevents_stale_memory(self, storage):
        """cleanup_old_events() should be called before load() to prevent loading expired events into memory."""
        # Create old and recent events in storage
        old_ts = time.time() - 8 * 86400  # 8 days ago
        recent_ts = time.time()
        await storage.save_events_batch(
            [
                ("task_started", "QR-old", "{}", old_ts),
                ("task_started", "QR-recent", "{}", recent_ts),
            ]
        )

        bus = EventBus()
        bus.set_storage(storage)

        # Cleanup should happen before load
        await bus.cleanup_old_events()
        await bus.load()

        # Only recent event should be in memory
        history = bus.get_global_history()
        assert len(history) == 1
        assert history[0].task_key == "QR-recent"
