"""Tests for web module (FastAPI endpoints)."""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.constants import CHAT_CHANNEL_KEY
from orchestrator.event_bus import Event, EventBus


# Mock SDK before importing web (it may transitively import agent_runner)
def _get_app():
    from orchestrator.web import app, configure

    return app, configure


class TestRestEndpoints:
    def test_api_status(self) -> None:
        app, configure = _get_app()
        state = {
            "dispatched": ["QR-1"],
            "active_tasks": ["agent-QR-1"],
            "tracked_prs": {},
            "config": {"queue": "QR", "tag": "ai-task", "max_agents": 2},
        }
        configure(EventBus(), lambda: state)

        client = TestClient(app)
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["dispatched"] == ["QR-1"]
        assert data["config"]["queue"] == "QR"

    def test_api_tasks(self) -> None:
        app, configure = _get_app()
        state = {
            "dispatched": ["QR-1", "QR-2"],
            "active_tasks": ["agent-QR-1"],
            "tracked_prs": {
                "QR-1": {"pr_url": "https://github.com/org/repo/pull/1", "issue_key": "QR-1", "last_check": 0.0},
            },
        }
        configure(EventBus(), lambda: state)

        client = TestClient(app)
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["dispatched"]) == 2
        assert "QR-1" in data["tracked_prs"]

    def test_api_task_detail(self) -> None:
        app, configure = _get_app()
        state = {
            "dispatched": ["QR-1"],
            "active_tasks": ["agent-QR-1"],
            "tracked_prs": {},
        }
        configure(EventBus(), lambda: state)

        client = TestClient(app)
        resp = client.get("/api/tasks/QR-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "QR-1"
        assert data["dispatched"] is True
        assert data["active"] is True

    def test_api_task_detail_not_found(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), lambda: {"dispatched": [], "active_tasks": [], "tracked_prs": {}})

        client = TestClient(app)
        resp = client.get("/api/tasks/QR-999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["dispatched"] is False
        assert data["active"] is False


class TestGlobalEventsEndpoint:
    def test_returns_all_events_sorted(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        configure(event_bus, dict)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            event_bus.publish(Event(type="task_started", task_key="QR-1", data={}, timestamp=100.0))
        )
        loop.run_until_complete(event_bus.publish(Event(type="agent_output", task_key="QR-2", data={}, timestamp=50.0)))
        loop.close()

        client = TestClient(app)
        resp = client.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Should be sorted by timestamp
        assert data[0]["ts"] == 50.0
        assert data[1]["ts"] == 100.0
        assert data[0]["task_key"] == "QR-2"
        assert data[1]["task_key"] == "QR-1"

    def test_returns_empty_when_no_events(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), dict)

        client = TestClient(app)
        resp = client.get("/api/events")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_empty_when_no_event_bus(self) -> None:
        app, configure = _get_app()
        configure(None, dict)

        client = TestClient(app)
        resp = client.get("/api/events")
        assert resp.status_code == 200
        assert resp.json() == []


