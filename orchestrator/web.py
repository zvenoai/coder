"""FastAPI web server for the AI Swarm dashboard."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import uvicorn
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from orchestrator.constants import CHANNEL_META, ChannelId
from orchestrator.event_bus import EventBus
from orchestrator.metrics import AGENTS_RUNNING, EPICS_ACTIVE, PRS_TRACKED, REGISTRY

if TYPE_CHECKING:
    from orchestrator.config import Config
    from orchestrator.storage import Storage

logger = logging.getLogger(__name__)

app = FastAPI(title="AI Swarm Dashboard")

# Keep strong references to background tasks to prevent garbage collection
_background_tasks: set[asyncio.Task] = set()


class Dependencies:
    """Container for web app dependencies (FastAPI DI pattern)."""

    def __init__(
        self,
        event_bus: EventBus | None,
        get_state: Callable[[], dict[str, Any]],
        send_message: Callable[[str, str], Awaitable[None]] | None = None,
        approve_proposal: Callable[[str], Awaitable[Any]] | None = None,
        reject_proposal: Callable[[str], Awaitable[Any]] | None = None,
        storage: Storage | None = None,
        chat_managers: dict[ChannelId, Any] | None = None,
        set_max_agents: Callable[[int], None] | None = None,
        alertmanager_webhook_enabled: bool = False,
        tracker_client: Any = None,
        auto_create_config: dict[str, Any] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.get_state = get_state
        self.send_message = send_message
        self.approve_proposal = approve_proposal
        self.reject_proposal = reject_proposal
        self.storage = storage
        self.chat_managers: dict[ChannelId, Any] = chat_managers or {}
        self.set_max_agents = set_max_agents
        self.alertmanager_webhook_enabled = alertmanager_webhook_enabled
        self.tracker_client = tracker_client
        self.auto_create_config = auto_create_config

    @property
    def chat_manager(self) -> Any:
        """Backwards-compat property — returns the 'chat' channel manager."""
        return self.chat_managers.get("chat")


_dependencies: Dependencies | None = None


def get_dependencies() -> Dependencies:
    """FastAPI dependency provider.

    Raises:
        RuntimeError: If dependencies not configured via configure()
    """
    if _dependencies is None:
        raise RuntimeError("Dependencies not configured. Call configure() first.")
    return _dependencies


def configure(
    event_bus: EventBus | None,
    get_state: Callable[[], dict[str, Any]],
    send_message: Callable[[str, str], Awaitable[None]] | None = None,
    approve_proposal: Callable[[str], Awaitable[Any]] | None = None,
    reject_proposal: Callable[[str], Awaitable[Any]] | None = None,
    storage: Storage | None = None,
    chat_manager: Any | None = None,
    set_max_agents: Callable[[int], None] | None = None,
    chat_managers: dict[ChannelId, Any] | None = None,
    alertmanager_webhook_enabled: bool = False,
    tracker_client: Any = None,
    auto_create_config: dict[str, Any] | None = None,
) -> None:
    """Configure web app dependencies.

    Accepts either legacy ``chat_manager`` (mapped to ``{"chat": ...}``)
    or the new ``chat_managers`` dict. If both are provided, ``chat_managers``
    takes precedence.
    """
    global _dependencies
    resolved_managers: dict[ChannelId, Any]
    if chat_managers is not None:
        resolved_managers = chat_managers
    elif chat_manager is not None:
        resolved_managers = {"chat": chat_manager}
    else:
        resolved_managers = {}
    _dependencies = Dependencies(
        event_bus=event_bus,
        get_state=get_state,
        send_message=send_message,
        approve_proposal=approve_proposal,
        reject_proposal=reject_proposal,
        storage=storage,
        chat_managers=resolved_managers,
        set_max_agents=set_max_agents,
        alertmanager_webhook_enabled=alertmanager_webhook_enabled,
        tracker_client=tracker_client,
        auto_create_config=auto_create_config,
    )


# ---- REST endpoints ----


@app.get("/api/status")
async def api_status(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return orchestrator state snapshot."""
    return JSONResponse(deps.get_state())


