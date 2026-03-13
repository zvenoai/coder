"""In-process MCP tools for the Orchestrator agent — result handling, epic, and classification."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool

from orchestrator.constants import EventType
from orchestrator.event_bus import Event

if TYPE_CHECKING:
    from orchestrator.agent_runner import AgentSession
    from orchestrator.config import Config
    from orchestrator.event_bus import EventBus
    from orchestrator.needs_info_monitor import NeedsInfoMonitor
    from orchestrator.pr_monitor import PRMonitor
    from orchestrator.recovery import RecoveryManager
    from orchestrator.tracker_client import TrackerClient
    from orchestrator.tracker_tools import ToolState
    from orchestrator.workspace_tools import WorkspaceState

logger = logging.getLogger(__name__)

_SERVER_NAME = "orchestrator"
_SERVER_VERSION = "1.0.0"
_DEFAULT_RECENT_EVENTS_COUNT = 50


# ---------------------------------------------------------------------------
# Implementation functions (testable without MCP wrapper)
# ---------------------------------------------------------------------------


async def track_pr_impl(
    *,
    issue_key: str,
    pr_url: str,
    session: AgentSession,
    workspace_state: WorkspaceState,
    event_bus: EventBus,
    tracker: TrackerClient,
    pr_monitor: PRMonitor,
    recovery: RecoveryManager,
    issue_summary: str | None = None,
    cost_usd: float | None = None,
    duration_seconds: float | None = None,
    resumed: bool = False,
) -> dict[str, str]:
    """Track a PR for review monitoring. Terminal action."""
    recovery.clear(issue_key)
    try:
        await asyncio.to_thread(tracker.transition_to_review, issue_key)
    except requests.RequestException:
        logger.warning("Failed to transition %s to review", issue_key)
    session_id = session.session_id
    pr_monitor.track(issue_key, pr_url, session, workspace_state, issue_summary)
    data: dict[str, object] = {
        "pr_url": pr_url,
        "cost": cost_usd,
        "duration": duration_seconds,
        "session_id": session_id,
    }
    if resumed:
        data["resumed"] = True
    await event_bus.publish(Event(type=EventType.PR_TRACKED, task_key=issue_key, data=data))
    return {"status": "tracking", "pr_url": pr_url}


async def monitor_needs_info_impl(
    *,
    issue_key: str,
    session: AgentSession,
    workspace_state: WorkspaceState,
    tool_state: ToolState,
    event_bus: EventBus,
    needs_info_monitor: NeedsInfoMonitor,
    tracker: TrackerClient,
    issue_summary: str | None = None,
) -> dict[str, str]:
    """Start needs-info monitoring. Terminal action."""
    from orchestrator.needs_info_monitor import TrackedNeedsInfo

    try:
        comments = await asyncio.to_thread(tracker.get_comments, issue_key)
        last_comment_id = max((c.get("id", 0) for c in comments), default=0)
    except requests.RequestException:
        last_comment_id = 0

    needs_info_monitor.add(
        TrackedNeedsInfo(
            issue_key=issue_key,
            session=session,
            workspace_state=workspace_state,
            tool_state=tool_state,
            last_check_at=time.monotonic(),
            last_seen_comment_id=last_comment_id,
            issue_summary=issue_summary or "",
        )
    )
    await event_bus.publish(
        Event(
            type=EventType.NEEDS_INFO,
            task_key=issue_key,
            data={"text": tool_state.needs_info_text},
        )
    )
    return {"status": "monitoring_needs_info"}


async def complete_task_impl(
    *,
    issue_key: str,
    summary: str,
    session: AgentSession,
    workspace_state: WorkspaceState,
    event_bus: EventBus,
    cleanup_worktrees_callback: Callable[[str, list[Path]], None],
) -> dict[str, str]:
    """Mark task as successfully completed without PR. Terminal action."""
    session_id = session.session_id
    await session.close()
    cleanup_worktrees_callback(issue_key, workspace_state.repo_paths)

    data: dict[str, object] = {
        "summary": summary,
        "session_id": session_id,
        "has_pr": False,
    }

    await event_bus.publish(
        Event(
            type=EventType.TASK_COMPLETED,
            task_key=issue_key,
            data=data,
        )
    )
    return {"status": "completed"}


async def fail_task_impl(
    *,
    issue_key: str,
    error: str,
    session: AgentSession,
    workspace_state: WorkspaceState,
    event_bus: EventBus,
    recovery: RecoveryManager,
    cleanup_worktrees_callback: Callable[[str, list[Path]], None],
    skip_record: bool = False,
    cost_usd: float | None = None,
    duration_seconds: float | None = None,
) -> dict[str, str]:
    """Record permanent failure. Terminal action.

    Args:
        skip_record: If True, skip recovery.record_failure (caller already recorded).
    """
    if not skip_record:
        recovery.record_failure(issue_key, error)
    session_id = session.session_id
    await session.close()
    cleanup_worktrees_callback(issue_key, workspace_state.repo_paths)
    await event_bus.publish(
        Event(
            type=EventType.TASK_FAILED,
            task_key=issue_key,
            data={
                "error": error[:500],
                "retryable": False,
                "cost": cost_usd,
                "duration": duration_seconds,
                "session_id": session_id,
            },
        )
    )
    return {"status": "failed"}


async def create_follow_up_task_impl(
    *,
    summary: str,
    description: str,
    component: str,
    assignee: str,
    tracker: TrackerClient,
    config: Config,
) -> str:
    """Create a follow-up task in Tracker with ai-task tag. Returns issue key."""
    result = await asyncio.to_thread(
        lambda: tracker.create_issue(
            queue=config.tracker_queue,
            summary=summary,
            description=description,
            components=[component],
            assignee=assignee,
            project_id=config.tracker_project_id,
            boards=config.tracker_boards,
            tags=[config.tracker_tag],
        )
    )
    return result["key"]


def get_task_history_impl(
    *,
    issue_key: str,
    event_bus: EventBus,
    recovery: RecoveryManager,
) -> dict[str, Any]:
    """Get task history including recovery state and events."""
    state = recovery.get_state(issue_key)
    events = event_bus.get_task_history(issue_key)

    return {
        "attempt_count": state.attempt_count,
        "last_category": state.last_category.value if state.last_category else None,
        "should_retry": state.should_retry,
        "backoff_seconds": state.backoff_seconds,
        "events": [
            {"type": e.type, "task_key": e.task_key, "data": e.data, "timestamp": e.timestamp} for e in events[-20:]
        ],
    }


def get_recent_events_impl(
    *,
    count: int,
    event_bus: EventBus,
) -> list[dict[str, object]]:
    """Get recent global events."""
    events = event_bus.get_global_history()[-count:]
    return [{"type": e.type, "task_key": e.task_key, "timestamp": e.timestamp, "data": e.data} for e in events]


# ---------------------------------------------------------------------------
# MCP server builder
# ---------------------------------------------------------------------------


def build_orchestrator_server(
    *,
    event_bus: EventBus,
    recovery: RecoveryManager,
) -> Any:
    """Build MCP server with orchestrator context tools.

    Terminal tools are called directly by OrchestratorAgent, not through MCP.
    """

    @tool(
        "get_task_history",
        "Get recovery state and recent events for a task.",
        {"issue_key": str},
    )
    async def get_task_history(args: dict[str, Any]) -> dict[str, Any]:
        issue_key = args["issue_key"]
        result = get_task_history_impl(
            issue_key=issue_key,
            event_bus=event_bus,
            recovery=recovery,
        )
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    @tool(
        "get_recent_events",
        "Get recent orchestrator events (task completions, failures, etc.).",
        {"count": int},
    )
    async def get_recent_events(args: dict[str, Any]) -> dict[str, Any]:
        count = args.get("count", _DEFAULT_RECENT_EVENTS_COUNT)
        events = get_recent_events_impl(count=count, event_bus=event_bus)
        if not events:
            return {"content": [{"type": "text", "text": "No recent events."}]}
        return {"content": [{"type": "text", "text": json.dumps(events, ensure_ascii=False, indent=2)}]}

    all_tools = [get_task_history, get_recent_events]

    return create_sdk_mcp_server(
        name=_SERVER_NAME,
        version=_SERVER_VERSION,
        tools=all_tools,
    )


def build_orchestrator_allowed_tools(server_name: str) -> list[str]:
    """Build the allowed_tools list for an orchestrator SDK session."""
    return [
        f"mcp__{server_name}__get_task_history",
        f"mcp__{server_name}__get_recent_events",
    ]