class TestTaskEventsEndpoint:
    def test_returns_empty_for_unknown_task(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), dict)

        client = TestClient(app)
        resp = client.get("/api/tasks/QR-999/events")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_events_for_task(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        configure(event_bus, dict)

        # Populate history via publish (need async context)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            event_bus.publish(Event(type="task_started", task_key="QR-1", data={"summary": "Test"}))
        )
        loop.run_until_complete(event_bus.publish(Event(type="agent_output", task_key="QR-1", data={"text": "hello"})))
        loop.run_until_complete(event_bus.publish(Event(type="agent_output", task_key="QR-2", data={"text": "other"})))
        loop.close()

        client = TestClient(app)
        resp = client.get("/api/tasks/QR-1/events")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["type"] == "task_started"
        assert data[0]["task_key"] == "QR-1"
        assert data[1]["type"] == "agent_output"
        assert data[1]["data"]["text"] == "hello"

    def test_does_not_leak_events_from_other_tasks(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        configure(event_bus, dict)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(event_bus.publish(Event(type="output", task_key="QR-1", data={"n": 1})))
        loop.run_until_complete(event_bus.publish(Event(type="output", task_key="QR-2", data={"n": 2})))
        loop.close()

        client = TestClient(app)
        # QR-1 should only see its own events
        resp1 = client.get("/api/tasks/QR-1/events")
        events1 = resp1.json()
        assert len(events1) == 1
        assert events1[0]["data"]["n"] == 1

        # QR-2 should only see its own events
        resp2 = client.get("/api/tasks/QR-2/events")
        events2 = resp2.json()
        assert len(events2) == 1
        assert events2[0]["data"]["n"] == 2

    def test_returns_empty_when_no_event_bus(self) -> None:
        app, configure = _get_app()
        configure(None, dict)

        client = TestClient(app)
        resp = client.get("/api/tasks/QR-1/events")
        assert resp.status_code == 200
        assert resp.json() == []


class TestWebSocket:
    def test_task_stream_websocket(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        configure(event_bus, dict)

        client = TestClient(app)
        with client.websocket_connect("/ws/tasks/QR-1/stream") as ws:
            # Publish an event to the bus — need to run in async context
            event = Event(type="agent_output", task_key="QR-1", data={"text": "hello"})
            # Put directly into the subscriber queue
            subs = event_bus._task_subscribers.get("QR-1", [])
            assert len(subs) == 1
            subs[0].put_nowait(event)

            data = ws.receive_json()
            assert data["type"] == "agent_output"
            assert data["data"]["text"] == "hello"
            assert data["task_key"] == "QR-1"

    def test_global_stream_websocket(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        configure(event_bus, dict)

        client = TestClient(app)
        with client.websocket_connect("/ws/events") as ws:
            event = Event(type="task_started", task_key="QR-2", data={"summary": "Test"})
            assert len(event_bus._global_subscribers) == 1
            event_bus._global_subscribers[0].put_nowait(event)

            data = ws.receive_json()
            assert data["type"] == "task_started"
            assert data["task_key"] == "QR-2"
            assert data["data"]["summary"] == "Test"

    def test_task_stream_cleans_up_on_disconnect(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        configure(event_bus, dict)

        client = TestClient(app)
        with client.websocket_connect("/ws/tasks/QR-1/stream"):
            assert len(event_bus._task_subscribers.get("QR-1", [])) == 1

        # After disconnect, subscriber should be cleaned up
        assert len(event_bus._task_subscribers.get("QR-1", [])) == 0

    def test_task_stream_isolation(self) -> None:
        """Events for QR-1 should not appear on QR-2's WebSocket stream."""
        app, configure = _get_app()
        event_bus = EventBus()
        configure(event_bus, dict)

        client = TestClient(app)
        with client.websocket_connect("/ws/tasks/QR-1/stream") as ws1:
            # Verify subscriber is for QR-1
            assert len(event_bus._task_subscribers.get("QR-1", [])) == 1
            assert len(event_bus._task_subscribers.get("QR-2", [])) == 0

            # Push an event for QR-2 — QR-1's queue should NOT receive it
            event_qr2 = Event(type="agent_output", task_key="QR-2", data={"text": "wrong"})
            qr1_subs = event_bus._task_subscribers.get("QR-1", [])
            # Event is for QR-2, so QR-1's queue should remain empty
            assert qr1_subs[0].empty()

            # Push correct event for QR-1
            event_qr1 = Event(type="agent_output", task_key="QR-1", data={"text": "right"})
            qr1_subs[0].put_nowait(event_qr1)

            data = ws1.receive_json()
            assert data["data"]["text"] == "right"
            assert data["task_key"] == "QR-1"

    def test_global_stream_cleans_up_on_disconnect(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        configure(event_bus, dict)

        client = TestClient(app)
        with client.websocket_connect("/ws/events"):
            assert len(event_bus._global_subscribers) == 1

        assert len(event_bus._global_subscribers) == 0

    def test_task_stream_closes_without_event_bus(self) -> None:
        app, configure = _get_app()
        configure(None, dict)

        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect):
            # WebSocket should be closed with 1011 when event_bus is None
            with client.websocket_connect("/ws/tasks/QR-1/stream"):
                pass

    def test_global_stream_closes_without_event_bus(self) -> None:
        app, configure = _get_app()
        configure(None, dict)

        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/events"):
                pass


class TestSendMessage:
    def test_send_message_not_configured(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), lambda: {"tracked_prs": {}, "tracked_needs_info": {}})

        client = TestClient(app)
        resp = client.post("/api/tasks/QR-1/message", json={"text": "hello"})
        assert resp.status_code == 503
        assert "not configured" in resp.json()["error"]

    def test_send_message_no_session_id_returns_404(self) -> None:
        """When send_message raises ValueError (no session_id) → 404."""
        app, configure = _get_app()
        send_fn = AsyncMock(
            side_effect=ValueError("No session_id available for QR-1"),
        )
        configure(EventBus(), dict, send_message=send_fn)

        client = TestClient(app)
        resp = client.post("/api/tasks/QR-1/message", json={"text": "hello"})
        assert resp.status_code == 404
        assert "No session_id" in resp.json()["error"]

    def test_send_message_success(self) -> None:
        app, configure = _get_app()
        send_fn = AsyncMock()
        configure(EventBus(), dict, send_message=send_fn)

        client = TestClient(app)
        resp = client.post(
            "/api/tasks/QR-1/message",
            json={"text": "hello"},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"
        send_fn.assert_awaited_once_with("QR-1", "hello")

    def test_send_message_internal_error_returns_500(self) -> None:
        """When send_message raises unexpected error → 500."""
        app, configure = _get_app()
        send_fn = AsyncMock(
            side_effect=RuntimeError("agent crashed"),
        )
        configure(EventBus(), dict, send_message=send_fn)

        client = TestClient(app)
        resp = client.post(
            "/api/tasks/QR-1/message",
            json={"text": "hello"},
        )
        assert resp.status_code == 500
        assert "Internal error" in resp.json()["error"]


class TestProposals:
    def test_list_proposals(self) -> None:
        app, configure = _get_app()
        configure(
            EventBus(),
            lambda: {
                "proposals": {
                    "p1": {"summary": "First", "created_at": 100.0},
                    "p2": {"summary": "Second", "created_at": 200.0},
                }
            },
        )

        client = TestClient(app)
        resp = client.get("/api/proposals")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Should be sorted by created_at descending
        assert data[0]["summary"] == "Second"
        assert data[1]["summary"] == "First"

    def test_list_proposals_empty(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), lambda: {"proposals": {}})

        client = TestClient(app)
        resp = client.get("/api/proposals")
        assert resp.status_code == 200
        assert resp.json() == []


class TestApproveProposal:
    def test_approve_not_configured(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), dict)

        client = TestClient(app)
        resp = client.post("/api/proposals/p1/approve")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["error"]

    def test_approve_success(self) -> None:
        app, configure = _get_app()

        @dataclass
        class FakeProposal:
            tracker_issue_key: str = "QR-42"

        approve_fn = AsyncMock(return_value=FakeProposal())
        configure(EventBus(), dict, approve_proposal=approve_fn)

        client = TestClient(app)
        resp = client.post("/api/proposals/p1/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert data["issue_key"] == "QR-42"
        approve_fn.assert_called_once_with("p1")

    def test_approve_not_found(self) -> None:
        app, configure = _get_app()
        approve_fn = AsyncMock(side_effect=ValueError("Proposal p1 not found"))
        configure(EventBus(), dict, approve_proposal=approve_fn)

        client = TestClient(app)
        resp = client.post("/api/proposals/p1/approve")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]

    def test_approve_internal_error(self) -> None:
        app, configure = _get_app()
        approve_fn = AsyncMock(side_effect=RuntimeError("Tracker API down"))
        configure(EventBus(), dict, approve_proposal=approve_fn)

        client = TestClient(app)
        resp = client.post("/api/proposals/p1/approve")
        assert resp.status_code == 500
        assert "Tracker API down" in resp.json()["error"]


class TestRejectProposal:
    def test_reject_not_configured(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), dict)

        client = TestClient(app)
        resp = client.post("/api/proposals/p1/reject")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["error"]

    def test_reject_success(self) -> None:
        app, configure = _get_app()
        reject_fn = AsyncMock()
        configure(EventBus(), dict, reject_proposal=reject_fn)

        client = TestClient(app)
        resp = client.post("/api/proposals/p1/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        reject_fn.assert_called_once_with("p1")

    def test_reject_not_found(self) -> None:
        app, configure = _get_app()
        reject_fn = AsyncMock(side_effect=ValueError("Proposal p1 not found"))
        configure(EventBus(), dict, reject_proposal=reject_fn)

        client = TestClient(app)
        resp = client.post("/api/proposals/p1/reject")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]

    def test_reject_internal_error(self) -> None:
        app, configure = _get_app()
        reject_fn = AsyncMock(side_effect=RuntimeError("DB error"))
        configure(EventBus(), dict, reject_proposal=reject_fn)

        client = TestClient(app)
        resp = client.post("/api/proposals/p1/reject")
        assert resp.status_code == 500
        assert "DB error" in resp.json()["error"]


class TestStatsEndpoints:
    def test_stats_summary_not_configured(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), dict, storage=None)

        client = TestClient(app)
        resp = client.get("/api/stats/summary")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["error"].lower()

    def test_stats_summary_with_db(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_summary.return_value = {
            "total_tasks": 5,
            "success_rate": 80.0,
            "total_cost": 2.5,
            "avg_duration": 60.0,
            "days": 7,
        }
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/stats/summary?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_tasks"] == 5
        assert data["success_rate"] == 80.0
        storage.get_summary.assert_called_once_with(days=7)

    def test_stats_costs_not_configured(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), dict, storage=None)

        client = TestClient(app)
        resp = client.get("/api/stats/costs")
        assert resp.status_code == 503

    def test_stats_costs_with_db(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_costs.return_value = [{"group": "sonnet", "total_cost": 3.0, "count": 5}]
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/stats/costs?group_by=model&days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["group"] == "sonnet"

    def test_stats_tasks_with_db(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_recent_tasks.return_value = [{"task_key": "QR-1", "model": "sonnet", "cost_usd": 1.0}]
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/stats/tasks?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["task_key"] == "QR-1"
        storage.get_recent_tasks.assert_called_once_with(limit=10)

    def test_stats_errors_with_db(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_error_stats.return_value = [{"category": "timeout", "count": 3, "retryable_count": 2}]
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/stats/errors?days=14")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["category"] == "timeout"
        storage.get_error_stats.assert_called_once_with(days=14)

    def test_stats_summary_db_error(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_summary.side_effect = RuntimeError("DB broken")
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/stats/summary")
        assert resp.status_code == 500
        assert "error" in resp.json()

    def test_stats_costs_db_error(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_costs.side_effect = RuntimeError("DB broken")
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/stats/costs")
        assert resp.status_code == 500
        assert "error" in resp.json()

    def test_stats_tasks_db_error(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_recent_tasks.side_effect = RuntimeError("DB broken")
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/stats/tasks")
        assert resp.status_code == 500
        assert "error" in resp.json()

    def test_stats_errors_db_error(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_error_stats.side_effect = RuntimeError("DB broken")
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/stats/errors")
        assert resp.status_code == 500
        assert "error" in resp.json()

    def test_task_cost_summary_not_configured(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), dict, storage=None)

        client = TestClient(app)
        resp = client.get("/api/tasks/QR-1/cost-summary")
        assert resp.status_code == 503

    def test_task_cost_summary_db_error(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_task_cost_summary.side_effect = RuntimeError(
            "DB broken",
        )
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/tasks/QR-1/cost-summary")
        assert resp.status_code == 500
        assert "error" in resp.json()

    def test_task_cost_summary_with_db(self) -> None:
        app, configure = _get_app()
        storage = AsyncMock()
        storage.get_task_cost_summary.return_value = {
            "total_cost_usd": 5.25,
            "run_count": 3,
        }
        configure(EventBus(), dict, storage=storage)

        client = TestClient(app)
        resp = client.get("/api/tasks/QR-206/cost-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_cost_usd"] == 5.25
        assert data["run_count"] == 3
        storage.get_task_cost_summary.assert_called_once_with("QR-206")


class TestWsPumpCancelledError:
    """CancelledError in _ws_pump must not propagate during shutdown.

    In Python 3.9+ CancelledError is a BaseException, not Exception.
    The except clause must catch it to avoid noisy tracebacks on shutdown.
    Also, get_task must be cancelled when pump exits via cancellation.
    """

    async def test_ws_pump_cancellation_during_wait(self) -> None:
        """When the pump task is cancelled (server shutdown), it should exit cleanly."""
        from orchestrator.web import _ws_pump

        ws = AsyncMock()

        async def _never_return():
            await asyncio.get_event_loop().create_future()

        ws.receive_text = AsyncMock(side_effect=_never_return)
        ws.send_json = AsyncMock()

        queue: asyncio.Queue = asyncio.Queue()
        # Empty queue — pump blocks at asyncio.wait()

        task = asyncio.create_task(_ws_pump(ws, queue))
        await asyncio.sleep(0.05)
        task.cancel()

        # Pump should handle CancelledError gracefully (not propagate)
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            pytest.fail("_ws_pump should handle CancelledError without propagating")


class TestSupervisorChatEndpoints:
    """Tests for supervisor chat REST and WebSocket endpoints."""

    def _make_chat_manager(self):
        """Create a mock chat manager."""
        from unittest.mock import MagicMock

        from orchestrator.supervisor_chat import ChatMessage, ChatSessionInfo

        cm = MagicMock()
        cm.get_session_info.return_value = ChatSessionInfo(
            session_id="test-session",
            created_at=1000000.0,  # milliseconds
            message_count=2,
            generating=False,
        )
        cm.get_history.return_value = [
            ChatMessage(role="user", content="hello", timestamp=1000000.0),  # milliseconds
            ChatMessage(role="assistant", content="hi there", timestamp=1001000.0),  # milliseconds
        ]
        cm.create_session = AsyncMock(
            return_value=ChatSessionInfo(
                session_id="new-session",
                created_at=2000000.0,  # milliseconds
                message_count=0,
                generating=False,
            )
        )
        cm.close = AsyncMock()
        cm.send = AsyncMock()
        cm.abort = AsyncMock(return_value=True)
        return cm

    def test_create_session(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/chat/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "new-session"
        cm.create_session.assert_called_once()

    def test_create_session_not_configured(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), dict)

        client = TestClient(app)
        resp = client.post("/api/supervisor/chat/session")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["error"]

    def test_get_session_info(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.get("/api/supervisor/chat/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "test-session"
        assert data["message_count"] == 2

    def test_get_session_no_session(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        cm.get_session_info.return_value = None
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.get("/api/supervisor/chat/session")
        assert resp.status_code == 404
        assert resp.json()["session_id"] is None

    def test_delete_session(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.delete("/api/supervisor/chat/session")
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"
        cm.close.assert_called_once()

    def test_get_history(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.get("/api/supervisor/chat/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["role"] == "user"
        assert data[1]["role"] == "assistant"

    def test_send_message_accepted(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/chat/send", json={"text": "analyze QR-1"})
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"
        cm.send.assert_called_once_with("analyze QR-1")

    def test_send_message_no_session(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        cm.get_session_info.return_value = None  # No active session
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/chat/send", json={"text": "hello"})
        assert resp.status_code == 404
        assert "No active" in resp.json()["error"]

    def test_send_message_while_generating_returns_error(self) -> None:
        """Test that concurrent send requests return error instead of silently failing."""
        from orchestrator.supervisor_chat import ChatSessionInfo

        app, configure = _get_app()
        cm = self._make_chat_manager()
        # Simulate "already generating" state
        cm.get_session_info.return_value = ChatSessionInfo(
            session_id="test-session",
            created_at=1000000.0,  # milliseconds
            message_count=2,
            generating=True,  # Already generating
        )
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/chat/send", json={"text": "second message"})
        # Should return error instead of 202 with silent failure
        assert resp.status_code == 400
        assert "already generating" in resp.json()["error"]

    def test_send_message_empty_text_returns_error(self) -> None:
        """Test that empty text returns error instead of silently failing."""
        app, configure = _get_app()
        cm = self._make_chat_manager()
        cm.send = AsyncMock(side_effect=ValueError("Message text must not be empty"))
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/chat/send", json={"text": "   "})
        assert resp.status_code == 400
        assert "empty" in resp.json()["error"].lower()

    def test_abort_generation(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/chat/abort")
        assert resp.status_code == 200
        assert resp.json()["aborted"] is True

    def test_abort_nothing_to_abort(self) -> None:
        app, configure = _get_app()
        cm = self._make_chat_manager()
        cm.abort = AsyncMock(return_value=False)
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/chat/abort")
        assert resp.status_code == 200
        assert resp.json()["aborted"] is False

    def test_ws_supervisor_chat_streams_events(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        cm = self._make_chat_manager()
        configure(event_bus, dict, chat_manager=cm)

        client = TestClient(app)
        with client.websocket_connect("/ws/supervisor/chat") as ws:
            # Verify subscriber was created for supervisor-chat task
            subs = event_bus._task_subscribers.get(CHAT_CHANNEL_KEY, [])
            assert len(subs) == 1

            # Push a chat chunk event
            event = Event(type="supervisor_chat_chunk", task_key=CHAT_CHANNEL_KEY, data={"text": "Hello!"})
            subs[0].put_nowait(event)

            data = ws.receive_json()
            assert data["type"] == "supervisor_chat_chunk"
            assert data["data"]["text"] == "Hello!"

    def test_ws_supervisor_chat_cleans_up(self) -> None:
        app, configure = _get_app()
        event_bus = EventBus()
        cm = self._make_chat_manager()
        configure(event_bus, dict, chat_manager=cm)

        client = TestClient(app)
        with client.websocket_connect("/ws/supervisor/chat"):
            assert len(event_bus._task_subscribers.get(CHAT_CHANNEL_KEY, [])) == 1

        # After disconnect, subscriber should be cleaned up
        assert len(event_bus._task_subscribers.get(CHAT_CHANNEL_KEY, [])) == 0


class TestConfigEndpoint:
    """PUT /api/config updates max_agents at runtime."""

    def test_put_config_updates_max_agents(self) -> None:
        app, configure = _get_app()
        state = {"config": {"queue": "QR", "tag": "ai-task", "max_agents": 2}}
        set_max_agents = __import__("unittest").mock.MagicMock()
        configure(EventBus(), lambda: state, set_max_agents=set_max_agents)

        client = TestClient(app)
        resp = client.put("/api/config", json={"max_agents": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["max_agents"] == 5
        set_max_agents.assert_called_once_with(5)

    def test_put_config_rejects_zero(self) -> None:
        app, configure = _get_app()
        state = {"config": {"queue": "QR", "tag": "ai-task", "max_agents": 2}}
        set_max_agents = __import__("unittest").mock.MagicMock()
        configure(EventBus(), lambda: state, set_max_agents=set_max_agents)

        client = TestClient(app)
        resp = client.put("/api/config", json={"max_agents": 0})
        assert resp.status_code == 400
        set_max_agents.assert_not_called()

    def test_put_config_rejects_negative(self) -> None:
        app, configure = _get_app()
        state = {"config": {"queue": "QR", "tag": "ai-task", "max_agents": 2}}
        set_max_agents = __import__("unittest").mock.MagicMock()
        configure(EventBus(), lambda: state, set_max_agents=set_max_agents)

        client = TestClient(app)
        resp = client.put("/api/config", json={"max_agents": -1})
        assert resp.status_code == 400
        set_max_agents.assert_not_called()

    def test_put_config_not_configured(self) -> None:
        app, configure = _get_app()
        configure(EventBus(), lambda: {"config": {"max_agents": 2}})

        client = TestClient(app)
        resp = client.put("/api/config", json={"max_agents": 5})
        assert resp.status_code == 503
        assert "not configured" in resp.json()["error"].lower()


class TestGetDependencies:
    def test_raises_when_not_configured(self) -> None:
        import orchestrator.web as web_mod

        old = web_mod._dependencies
        try:
            web_mod._dependencies = None
            with pytest.raises(RuntimeError, match="not configured"):
                web_mod.get_dependencies()
        finally:
            web_mod._dependencies = old


class TestSupervisorChannelEndpoints:
    """Tests for multi-channel supervisor REST and WebSocket endpoints."""

    def _make_chat_manager(self):
        """Create a mock chat manager."""
        from unittest.mock import MagicMock

        from orchestrator.supervisor_chat import ChatMessage, ChatSessionInfo

        cm = MagicMock()
        cm.get_session_info.return_value = ChatSessionInfo(
            session_id="test-session",
            created_at=1000000.0,
            message_count=2,
            generating=False,
        )
        cm.get_history.return_value = [
            ChatMessage(role="user", content="hello", timestamp=1000000.0),
        ]
        cm.create_session = AsyncMock(
            return_value=ChatSessionInfo(
                session_id="new-session",
                created_at=2000000.0,
                message_count=0,
                generating=False,
            )
        )
        cm.close = AsyncMock()
        cm.send = AsyncMock()
        cm.abort = AsyncMock(return_value=True)
        return cm

    def test_api_supervisor_channels_returns_metadata(self) -> None:
        """GET /api/supervisor/channels returns list with id/display/available."""
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(event_bus=EventBus(), get_state=dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.get("/api/supervisor/channels")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        ids = [item["id"] for item in data]
        assert "chat" in ids
        assert "tasks" in ids
        assert "heartbeat" in ids
        # The "chat" channel should be available (we passed a chat_manager)
        chat_item = next(item for item in data if item["id"] == "chat")
        assert chat_item["available"] is True
        assert chat_item["display"] == "Чат"
        # tasks and heartbeat should NOT be available (not configured)
        tasks_item = next(item for item in data if item["id"] == "tasks")
        assert tasks_item["available"] is False

    def test_api_channel_session_unknown_channel(self) -> None:
        """Unknown channel returns 404."""
        app, configure = _get_app()
        configure(EventBus(), dict)

        client = TestClient(app)
        resp = client.post("/api/supervisor/channels/bogus/session")
        assert resp.status_code == 404
        assert "Unknown channel" in resp.json()["error"]

    def test_api_channel_session_not_configured(self) -> None:
        """Known channel with no manager returns 503."""
        app, configure = _get_app()
        configure(EventBus(), dict)  # no chat_manager

        client = TestClient(app)
        resp = client.post("/api/supervisor/channels/chat/session")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["error"]

    def test_api_channel_create_session(self) -> None:
        """POST /api/supervisor/channels/chat/session creates session."""
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/channels/chat/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "new-session"
        cm.create_session.assert_called_once()

    def test_api_channel_get_session(self) -> None:
        """GET /api/supervisor/channels/chat/session returns session info."""
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.get("/api/supervisor/channels/chat/session")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "test-session"

    def test_api_channel_delete_session(self) -> None:
        """DELETE /api/supervisor/channels/chat/session closes session."""
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.delete("/api/supervisor/channels/chat/session")
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"
        cm.close.assert_called_once()

    def test_api_channel_history(self) -> None:
        """GET /api/supervisor/channels/chat/history returns messages."""
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.get("/api/supervisor/channels/chat/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["role"] == "user"

    def test_api_channel_send_accepted(self) -> None:
        """POST /api/supervisor/channels/chat/send returns 202."""
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/channels/chat/send", json={"text": "hello"})
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_api_channel_abort(self) -> None:
        """POST /api/supervisor/channels/chat/abort returns aborted status."""
        app, configure = _get_app()
        cm = self._make_chat_manager()
        configure(EventBus(), dict, chat_manager=cm)

        client = TestClient(app)
        resp = client.post("/api/supervisor/channels/chat/abort")
        assert resp.status_code == 200
        assert resp.json()["aborted"] is True

    def test_ws_supervisor_channel_chat(self) -> None:
        """WebSocket /ws/supervisor/channels/chat streams events."""
        app, configure = _get_app()
        event_bus = EventBus()
        cm = self._make_chat_manager()
        configure(event_bus, dict, chat_manager=cm)

        client = TestClient(app)
        with client.websocket_connect("/ws/supervisor/channels/chat") as ws:
            subs = event_bus._task_subscribers.get(CHAT_CHANNEL_KEY, [])
            assert len(subs) == 1

            event = Event(
                type="supervisor_chat_chunk",
                task_key=CHAT_CHANNEL_KEY,
                data={"text": "Hi!"},
            )
            subs[0].put_nowait(event)

            data = ws.receive_json()
            assert data["type"] == "supervisor_chat_chunk"
            assert data["data"]["text"] == "Hi!"

    def test_ws_supervisor_channel_unknown_closes_with_1008(self) -> None:
        """Unknown channel WS closes with code 1008."""
        from starlette.websockets import WebSocketDisconnect

        app, configure = _get_app()
        configure(EventBus(), dict)

        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/supervisor/channels/bogus"):
                pass

    def test_configure_chat_managers_dict(self) -> None:
        """configure() accepts chat_managers dict and routes correctly."""
        app, configure = _get_app()
        chat_cm = self._make_chat_manager()
        tasks_cm = self._make_chat_manager()
        configure(
            EventBus(),
            dict,
            chat_managers={"chat": chat_cm, "tasks": tasks_cm},
        )

        client = TestClient(app)
        resp = client.get("/api/supervisor/channels")
        assert resp.status_code == 200
        data = resp.json()
        available = {item["id"]: item["available"] for item in data}
        assert available["chat"] is True
        assert available["tasks"] is True
        assert available["heartbeat"] is False