class _ConfigUpdateBody(BaseModel):
    max_agents: int | None = None


@app.put("/api/config")
async def api_update_config(body: _ConfigUpdateBody, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Update runtime config (e.g. max concurrent agents). Returns current value."""
    if body.max_agents is not None:
        if body.max_agents < 1:
            return JSONResponse(
                {"error": "max_agents must be at least 1"},
                status_code=400,
            )
        if deps.set_max_agents is None:
            return JSONResponse(
                {"error": "set_max_agents not configured"},
                status_code=503,
            )
        deps.set_max_agents(body.max_agents)
        return JSONResponse({"max_agents": body.max_agents})
    current = deps.get_state().get("config", {}).get("max_agents")
    return JSONResponse({"max_agents": current})


@app.get("/api/tasks")
async def api_tasks(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return dispatched tasks with details."""
    state = deps.get_state()
    return JSONResponse(
        {
            "dispatched": state.get("dispatched", []),
            "active_tasks": state.get("active_tasks", []),
            "tracked_prs": state.get("tracked_prs", {}),
        }
    )


@app.get("/api/tasks/{key}")
async def api_task_detail(key: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return single task details."""
    state = deps.get_state()
    pr_info = state.get("tracked_prs", {}).get(key)
    return JSONResponse(
        {
            "key": key,
            "dispatched": key in state.get("dispatched", []),
            "active": f"agent-{key}" in state.get("active_tasks", []),
            "pr": pr_info,
        }
    )


@app.get("/api/events")
async def api_events(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return global event history across all tasks."""
    if deps.event_bus is None:
        return JSONResponse([])
    events = deps.event_bus.get_global_history()
    return JSONResponse(
        [
            {
                "type": e.type,
                "task_key": e.task_key,
                "data": e.data,
                "ts": e.timestamp,
            }
            for e in events
        ]
    )


@app.get("/api/tasks/{key}/cost-summary")
async def api_task_cost_summary(
    key: str,
    deps: Dependencies = Depends(get_dependencies),
) -> JSONResponse:
    """Return total cost and run count for a task."""
    if deps.storage is None:
        return JSONResponse(
            {"error": "Stats not configured"},
            status_code=503,
        )
    try:
        summary = await deps.storage.get_task_cost_summary(key)
        return JSONResponse(summary)
    except Exception:
        logger.exception(
            "Failed to fetch cost summary for %s",
            key,
        )
        return JSONResponse(
            {"error": "Internal error"},
            status_code=500,
        )


@app.get("/api/tasks/{key}/events")
async def api_task_events(key: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return historical events for a specific task."""
    if deps.event_bus is None:
        return JSONResponse([])
    events = deps.event_bus.get_task_history(key)
    return JSONResponse(
        [
            {
                "type": e.type,
                "task_key": e.task_key,
                "data": e.data,
                "ts": e.timestamp,
            }
            for e in events
        ]
    )


class _MessageBody(BaseModel):
    text: str


@app.post("/api/tasks/{key}/message")
async def api_send_message(
    key: str, body: _MessageBody, deps: Dependencies = Depends(get_dependencies)
) -> JSONResponse:
    """Send a message to a running agent session.

    Awaits send_message so that ValueError (no session) is caught
    and returned as 404 immediately.  The LLM response time is
    unavoidable — the caller should use a generous timeout.
    """
    if deps.send_message is None:
        return JSONResponse(
            {"error": "send_message not configured"},
            status_code=503,
        )

    try:
        await deps.send_message(key, body.text)
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=404,
        )
    except Exception:
        logger.exception("Error sending message to %s", key)
        return JSONResponse(
            {"error": "Internal error sending message"},
            status_code=500,
        )
    return JSONResponse({"status": "accepted"}, status_code=202)


# ---- Proposal endpoints ----


@app.get("/api/proposals")
async def api_proposals(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return all proposals sorted by created_at descending."""
    state = deps.get_state()
    proposals = list(state.get("proposals", {}).values())
    proposals.sort(key=lambda p: p.get("created_at", 0), reverse=True)
    return JSONResponse(proposals)


@app.post("/api/proposals/{proposal_id}/approve")
async def api_approve_proposal(proposal_id: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Approve a proposal — creates a Tracker issue."""
    if deps.approve_proposal is None:
        return JSONResponse({"error": "approve_proposal not configured"}, status_code=503)
    try:
        proposal = await deps.approve_proposal(proposal_id)
        return JSONResponse(
            {
                "status": "approved",
                "issue_key": proposal.tracker_issue_key,
            }
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.exception("Failed to approve proposal %s", proposal_id)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/proposals/{proposal_id}/reject")
async def api_reject_proposal(proposal_id: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Reject a proposal."""
    if deps.reject_proposal is None:
        return JSONResponse({"error": "reject_proposal not configured"}, status_code=503)
    try:
        await deps.reject_proposal(proposal_id)
        return JSONResponse({"status": "rejected"})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.exception("Failed to reject proposal %s", proposal_id)
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Supervisor chat endpoints ----


@app.post("/api/supervisor/chat/session")
async def api_chat_create_session(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Create or recreate a supervisor chat session."""
    if deps.chat_manager is None:
        return JSONResponse({"error": "Supervisor chat not configured"}, status_code=503)
    try:
        info = await deps.chat_manager.create_session()
        return JSONResponse(dataclasses.asdict(info))
    except Exception as e:
        logger.exception("Failed to create chat session")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/supervisor/chat/session")
async def api_chat_get_session(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Get current chat session info."""
    if deps.chat_manager is None:
        return JSONResponse({"error": "Supervisor chat not configured"}, status_code=503)
    info = deps.chat_manager.get_session_info()
    if info is None:
        return JSONResponse({"session_id": None}, status_code=404)
    return JSONResponse(dataclasses.asdict(info))


@app.delete("/api/supervisor/chat/session")
async def api_chat_delete_session(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Close the current chat session."""
    if deps.chat_manager is None:
        return JSONResponse({"error": "Supervisor chat not configured"}, status_code=503)
    await deps.chat_manager.close()
    return JSONResponse({"status": "closed"})


@app.get("/api/supervisor/chat/history")
async def api_chat_history(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Get chat message history."""
    if deps.chat_manager is None:
        return JSONResponse({"error": "Supervisor chat not configured"}, status_code=503)
    history = deps.chat_manager.get_history()
    return JSONResponse([dataclasses.asdict(msg) for msg in history])


class _ChatMessageBody(BaseModel):
    text: str


@app.post("/api/supervisor/chat/send")
async def api_chat_send(body: _ChatMessageBody, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Send a message to the supervisor chat (async, returns 202 immediately)."""
    if deps.chat_manager is None:
        return JSONResponse({"error": "Supervisor chat not configured"}, status_code=503)

    # Validate text before starting background task
    if not body.text.strip():
        return JSONResponse({"error": "Message text must not be empty"}, status_code=400)

    # Check for active session and not already generating
    session_info = deps.chat_manager.get_session_info()
    if session_info is None:
        return JSONResponse({"error": "No active chat session"}, status_code=404)
    if session_info.generating:
        return JSONResponse({"error": "already generating — wait or call abort()"}, status_code=400)

    # Start generation in background, return immediately
    asyncio.create_task(deps.chat_manager.send(body.text))
    return JSONResponse({"status": "accepted"}, status_code=202)


@app.post("/api/supervisor/chat/abort")
async def api_chat_abort(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Abort current chat generation."""
    if deps.chat_manager is None:
        return JSONResponse({"error": "Supervisor chat not configured"}, status_code=503)
    aborted = await deps.chat_manager.abort()
    return JSONResponse({"aborted": aborted})


async def _try_auto_create_tasks(
    payload: Any,
    tracker_client: Any,
    config: dict[str, Any],
) -> None:
    """Create Tracker Bug tasks for critical/error alerts.

    Checks for duplicates before creating. Logs errors instead
    of raising to maintain fail-open behavior.
    """
    from orchestrator.alertmanager_webhook import (
        AUTO_CREATE_SEVERITIES,
        build_issue_description,
        build_issue_summary,
        map_component,
    )

    queue = config["queue"]
    tag = config["tag"]
    project_id = config.get("project_id")
    boards = config.get("boards")

    for alert in payload.alerts:
        severity = alert.labels.get("severity", "")
        if severity not in AUTO_CREATE_SEVERITIES:
            continue

        alertname = alert.labels.get("alertname", "unknown")
        # Sanitize alertname for safe Tracker query interpolation
        safe_alertname = alertname.replace('"', "").replace("\\", "")

        try:
            # Check for existing open issue with same alertname
            query = f'"Queue": "{queue}" AND Summary: "[Alert] {safe_alertname}" AND Resolution: unresolved()'
            existing = await asyncio.to_thread(
                tracker_client.search,
                query,
            )
            if existing:
                logger.info(
                    "Skipping auto-create for %s: duplicate %s exists",
                    alertname,
                    existing[0].key,
                )
                continue

            summary = build_issue_summary(alert)
            description = build_issue_description(alert)
            component = map_component(alert)

            result = await asyncio.to_thread(
                tracker_client.create_issue,
                queue,
                summary,
                description,
                issue_type=1,  # Bug
                components=[component],
                project_id=project_id,
                boards=boards,
                tags=[tag],
            )
            logger.info(
                "Auto-created task %s for alert %s",
                result.get("key", "?"),
                alertname,
            )
        except Exception:
            logger.exception(
                "Failed to auto-create task for alert %s",
                alertname,
            )


@app.post("/webhook/alertmanager")
async def webhook_alertmanager(
    request: Request,
    deps: Dependencies = Depends(get_dependencies),
) -> JSONResponse:
    """Receive Alertmanager webhook payload (fail-open)."""
    if not deps.alertmanager_webhook_enabled:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        raw = await request.json()
    except Exception:
        logger.warning("Alertmanager webhook: malformed JSON body")
        return JSONResponse({"status": "ok"})
    try:
        from orchestrator.alertmanager_webhook import (
            format_alert_prompt,
            parse_payload,
        )

        payload = parse_payload(raw)
        for alert in payload.alerts:
            logger.info(
                "Alertmanager alert: %s severity=%s namespace=%s",
                alert.labels.get("alertname", "unknown"),
                alert.labels.get("severity", "unknown"),
                alert.labels.get("namespace", "unknown"),
            )
        if payload.alerts and deps.chat_manager is not None:
            prompt = format_alert_prompt(payload)
            chat_manager = deps.chat_manager

            # Wrap auto_send in exception handler (fire-and-forget pattern)
            async def _safe_auto_send() -> None:
                try:
                    await chat_manager.auto_send(prompt)
                except Exception:
                    logger.warning(
                        "Failed to send Alertmanager alert to supervisor",
                        exc_info=True,
                    )

            task = asyncio.create_task(_safe_auto_send())
            # Keep reference to prevent garbage collection
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Auto-create Tracker tasks for critical/error alerts
        ac_config = deps.auto_create_config
        if payload.alerts and ac_config is not None and ac_config.get("enabled") and deps.tracker_client is not None:
            auto_task = asyncio.create_task(
                _try_auto_create_tasks(
                    payload,
                    deps.tracker_client,
                    ac_config,
                )
            )
            _background_tasks.add(auto_task)
            auto_task.add_done_callback(_background_tasks.discard)
    except Exception:
        logger.exception("Alertmanager webhook processing error")
    return JSONResponse({"status": "ok"})


# ---- Stats endpoints ----


@app.get("/api/stats/summary")
async def api_stats_summary(days: int = 7, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return summary statistics for the given time window."""
    if deps.storage is None:
        return JSONResponse({"error": "Stats not configured"}, status_code=503)
    try:
        summary = await deps.storage.get_summary(days=days)
        return JSONResponse(summary)
    except Exception as e:
        logger.exception("Failed to fetch stats summary")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats/costs")
async def api_stats_costs(
    group_by: str = "model", days: int = 7, deps: Dependencies = Depends(get_dependencies)
) -> JSONResponse:
    """Return cost breakdown grouped by model or day."""
    if deps.storage is None:
        return JSONResponse({"error": "Stats not configured"}, status_code=503)
    try:
        costs = await deps.storage.get_costs(group_by=group_by, days=days)
        return JSONResponse(costs)
    except Exception as e:
        logger.exception("Failed to fetch stats costs")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats/tasks")
async def api_stats_tasks(limit: int = 20, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return most recent task runs."""
    if deps.storage is None:
        return JSONResponse({"error": "Stats not configured"}, status_code=503)
    try:
        tasks = await deps.storage.get_recent_tasks(limit=limit)
        return JSONResponse(tasks)
    except Exception as e:
        logger.exception("Failed to fetch stats tasks")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats/errors")
async def api_stats_errors(days: int = 7, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return error statistics aggregated by category."""
    if deps.storage is None:
        return JSONResponse({"error": "Stats not configured"}, status_code=503)
    try:
        errors = await deps.storage.get_error_stats(days=days)
        return JSONResponse(errors)
    except Exception as e:
        logger.exception("Failed to fetch stats errors")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Metrics endpoint ----


@app.get("/metrics")
async def metrics_endpoint(deps: Dependencies = Depends(get_dependencies)) -> Response:
    """Return Prometheus metrics in text format for VictoriaMetrics scraping."""
    # Get current orchestrator state
    state = deps.get_state()

    # Update gauge metrics from current state
    AGENTS_RUNNING.set(len(state.get("active_tasks", [])))
    PRS_TRACKED.set(len(state.get("tracked_prs", {})))
    EPICS_ACTIVE.set(len(state.get("epics", {})))

    # Render all metrics in Prometheus text format
    metrics_text = REGISTRY.render()

    return Response(
        content=metrics_text,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ---- WebSocket helpers ----


def _serialize_event(event) -> dict:
    return {
        "type": event.type,
        "task_key": event.task_key,
        "data": event.data,
        "ts": event.timestamp,
    }


async def _ws_pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    """Forward events from queue to WebSocket until client disconnects.

    Uses a receive task to detect disconnect — without it the handler
    would block on queue.get() and never notice the client left.
    """

    async def _wait_disconnect():
        try:
            await ws.receive_text()
        except WebSocketDisconnect:
            pass

    disconnect_task = asyncio.create_task(_wait_disconnect())
    get_task: asyncio.Task | None = None
    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {get_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                get_task.cancel()
                get_task = None
                return
            event = get_task.result()
            get_task = None
            await ws.send_json(_serialize_event(event))
    except (WebSocketDisconnect, asyncio.CancelledError, Exception):
        pass
    finally:
        disconnect_task.cancel()
        if get_task is not None:
            get_task.cancel()


# ---- WebSocket: per-task agent output stream ----


@app.websocket("/ws/tasks/{key}/stream")
async def task_stream(ws: WebSocket, key: str, deps: Dependencies = Depends(get_dependencies)) -> None:
    """Stream agent output for a specific task."""
    if deps.event_bus is None:
        await ws.close(code=1011, reason="Event bus not configured")
        return

    await ws.accept()
    queue = deps.event_bus.subscribe_task(key)
    try:
        await _ws_pump(ws, queue)
    except Exception:
        logger.debug("WebSocket error for task %s", key, exc_info=True)
    finally:
        deps.event_bus.unsubscribe_task(key, queue)


# ---- WebSocket: global orchestrator events ----


@app.websocket("/ws/events")
async def global_stream(ws: WebSocket, deps: Dependencies = Depends(get_dependencies)) -> None:
    """Stream all orchestrator events."""
    if deps.event_bus is None:
        await ws.close(code=1011, reason="Event bus not configured")
        return

    await ws.accept()
    queue = deps.event_bus.subscribe_global()
    try:
        await _ws_pump(ws, queue)
    except Exception:
        logger.debug("WebSocket error on global stream", exc_info=True)
    finally:
        deps.event_bus.unsubscribe_global(queue)


# ---- WebSocket: supervisor chat stream (backwards compat) ----


@app.websocket("/ws/supervisor/chat")
async def ws_supervisor_chat(ws: WebSocket, deps: Dependencies = Depends(get_dependencies)) -> None:
    """Stream supervisor chat events — backwards-compat alias for /ws/supervisor/channels/chat."""
    if deps.event_bus is None:
        await ws.close(code=1011, reason="Event bus not configured")
        return

    task_key = CHANNEL_META["chat"]["task_key"]
    await ws.accept()
    queue = deps.event_bus.subscribe_task(task_key)
    try:
        await _ws_pump(ws, queue)
    except Exception:
        logger.debug("WebSocket error on supervisor chat stream", exc_info=True)
    finally:
        deps.event_bus.unsubscribe_task(task_key, queue)


# ---- Supervisor channel metadata endpoint ----


@app.get("/api/supervisor/channels")
async def api_supervisor_channels(deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Return metadata for all supervisor channels."""
    result = [
        {
            "id": channel_id,
            "display": meta["display"],
            "task_key": meta["task_key"],
            "available": channel_id in deps.chat_managers,
        }
        for channel_id, meta in CHANNEL_META.items()
    ]
    return JSONResponse(result)


# ---- Per-channel supervisor REST endpoints ----


def _get_channel_manager(
    channel: str,
    deps: Dependencies,
) -> tuple[Any, JSONResponse | None]:
    """Look up channel manager, returning (manager, None) or (None, error_response)."""
    if channel not in CHANNEL_META:
        return None, JSONResponse(
            {"error": f"Unknown channel: {channel}"},
            status_code=404,
        )
    # cast: channel already validated against CHANNEL_META keys above
    manager = deps.chat_managers.get(cast(ChannelId, channel))
    if manager is None:
        return None, JSONResponse(
            {"error": f"Channel '{channel}' not configured"},
            status_code=503,
        )
    return manager, None


@app.post("/api/supervisor/channels/{channel}/session")
async def api_channel_create_session(channel: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Create or recreate a supervisor session for the given channel."""
    manager, err = _get_channel_manager(channel, deps)
    if err is not None:
        return err
    try:
        info = await manager.create_session()
        return JSONResponse(dataclasses.asdict(info))
    except Exception as e:
        logger.exception("Failed to create session for channel %s", channel)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/supervisor/channels/{channel}/session")
async def api_channel_get_session(channel: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Get current session info for the given channel."""
    manager, err = _get_channel_manager(channel, deps)
    if err is not None:
        return err
    info = manager.get_session_info()
    if info is None:
        return JSONResponse({"error": "No active session"}, status_code=404)
    return JSONResponse(dataclasses.asdict(info))


@app.delete("/api/supervisor/channels/{channel}/session")
async def api_channel_delete_session(channel: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Close the current session for the given channel."""
    manager, err = _get_channel_manager(channel, deps)
    if err is not None:
        return err
    await manager.close()
    return JSONResponse({"status": "closed"})


@app.get("/api/supervisor/channels/{channel}/history")
async def api_channel_history(channel: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Get message history for the given channel."""
    manager, err = _get_channel_manager(channel, deps)
    if err is not None:
        return err
    history = manager.get_history()
    return JSONResponse([dataclasses.asdict(msg) for msg in history])


@app.post("/api/supervisor/channels/{channel}/send")
async def api_channel_send(
    channel: str, body: _ChatMessageBody, deps: Dependencies = Depends(get_dependencies)
) -> JSONResponse:
    """Send a message to the given channel (async, returns 202 immediately)."""
    manager, err = _get_channel_manager(channel, deps)
    if err is not None:
        return err

    if not body.text.strip():
        return JSONResponse({"error": "Message text must not be empty"}, status_code=400)

    session_info = manager.get_session_info()
    if session_info is None:
        return JSONResponse({"error": "No active chat session"}, status_code=404)
    if session_info.generating:
        return JSONResponse(
            {"error": "already generating — wait or call abort()"},
            status_code=400,
        )

    asyncio.create_task(manager.send(body.text))
    return JSONResponse({"status": "accepted"}, status_code=202)


@app.post("/api/supervisor/channels/{channel}/abort")
async def api_channel_abort(channel: str, deps: Dependencies = Depends(get_dependencies)) -> JSONResponse:
    """Abort current generation for the given channel."""
    manager, err = _get_channel_manager(channel, deps)
    if err is not None:
        return err
    aborted = await manager.abort()
    return JSONResponse({"aborted": aborted})


# ---- WebSocket: per-channel supervisor stream ----


@app.websocket("/ws/supervisor/channels/{channel}")
async def ws_supervisor_channel(ws: WebSocket, channel: str, deps: Dependencies = Depends(get_dependencies)) -> None:
    """Stream supervisor events for a specific channel."""
    if channel not in CHANNEL_META:
        await ws.close(code=1008, reason=f"Unknown channel: {channel}")
        return

    if deps.event_bus is None:
        await ws.close(code=1011, reason="Event bus not configured")
        return

    # cast: channel already validated against CHANNEL_META keys above
    task_key = CHANNEL_META[cast(ChannelId, channel)]["task_key"]
    await ws.accept()
    queue = deps.event_bus.subscribe_task(task_key)
    try:
        await _ws_pump(ws, queue)
    except Exception:
        logger.debug("WebSocket error on supervisor channel %s", channel, exc_info=True)
    finally:
        deps.event_bus.unsubscribe_task(task_key, queue)


# ---- Static files (frontend) ----

_FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")


async def start_web_server(
    config: Config,
    event_bus: EventBus,
    orchestrator: Any,
    storage: Storage | None = None,
    # TODO: remove legacy chat_manager once all callers pass chat_managers directly
    chat_manager: Any | None = None,
    chat_managers: dict[ChannelId, Any] | None = None,
    tracker_client: Any = None,
) -> None:
    """Start the FastAPI server programmatically inside the asyncio loop."""
    send_fn = getattr(orchestrator, "send_message_to_agent", None)
    approve_fn = getattr(orchestrator, "approve_proposal", None)
    reject_fn = getattr(orchestrator, "reject_proposal", None)
    set_max_agents_fn = getattr(orchestrator, "set_max_agents", None)

    # Build auto-create config if enabled
    auto_create_cfg: dict[str, Any] | None = None
    if config.alertmanager_auto_create_task:
        auto_create_cfg = {
            "enabled": True,
            "queue": config.alertmanager_auto_task_queue,
            "tag": config.alertmanager_auto_task_tag,
            "project_id": config.tracker_project_id,
            "boards": config.tracker_boards,
        }

    configure(
        event_bus,
        orchestrator.get_state,
        send_message=send_fn,
        approve_proposal=approve_fn,
        reject_proposal=reject_fn,
        storage=storage,
        chat_manager=chat_manager,
        set_max_agents=set_max_agents_fn,
        chat_managers=chat_managers,
        alertmanager_webhook_enabled=config.alertmanager_webhook_enabled,
        tracker_client=tracker_client,
        auto_create_config=auto_create_cfg,
    )

    server_config = uvicorn.Config(
        app,
        host=config.web_host,
        port=config.web_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(server_config)
    logger.info("Starting web dashboard on %s:%d", config.web_host, config.web_port)
    await server.serve()
