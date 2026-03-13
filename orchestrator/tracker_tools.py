"""In-process MCP tool wrappers for Yandex Tracker, scoped to a single issue."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool

from orchestrator.config import Config
from orchestrator.tracker_client import TrackerClient
from orchestrator.tracker_types import TrackerAttachmentDict, TrackerChecklistItemDict, TrackerCommentDict

if TYPE_CHECKING:
    from orchestrator.storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class ToolState:
    """Mutable shared state between MCP tools and orchestrator."""

    needs_info_requested: bool = False
    needs_info_text: str = ""
    proposals: list[dict[str, str]] = field(default_factory=list)
    created_subtasks: list[str] = field(default_factory=list)
    blocked_by_agent: str = ""
    blocking_reason: str = ""
    workpad_comment_id: int | None = None
    task_complete: bool = False


def format_issue(issue: Any) -> str:
    """Format a TrackerIssue for agent consumption."""
    return (
        f"**{issue.key}**: {issue.summary}\n\n"
        f"**Status**: {issue.status}\n"
        f"**Components**: {', '.join(issue.components)}\n"
        f"**Tags**: {', '.join(issue.tags)}\n\n"
        f"**Description**:\n{issue.description}"
    )


def format_comments(comments: list[TrackerCommentDict]) -> str:
    """Format comments for agent consumption."""
    if not comments:
        return "No comments."
    lines = []
    for c in comments:
        author = c.get("createdBy", {}).get("display", "unknown")
        text = c.get("text", "")
        created = c.get("createdAt", "")
        lines.append(f"**{author}** ({created}):\n{text}")
    return "\n\n---\n\n".join(lines)


def format_checklist(items: list[TrackerChecklistItemDict]) -> str:
    """Format checklist items for agent consumption."""
    if not items:
        return "No checklist items."
    lines = []
    for item in items:
        checked = "[x]" if item.get("checked", False) else "[ ]"
        text = item.get("text", "")
        lines.append(f"- {checked} {text}")
    return "\n".join(lines)


def format_attachments(attachments: list[TrackerAttachmentDict]) -> str:
    """Format attachment list for agent consumption."""
    if not attachments:
        return "No attachments."
    lines = []
    for a in attachments:
        lines.append(
            f"- **{a.get('name', 'unknown')}** (id: {a.get('id')}, "
            f"type: {a.get('mimetype', 'unknown')}, size: {a.get('size', 0)} bytes)"
        )
    return "\n".join(lines)


def is_text_mimetype(mimetype: str) -> bool:
    """Check if a MIME type represents text content that can be displayed."""
    from orchestrator.tracker_client import TEXT_MIMETYPES

    # Strip parameters (e.g. "; charset=utf-8") from MIME type
    base_mimetype = mimetype.split(";", 1)[0].strip()
    return base_mimetype in TEXT_MIMETYPES or base_mimetype.startswith("text/")


_WORKPAD_MARKER_PREFIX = "<!-- workpad:"
_WORKPAD_MARKER_SUFFIX = " -->"


def _workpad_marker(issue_key: str) -> str:
    return f"{_WORKPAD_MARKER_PREFIX}{issue_key}{_WORKPAD_MARKER_SUFFIX}"


def _is_workpad_comment(text: str, issue_key: str) -> bool:
    return text.startswith(_workpad_marker(issue_key))


def build_tracker_server(
    client: TrackerClient,
    issue_key: str,
    tool_state: ToolState | None = None,
    config: Config | None = None,
    issue_components: list[str] | None = None,
    storage: Storage | None = None,
) -> Any:
    """Build an MCP server with Tracker tools scoped to a single issue.

    The agent can ONLY access its assigned issue — no arbitrary issue access.
    """

    @tool("tracker_get_issue", "Get details of the assigned task", {})
    async def get_issue(args: dict[str, Any]) -> dict[str, Any]:
        issue = await asyncio.to_thread(client.get_issue, issue_key)
        return {"content": [{"type": "text", "text": format_issue(issue)}]}

    @tool("tracker_add_comment", "Add a comment to the assigned task", {"text": str})
    async def add_comment(args: dict[str, Any]) -> dict[str, Any]:
        text = args["text"]
        await asyncio.to_thread(client.add_comment, issue_key, text)
        return {"content": [{"type": "text", "text": "Comment added successfully."}]}

    @tool("tracker_get_comments", "Get all comments on the assigned task", {})
    async def get_comments(args: dict[str, Any]) -> dict[str, Any]:
        comments = await asyncio.to_thread(client.get_comments, issue_key)
        return {"content": [{"type": "text", "text": format_comments(comments)}]}

    @tool("tracker_get_checklist", "Get checklist items of the assigned task", {})
    async def get_checklist(args: dict[str, Any]) -> dict[str, Any]:
        items = await asyncio.to_thread(client.get_checklist, issue_key)
        return {"content": [{"type": "text", "text": format_checklist(items)}]}

    @tool(
        "tracker_request_info",
        "Request information from a human when blocked. IMPORTANT: commit and push all changes before calling this tool. Transitions the task to 'Needs Info' status and adds a comment describing the blocker. Use this instead of tracker_add_comment when you cannot proceed without human input.",
        {"text": str},
    )
    async def request_info(args: dict[str, Any]) -> dict[str, Any]:
        text = args["text"]
        await asyncio.to_thread(client.add_comment, issue_key, text)
        try:
            await asyncio.to_thread(client.transition_to_needs_info, issue_key)
        except requests.RequestException:
            logger.warning("Failed to transition %s to needs-info", issue_key, exc_info=True)
        if tool_state is not None:
            tool_state.needs_info_requested = True
            tool_state.needs_info_text = text
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Information request submitted. Task moved to 'Needs Info' status. Your session will resume when a human responds.",
                }
            ]
        }

    @tool(
        "tracker_signal_blocked",
        "Signal that this task is blocked by another agent's task. "
        "IMPORTANT: commit and push all changes before calling this tool. "
        "Creates a link to the blocking task and transitions to 'Needs Info' status. "
        "Use this instead of completing with success when you cannot proceed because another agent "
        "must finish their work first.",
        {"blocking_agent": str, "reason": str},
    )
    async def signal_blocked(args: dict[str, Any]) -> dict[str, Any]:
        blocking_agent = args["blocking_agent"]
        reason = args["reason"]

        # Add comment about the blockage
        comment_text = f"Заблокировано агентом {blocking_agent}. Причина: {reason}"
        await asyncio.to_thread(client.add_comment, issue_key, comment_text)

        # Create link to blocking task
        try:
            await asyncio.to_thread(client.add_link, issue_key, blocking_agent, "depends on")
        except requests.RequestException:
            logger.warning("Failed to create link to blocking task %s", blocking_agent, exc_info=True)

        # Transition to needs-info
        try:
            await asyncio.to_thread(client.transition_to_needs_info, issue_key)
        except requests.RequestException:
            logger.warning("Failed to transition %s to needs-info", issue_key, exc_info=True)

        # Set state for orchestrator
        if tool_state is not None:
            tool_state.blocked_by_agent = blocking_agent
            tool_state.blocking_reason = reason
            tool_state.needs_info_requested = True  # Treat as needs_info
            tool_state.needs_info_text = comment_text  # Ensure NEEDS_INFO event has the reason

        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Task marked as blocked by {blocking_agent}. Moved to 'Needs Info' status. "
                    "Your session will resume when the blocking task completes.",
                }
            ]
        }

    @tool(
        "propose_improvement",
        "Propose an improvement to the AI agent system (orchestrator, tools, prompts, dashboard). "
        "Non-blocking: the proposal is queued for human review. Use for issues you discovered in "
        "your working environment, NOT in the project you're working on.",
        {"summary": str, "description": str, "component": str, "category": str},
    )
    async def propose_improvement(args: dict[str, Any]) -> dict[str, Any]:
        summary = args["summary"]
        description = args["description"]
        component = args["component"]
        category = args["category"]
        if tool_state is not None:
            tool_state.proposals.append(
                {
                    "summary": summary,
                    "description": description,
                    "component": component,
                    "category": category,
                }
            )
        return {
            "content": [
                {"type": "text", "text": "Proposal submitted for human review. Continue with your current task."}
            ]
        }

    @tool(
        "tracker_create_subtask",
        "Create a follow-up subtask under the assigned task. Use this when the remaining work must be done in a separate task, for example in multi-repository flows requiring multiple PRs.",
        {"summary": str, "description": str},
    )
    async def create_subtask(args: dict[str, Any]) -> dict[str, Any]:
        summary = args["summary"]
        description = args["description"]
        queue = config.tracker_queue if config else issue_key.split("-", maxsplit=1)[0]
        tracker_tag = config.tracker_tag if config else "ai-task"
        result = await asyncio.to_thread(
            client.create_subtask,
            parent_key=issue_key,
            queue=queue,
            summary=summary,
            description=description,
            components=issue_components,
            project_id=config.tracker_project_id if config else None,
            boards=config.tracker_boards if config else None,
            tags=[tracker_tag],
        )
        subtask_key = result.get("key", "")
        if subtask_key and tool_state is not None:
            tool_state.created_subtasks.append(subtask_key)
        link_warning = ""
        if result.get("link_failed"):
            link_warning = " WARNING: Failed to link as subtask to parent. Task created but not linked."
        return {"content": [{"type": "text", "text": f"Created subtask {subtask_key} with ai-task tag.{link_warning}"}]}

    @tool(
        "tracker_get_attachments",
        "Get list of file attachments on the assigned task. Returns id, name, mimetype, size for each.",
        {},
    )
    async def get_attachments(args: dict[str, Any]) -> dict[str, Any]:
        attachments = await asyncio.to_thread(client.get_attachments, issue_key)
        return {"content": [{"type": "text", "text": format_attachments(attachments)}]}

    @tool(
        "tracker_download_attachment",
        "Download an attachment by ID. Returns text content for text files (XML, JSON, CSV, TXT, MD). "
        "Returns only metadata for binary files (images). Max file size: 5 MB.",
        {"attachment_id": int},
    )
    async def download_attachment(args: dict[str, Any]) -> dict[str, Any]:
        attachment_id = args["attachment_id"]

        # Security: verify attachment belongs to the assigned issue
        attachments = await asyncio.to_thread(client.get_attachments, issue_key)
        attachment_ids = {str(att["id"]) for att in attachments}
        if str(attachment_id) not in attachment_ids:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: Attachment {attachment_id} not found in task {issue_key}. "
                        f"Available attachments: {', '.join(attachment_ids) if attachment_ids else 'none'}",
                    }
                ]
            }

        try:
            content, content_type = await asyncio.to_thread(client.download_attachment, attachment_id)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}]}

        if is_text_mimetype(content_type):
            text = content.decode("utf-8", errors="replace")
            return {"content": [{"type": "text", "text": text}]}

        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Binary file (type: {content_type}, size: {len(content)} bytes). "
                    "Content cannot be displayed.",
                }
            ]
        }

    # --- Workpad tools ---

    def _error(msg: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": msg}]}

    @tool(
        "tracker_create_workpad",
        "Create or re-attach to a Workpad comment. "
        "Safe to call on every task start — reuses "
        "existing Workpad if found. Use "
        "tracker_update_workpad to update it later.",
        {"text": str},
    )
    async def create_workpad(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_state is None:
            return _error("Workpad not available in this context.")
        text = args["text"]
        marker = _workpad_marker(issue_key)
        tagged_text = f"{marker}\n{text}"
        comments = await asyncio.to_thread(client.get_comments, issue_key)
        for c in reversed(comments):
            if _is_workpad_comment(c.get("text", ""), issue_key):
                tool_state.workpad_comment_id = c["id"]
                await asyncio.to_thread(
                    client.update_comment,
                    issue_key,
                    tool_state.workpad_comment_id,
                    tagged_text,
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Workpad found and updated.",
                        }
                    ]
                }
        response = await asyncio.to_thread(client.add_comment, issue_key, tagged_text)
        tool_state.workpad_comment_id = response["id"]
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Workpad created.",
                }
            ]
        }

    @tool(
        "tracker_update_workpad",
        "Update the Workpad comment with new content. Automatically finds the Workpad if session was resumed.",
        {"text": str},
    )
    async def update_workpad(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_state is None:
            return _error("Workpad not available in this context.")
        text = args["text"]
        marker = _workpad_marker(issue_key)
        tagged_text = f"{marker}\n{text}"
        if tool_state.workpad_comment_id is None:
            comments = await asyncio.to_thread(client.get_comments, issue_key)
            for c in reversed(comments):
                if _is_workpad_comment(c.get("text", ""), issue_key):
                    tool_state.workpad_comment_id = c["id"]
                    break
        if tool_state.workpad_comment_id is None:
            return _error("No Workpad found. Call tracker_create_workpad first.")
        await asyncio.to_thread(
            client.update_comment,
            issue_key,
            tool_state.workpad_comment_id,
            tagged_text,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Workpad updated.",
                }
            ]
        }

    @tool(
        "tracker_mark_complete",
        "Signal that this task is legitimately complete "
        "without a PR. Use when the task doesn't require "
        "code changes (research, docs, config, "
        "investigation). The system will NOT retry or "
        "continue after this signal.",
        {},
    )
    async def mark_complete(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_state is None:
            return _error("Not available in this context.")
        tool_state.task_complete = True
        return {
            "content": [
                {
                    "type": "text",
                    "text": ("Task marked as complete (no PR)."),
                }
            ]
        }

    # Environment config (read-only for worker agents)
    env_tools: list[Any] = []

    if storage is not None:

        @tool(
            "env_get",
            "Get environment config by name "
            "(e.g. 'dev', 'staging', 'prod'). "
            "Returns connection details for "
            "verification.",
            {"name": str},
        )
        async def env_get(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            if storage is None:
                raise RuntimeError("storage is not set")
            name = args["name"]
            result = await storage.get_environment(name)
            if result is None:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Environment '{name}' not found.",
                        }
                    ]
                }
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            result,
                            ensure_ascii=False,
                            indent=2,
                        ),
                    }
                ]
            }

        env_tools = [env_get]

    all_tools = [
        get_issue,
        add_comment,
        get_comments,
        get_checklist,
        request_info,
        signal_blocked,
        create_subtask,
        propose_improvement,
        get_attachments,
        download_attachment,
        create_workpad,
        update_workpad,
        mark_complete,
    ]
    all_tools.extend(env_tools)

    return create_sdk_mcp_server(
        name="tracker",
        version="1.0.0",
        tools=all_tools,
    )
