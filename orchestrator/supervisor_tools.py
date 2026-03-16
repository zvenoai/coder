"""In-process MCP tool wrappers for Supervisor agent — extended Tracker access."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool

from orchestrator.constants import EventType
from orchestrator.event_bus import Event
from orchestrator.github_client import GitHubClient
from orchestrator.supervisor_memory import EmbeddingClient, MemoryIndex, list_memory_files
from orchestrator.tracker_client import TrackerClient
from orchestrator.tracker_tools import format_checklist, format_comments, format_issue

if TYPE_CHECKING:
    from orchestrator.agent_mailbox import AgentMailbox
    from orchestrator.dependency_manager import DependencyManager
    from orchestrator.epic_coordinator import EpicCoordinator
    from orchestrator.event_bus import EventBus
    from orchestrator.heartbeat import HeartbeatMonitor
    from orchestrator.k8s_client import K8sClient
    from orchestrator.preflight_checker import PreflightChecker
    from orchestrator.storage import Storage

logger = logging.getLogger(__name__)

_SERVER_NAME = "supervisor"
_SERVER_VERSION = "1.0.0"
_DEFAULT_RECENT_EVENTS_COUNT = 50
_PR_URL_PATTERN = re.compile(r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)")


def build_supervisor_server(
    client: TrackerClient,
    get_pending_proposals: Callable[[], list[dict[str, str]]],
    get_recent_events: Callable[[int], list[dict[str, object]]],
    on_task_created: Callable[[str], None],
    tracker_queue: str,
    tracker_project_id: int,
    tracker_boards: list[int],
    tracker_tag: str,
    storage: Storage | None = None,
    github: GitHubClient | None = None,
    memory_index: MemoryIndex | None = None,
    embedder: EmbeddingClient | None = None,
    # Agent management callbacks (optional — available in chat mode)
    list_running_tasks_callback: Callable[[], list[dict[str, object]]] | None = None,
    send_message_callback: Callable[[str, str], Any] | None = None,
    abort_task_callback: Callable[[str], Any] | None = None,
    cancel_task_callback: Callable[[str, str], Any] | None = None,
    epic_coordinator: EpicCoordinator | None = None,
    mailbox: AgentMailbox | None = None,
    k8s_client: K8sClient | None = None,
    dependency_manager: DependencyManager | None = None,
    # Preflight review (optional — supervisor-gated dispatch)
    preflight_checker: PreflightChecker | None = None,
    mark_dispatched_callback: Callable[[str], None] | None = None,
    remove_dispatched_callback: Callable[[str], None] | None = None,
    clear_recovery_callback: Callable[[str], None] | None = None,
    event_bus: EventBus | None = None,
    # Heartbeat monitor (optional — for get_agent_health tool)
    heartbeat_monitor: HeartbeatMonitor | None = None,
    # Diagnostic callbacks (optional — available in chat mode)
    get_state_callback: Callable[[], dict[str, Any]] | None = None,
    get_task_events_callback: (Callable[[str], list[dict[str, Any]]] | None) = None,
    # Escalation tag for human review
    escalation_tag: str = "needs-human-review",
) -> Any:
    """Build an MCP server with extended Tracker tools for the Supervisor agent.

    Unlike worker agents (scoped to a single issue), the Supervisor can
    search and read any issue in the tracker.
    """

    @tool(
        "tracker_search_issues",
        "Search Yandex Tracker issues using query language. Returns a list of matching issues.",
        {"query": str},
    )
    async def search_issues(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"]
        issues = await asyncio.to_thread(client.search, query)
        if not issues:
            return {"content": [{"type": "text", "text": "No issues found."}]}
        parts = [format_issue(issue) for issue in issues]
        return {"content": [{"type": "text", "text": "\n\n---\n\n".join(parts)}]}

    @tool(
        "tracker_get_issue",
        "Get details of any Yandex Tracker issue by key.",
        {"issue_key": str},
    )
    async def get_issue(args: dict[str, Any]) -> dict[str, Any]:
        issue_key = args["issue_key"]
        issue = await asyncio.to_thread(client.get_issue, issue_key)
        return {"content": [{"type": "text", "text": format_issue(issue)}]}

    @tool(
        "tracker_get_comments",
        "Get all comments on any Yandex Tracker issue.",
        {"issue_key": str},
    )
    async def get_comments(args: dict[str, Any]) -> dict[str, Any]:
        issue_key = args["issue_key"]
        comments = await asyncio.to_thread(client.get_comments, issue_key)
        return {"content": [{"type": "text", "text": format_comments(comments)}]}

    @tool(
        "tracker_get_checklist",
        "Get checklist items of any Yandex Tracker issue.",
        {"issue_key": str},
    )
    async def get_checklist(args: dict[str, Any]) -> dict[str, Any]:
        issue_key = args["issue_key"]
        items = await asyncio.to_thread(client.get_checklist, issue_key)
        return {"content": [{"type": "text", "text": format_checklist(items)}]}

    @tool(
        "tracker_get_attachments",
        "Get list of file attachments on any Yandex Tracker issue.",
        {"issue_key": str},
    )
    async def get_attachments(args: dict[str, Any]) -> dict[str, Any]:
        from orchestrator.tracker_tools import format_attachments

        issue_key = args["issue_key"]
        attachments = await asyncio.to_thread(client.get_attachments, issue_key)
        return {"content": [{"type": "text", "text": format_attachments(attachments)}]}

    @tool(
        "tracker_download_attachment",
        "Download an attachment by ID. Returns text content for text files (XML, JSON, CSV, TXT, MD). "
        "Returns only metadata for binary files (images). Max file size: 5 MB. "
        "Optional: provide issue_key to validate that the attachment belongs to that issue.",
        {"attachment_id": int},
    )
    async def download_attachment(args: dict[str, Any]) -> dict[str, Any]:
        from orchestrator.tracker_tools import is_text_mimetype

        attachment_id = args["attachment_id"]
        issue_key = args.get("issue_key")

        # Validate attachment belongs to issue if issue_key is provided
        if issue_key:
            try:
                attachments = await asyncio.to_thread(client.get_attachments, issue_key)
                # Normalize types to string for comparison (API may return int or str)
                attachment_ids = {str(att["id"]) for att in attachments}
                if str(attachment_id) not in attachment_ids:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Error: Attachment {attachment_id} does not belong to issue {issue_key}.",
                            }
                        ]
                    }
            except requests.RequestException as e:
                return {"content": [{"type": "text", "text": f"Error validating attachment: {e}"}]}

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

    @tool(
        "get_pending_proposals",
        "Get all pending improvement proposals from worker agents.",
        {},
    )
    async def pending_proposals(args: dict[str, Any]) -> dict[str, Any]:
        proposals = get_pending_proposals()
        if not proposals:
            return {"content": [{"type": "text", "text": "No pending proposals."}]}
        return {"content": [{"type": "text", "text": json.dumps(proposals, ensure_ascii=False, indent=2)}]}

    @tool(
        "get_recent_events",
        "Get recent orchestrator events (task completions, failures, proposals, etc.).",
        {"count": int},
    )
    async def recent_events(args: dict[str, Any]) -> dict[str, Any]:
        count = args.get("count", _DEFAULT_RECENT_EVENTS_COUNT)
        events = get_recent_events(count)
        if not events:
            return {"content": [{"type": "text", "text": "No recent events."}]}
        return {"content": [{"type": "text", "text": json.dumps(events, ensure_ascii=False, indent=2)}]}

    @tool(
        "tracker_create_issue",
        "Create a new task in Yandex Tracker with ai-task tag.",
        {"summary": str, "description": str, "component": str, "assignee": str},
    )
    async def create_issue(args: dict[str, Any]) -> dict[str, Any]:
        result = await asyncio.to_thread(
            lambda: client.create_issue(
                queue=tracker_queue,
                summary=args["summary"],
                description=args["description"],
                components=[args["component"]],
                assignee=args["assignee"],
                project_id=tracker_project_id,
                boards=tracker_boards,
                tags=[tracker_tag],
            )
        )
        issue_key = result["key"]
        on_task_created(issue_key)
        return {"content": [{"type": "text", "text": f"Created task {issue_key}"}]}

    @tool(
        "tracker_update_issue",
        (
            "Update fields of an existing Yandex Tracker issue: summary, description, "
            "and/or tags. Only provided fields are changed. "
            "Tags REPLACE all existing tags — read current tags with tracker_get_issue "
            "first if you want to append rather than replace."
        ),
        {"issue_key": str},
    )
    async def update_issue(args: dict[str, Any]) -> dict[str, Any]:
        issue_key = args["issue_key"]
        summary = args.get("summary")
        description = args.get("description")
        tags = args.get("tags")
        try:
            # Result not needed — success confirmed by absence of exception.
            await asyncio.to_thread(
                lambda: client.update_issue(
                    issue_key,
                    summary=summary,
                    description=description,
                    tags=tags,
                )
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}]}
        updated = []
        if summary is not None:
            updated.append("summary")
        if description is not None:
            updated.append("description")
        if tags is not None:
            updated.append(f"tags={tags}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Updated {issue_key}: {', '.join(updated)}.",
                }
            ]
        }

    # ---- Escalation tool ----

    @tool(
        "escalate_to_human",
        (
            "Escalate an issue for human review when the "
            "supervisor is uncertain. Adds a "
            "needs-human-review tag, removes ai-task tag, "
            "and posts a comment explaining the uncertainty "
            "and options being considered."
        ),
        {
            "issue_key": str,
            "reason": str,
            "options": list,
        },
    )
    async def escalate_tool(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        from orchestrator.escalation import (
            escalate_to_human as _escalate,
        )

        issue_key = args["issue_key"]
        reason = args["reason"]
        options = args.get("options", [])
        result = await _escalate(
            tracker_client=client,
            issue_key=issue_key,
            reason=reason,
            options=options,
            tag=escalation_tag,
        )
        return {"content": [{"type": "text", "text": result}]}

    # ---- Stats analytics tools (only registered when storage is available) ----

    stats_tools: list[Any] = []

    if storage is not None:

        @tool(
            "stats_query_summary",
            "Get summary statistics: total tasks, success rate, cost, avg duration. Args: days (int, default 7).",
            {"days": int},
        )
        async def stats_summary(args: dict[str, Any]) -> dict[str, Any]:
            days = args.get("days", 7)
            if storage is None:
                raise RuntimeError("storage is not set")
            summary = await storage.get_summary(days=days)
            return {"content": [{"type": "text", "text": json.dumps(summary, indent=2)}]}

        @tool(
            "stats_query_costs",
            "Get cost breakdown by model or day. Args: group_by ('model'|'day'), days (int), limit (int).",
            {"group_by": str, "days": int, "limit": int},
        )
        async def stats_costs(args: dict[str, Any]) -> dict[str, Any]:
            group_by = args.get("group_by", "model")
            days = args.get("days", 7)
            limit = args.get("limit", 50)
            if storage is None:
                raise RuntimeError("storage is not set")
            costs = await storage.get_costs(group_by=group_by, days=days, limit=limit)
            if not costs:
                return {"content": [{"type": "text", "text": "No cost data found."}]}
            return {"content": [{"type": "text", "text": json.dumps(costs, indent=2)}]}

        @tool(
            "stats_query_errors",
            "Get error statistics by category. Args: days (int, default 7).",
            {"days": int},
        )
        async def stats_errors(args: dict[str, Any]) -> dict[str, Any]:
            days = args.get("days", 7)
            if storage is None:
                raise RuntimeError("storage is not set")
            errors = await storage.get_error_stats(days=days)
            if not errors:
                return {"content": [{"type": "text", "text": "No errors in the specified period."}]}
            return {"content": [{"type": "text", "text": json.dumps(errors, indent=2)}]}

        @tool(
            "stats_query_custom",
            "Execute a custom read-only SQL query on the stats database. Only SELECT allowed. "
            "Tables: task_runs, supervisor_runs, pr_lifecycle, error_log. "
            "Args: query (str). Max 100 rows, 5s timeout.",
            {"query": str},
        )
        async def stats_custom(args: dict[str, Any]) -> dict[str, Any]:
            query = args["query"]
            if storage is None:
                raise RuntimeError("storage is not set")
            try:
                rows = await storage.execute_readonly(query, limit=100, timeout_seconds=5.0)
            except ValueError as e:
                return {"content": [{"type": "text", "text": f"Query rejected: {e}"}]}
            except TimeoutError:
                return {"content": [{"type": "text", "text": "Query timed out (5s limit)."}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Query error: {e}"}]}
            if not rows:
                return {"content": [{"type": "text", "text": "No results."}]}
            return {"content": [{"type": "text", "text": json.dumps(rows, indent=2)}]}

        stats_tools = [stats_summary, stats_costs, stats_errors, stats_custom]

    # Environment config tools (only if storage available)
    env_config_tools: list[Any] = []

    if storage is not None:

        @tool(
            "env_get",
            "Get environment config by name "
            "(e.g. 'dev', 'staging', 'prod'). "
            "Returns connection details, API keys, "
            "test users, etc.",
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

        @tool(
            "env_set",
            "Set or update environment config. "
            "Args: name (str), config (dict with "
            "api_url, frontend_url, api_key, "
            "test_users, etc.).",
            {"name": str, "config": dict},
        )
        async def env_set(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            if storage is None:
                raise RuntimeError("storage is not set")
            name = args["name"]
            config = args["config"]
            await storage.set_environment(name, config, "supervisor")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Environment '{name}' saved.",
                    }
                ]
            }

        @tool(
            "env_list",
            "List all configured environments with last update time.",
            {},
        )
        async def env_list(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            if storage is None:
                raise RuntimeError("storage is not set")
            envs = await storage.list_environments()
            if not envs:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "No environments configured.",
                        }
                    ]
                }
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            envs,
                            ensure_ascii=False,
                            indent=2,
                        ),
                    }
                ]
            }

        env_config_tools = [env_get, env_set, env_list]

    # GitHub tools (only registered if GitHubClient is provided)
    github_tools: list[Any] = []
    if github is not None:

        @tool(
            "github_get_pr",
            "Get PR details (title, body, author, state, stats) from GitHub.",
            {"owner": str, "repo": str, "pr_number": int},
        )
        async def github_get_pr(args: dict[str, Any]) -> dict[str, Any]:
            details = await asyncio.to_thread(github.get_pr_details, args["owner"], args["repo"], args["pr_number"])
            lines = [
                f"# PR #{args['pr_number']}: {details.title}",
                f"**Author:** {details.author}",
                f"**State:** {details.state} | **Review:** {details.review_decision or 'pending'}",
                f"**Branches:** {details.head_branch} → {details.base_branch}",
                f"**Changes:** +{details.additions} / -{details.deletions} in {details.changed_files} file(s)",
                "",
                details.body or "(no description)",
            ]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "github_get_pr_diff",
            "Get raw diff text of a GitHub PR.",
            {"owner": str, "repo": str, "pr_number": int},
        )
        async def github_get_pr_diff(args: dict[str, Any]) -> dict[str, Any]:
            diff = await asyncio.to_thread(github.get_pr_diff, args["owner"], args["repo"], args["pr_number"])
            return {"content": [{"type": "text", "text": diff or "(empty diff)"}]}

        @tool(
            "github_get_pr_files",
            "Get list of changed files in a GitHub PR with status and patch.",
            {"owner": str, "repo": str, "pr_number": int},
        )
        async def github_get_pr_files(args: dict[str, Any]) -> dict[str, Any]:
            files = await asyncio.to_thread(github.get_pr_files, args["owner"], args["repo"], args["pr_number"])
            if not files:
                return {"content": [{"type": "text", "text": "No changed files."}]}
            parts = []
            for f in files:
                header = f"**{f.filename}** ({f.status}) +{f.additions}/-{f.deletions}"
                if f.patch:
                    parts.append(f"{header}\n```diff\n{f.patch}\n```")
                else:
                    parts.append(header)
            return {"content": [{"type": "text", "text": "\n\n".join(parts)}]}

        @tool(
            "github_get_pr_reviews",
            "Get all review threads (resolved + unresolved) on a GitHub PR.",
            {"owner": str, "repo": str, "pr_number": int},
        )
        async def github_get_pr_reviews(args: dict[str, Any]) -> dict[str, Any]:
            threads = await asyncio.to_thread(github.get_review_threads, args["owner"], args["repo"], args["pr_number"])
            if not threads:
                return {"content": [{"type": "text", "text": "No review threads."}]}
            parts = []
            for t in threads:
                status = "resolved" if t.is_resolved else "UNRESOLVED"
                location = f"{t.path}:{t.line}" if t.path else "(general)"
                comments_text = "\n".join(f"  - **{c.author}**: {c.body}" for c in t.comments)
                parts.append(f"[{status}] {location}\n{comments_text}")
            return {"content": [{"type": "text", "text": "\n\n".join(parts)}]}

        @tool(
            "github_get_pr_checks",
            "Get CI check status (all checks, not just failed) for a GitHub PR.",
            {"owner": str, "repo": str, "pr_number": int},
        )
        async def github_get_pr_checks(args: dict[str, Any]) -> dict[str, Any]:
            checks = await asyncio.to_thread(github.get_all_checks, args["owner"], args["repo"], args["pr_number"])
            if not checks:
                return {"content": [{"type": "text", "text": "No CI checks found."}]}
            lines = []
            for c in checks:
                icon = (
                    "\u2713"
                    if c.conclusion == "SUCCESS"
                    else "\u2717"
                    if c.conclusion in ("FAILURE", "ERROR")
                    else "\u25cb"
                )
                line = f"{icon} **{c.name}**: {c.conclusion or c.status}"
                if c.details_url:
                    line += f" ([details]({c.details_url}))"
                lines.append(line)
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "github_list_prs",
            "List PRs in a GitHub repo.",
            {"owner": str, "repo": str, "state": str},
        )
        async def github_list_prs(args: dict[str, Any]) -> dict[str, Any]:
            prs = await asyncio.to_thread(github.list_prs, args["owner"], args["repo"], state=args.get("state", "open"))
            if not prs:
                return {"content": [{"type": "text", "text": "No PRs found."}]}
            lines = []
            for pr in prs:
                lines.append(
                    f"#{pr['number']} [{pr['state']}] {pr['title']} "
                    f"({pr['author']}: {pr['head_branch']} \u2192 {pr['base_branch']})"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "github_check_pr_mergeability",
            "Check PR mergeability (merge conflict detection). Returns MERGEABLE, CONFLICTING, or UNKNOWN.",
            {"owner": str, "repo": str, "pr_number": int},
        )
        async def github_check_pr_mergeability(args: dict[str, Any]) -> dict[str, Any]:
            status = await asyncio.to_thread(github.get_pr_status, args["owner"], args["repo"], args["pr_number"])
            mergeable = status.mergeable or "UNKNOWN"
            state_icon = {"MERGEABLE": "\u2713", "CONFLICTING": "\u2717", "UNKNOWN": "?"}.get(mergeable, "?")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"{state_icon} PR #{args['pr_number']}: mergeable={mergeable} "
                        f"(state={status.state}, review={status.review_decision or 'pending'})",
                    }
                ]
            }

        github_tools = [
            github_get_pr,
            github_get_pr_diff,
            github_get_pr_files,
            github_get_pr_reviews,
            github_get_pr_checks,
            github_list_prs,
            github_check_pr_mergeability,
        ]

    # Memory tools (only registered if memory index is available)
    memory_tools: list[Any] = []
    if memory_index is not None and embedder is not None:

        @tool(
            "memory_search",
            "Hybrid search across supervisor memory files (vector + keyword). "
            "Args: query (str), max_results (int, default 6), min_score (float, default 0.3).",
            {"query": str, "max_results": int, "min_score": float},
        )
        async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
            query = args["query"]
            max_results = args.get("max_results", 6)
            min_score = args.get("min_score", 0.3)
            if embedder is None:
                raise RuntimeError("embedder is not set")
            if memory_index is None:
                raise RuntimeError("memory_index is not set")

            try:
                vector = await asyncio.to_thread(embedder.embed, query)
                results = await memory_index.hybrid_search(
                    query_embedding=vector,
                    query_text=query,
                    max_results=max_results,
                    min_score=min_score,
                )
                if not results:
                    return {"content": [{"type": "text", "text": "No relevant memories found."}]}

                lines = [f"Found {len(results)} relevant memories:"]
                for r in results:
                    rel_path = Path(r["path"]).name
                    lines.append(f"\n**{rel_path}** (lines {r['start_line']}-{r['end_line']}, score: {r['score']:.2f})")
                    # Show first 200 chars of snippet
                    snippet = r["snippet"][:200]
                    if len(r["snippet"]) > 200:
                        snippet += "..."
                    lines.append(snippet)
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}
            except Exception as e:
                logger.exception("Error searching memories")
                return {"content": [{"type": "text", "text": f"Error searching memories: {e}"}]}

        @tool(
            "memory_get",
            "Read specific lines from a memory file. "
            "Args: path (str, filename like 'MEMORY.md' or '2026-02-16.md'), "
            "from_line (int, optional), lines (int, optional, default all).",
            {"path": str, "from_line": int, "lines": int},
        )
        async def memory_get(args: dict[str, Any]) -> dict[str, Any]:
            filename = args["path"]
            from_line = args.get("from_line", 1)
            line_count = args.get("lines")

            # Validate: only .md files inside memory_dir
            if not filename.endswith(".md"):
                return {"content": [{"type": "text", "text": "Only .md files are supported."}]}
            if "/" in filename or "\\" in filename:
                return {"content": [{"type": "text", "text": "Path traversal not allowed. Use filename only."}]}

            full_path = Path(memory_index.memory_dir) / filename
            if not full_path.exists():
                return {"content": [{"type": "text", "text": f"File not found: {filename}"}]}

            try:
                text = await asyncio.to_thread(full_path.read_text, "utf-8")
                all_lines = text.splitlines()

                # Apply from_line and lines parameters (1-indexed)
                start = max(from_line - 1, 0)
                end = start + line_count if line_count is not None else len(all_lines)
                selected = all_lines[start:end]

                return {"content": [{"type": "text", "text": "\n".join(selected)}]}
            except OSError:
                logger.exception("Error reading memory file %s", filename)
                return {"content": [{"type": "text", "text": f"Error reading {filename}."}]}

        @tool(
            "memory_write",
            "Append content to a memory file. Creates the file if it doesn't exist. "
            "Args: path (str, filename like 'MEMORY.md' or '2026-02-16.md'), content (str).",
            {"path": str, "content": str},
        )
        async def memory_write(args: dict[str, Any]) -> dict[str, Any]:
            filename = args["path"]
            content = args["content"]

            # Validate: only .md files inside memory_dir
            if not filename.endswith(".md"):
                return {"content": [{"type": "text", "text": "Only .md files are supported."}]}
            if "/" in filename or "\\" in filename:
                return {"content": [{"type": "text", "text": "Path traversal not allowed. Use filename only."}]}

            full_path = Path(memory_index.memory_dir) / filename

            try:
                # Append content (with newline separator)
                if full_path.exists():
                    existing = await asyncio.to_thread(full_path.read_text, "utf-8")
                    if existing and not existing.endswith("\n"):
                        content = "\n" + content
                else:
                    existing = ""

                await asyncio.to_thread(full_path.write_text, existing + content + "\n", "utf-8")
            except OSError:
                logger.exception("Error writing memory file %s", filename)
                return {"content": [{"type": "text", "text": f"Error writing {filename}."}]}

            lines_added = len(content.strip().splitlines())

            # Trigger reindex (non-fatal: file is already written)
            try:
                await memory_index.reindex_file(str(full_path), embedder)
            except Exception:
                logger.warning("File written but reindex failed for %s", filename, exc_info=True)

            return {"content": [{"type": "text", "text": f"Written to {filename}: {lines_added} lines added."}]}

        @tool(
            "memory_list",
            "List all memory files with size and line count. "
            "Returns: MEMORY.md (curated long-term) + daily YYYY-MM-DD.md files.",
            {},
        )
        async def memory_list_tool(args: dict[str, Any]) -> dict[str, Any]:
            if memory_index is None:
                raise RuntimeError("memory_index is not set")
            files = await asyncio.to_thread(list_memory_files, memory_index.memory_dir)
            if not files:
                return {"content": [{"type": "text", "text": "No memory files found."}]}
            lines = [f"Memory files ({len(files)}):"]
            for f in files:
                lines.append(f"- **{f['name']}** ({f['size_bytes']} bytes, {f['lines']} lines)")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        memory_tools = [memory_search, memory_get, memory_write, memory_list_tool]

    # Agent management tools (registered when callbacks are provided)
    agent_mgmt_tools: list[Any] = []
    if list_running_tasks_callback is not None:

        @tool(
            "list_running_tasks",
            "List all agent tasks with active sessions (running, in_review, needs_info, on_demand). "
            "Note: you can also send messages to tasks NOT in this list — "
            "an on-demand session will be created automatically.",
            {},
        )
        async def list_running_tasks(args: dict[str, Any]) -> dict[str, Any]:
            tasks = list_running_tasks_callback()
            if not tasks:
                return {"content": [{"type": "text", "text": "No running tasks."}]}
            return {"content": [{"type": "text", "text": json.dumps(tasks, ensure_ascii=False, indent=2)}]}

        agent_mgmt_tools.append(list_running_tasks)

    if send_message_callback is not None:

        @tool(
            "send_message_to_task",
            "Send a message to any agent task (running, in_review, needs_info, or completed). "
            "If no active session exists, an on-demand session is created automatically "
            "by resuming the saved conversation. Works for any task that has ever had an agent session.",
            {"task_key": str, "message": str},
        )
        async def send_message_to_task(args: dict[str, Any]) -> dict[str, Any]:
            if send_message_callback is None:
                raise RuntimeError("send_message_callback is not set")
            try:
                await send_message_callback(args["task_key"], args["message"])
                return {"content": [{"type": "text", "text": f"Message sent to {args['task_key']}."}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error: {e}"}]}

        agent_mgmt_tools.append(send_message_to_task)

    if abort_task_callback is not None:

        @tool(
            "abort_task",
            "Abort a running agent task immediately. The task will be marked as failed.",
            {"task_key": str},
        )
        async def abort_task(args: dict[str, Any]) -> dict[str, Any]:
            if abort_task_callback is None:
                raise RuntimeError("abort_task_callback is not set")
            try:
                await abort_task_callback(args["task_key"])
                return {"content": [{"type": "text", "text": f"Task {args['task_key']} aborted."}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error aborting task: {e}"}]}

        agent_mgmt_tools.append(abort_task)

    if cancel_task_callback is not None:

        @tool(
            "cancel_task",
            "Cancel a task entirely — abort if running, remove from dispatch queue, and prevent re-dispatch. "
            "Optionally post a comment to the tracker issue.",
            {"task_key": str, "reason": str},
        )
        async def cancel_task(args: dict[str, Any]) -> dict[str, Any]:
            if cancel_task_callback is None:
                raise RuntimeError("cancel_task_callback is not set")
            try:
                await cancel_task_callback(args["task_key"], args.get("reason", "Cancelled by supervisor"))
                return {"content": [{"type": "text", "text": f"Task {args['task_key']} cancelled."}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error cancelling task: {e}"}]}

        agent_mgmt_tools.append(cancel_task)

    # Epic management tools (only registered if EpicCoordinator is provided)
    epic_tools: list[Any] = []
    if epic_coordinator is not None:

        @tool(
            "epic_list",
            "List all active epics with phase, children count, and status breakdown.",
            {},
        )
        async def epic_list(args: dict[str, Any]) -> dict[str, Any]:
            if epic_coordinator is None:
                raise RuntimeError("epic_coordinator is not set")
            epics = epic_coordinator.get_state()
            if not epics:
                return {"content": [{"type": "text", "text": "No active epics."}]}
            lines: list[str] = []
            for key, data in epics.items():
                children = data.get("children", {})
                statuses: dict[str, int] = {}
                for c in children.values():
                    s = c.get("status", "unknown")
                    statuses[s] = statuses.get(s, 0) + 1
                status_str = ", ".join(f"{s}: {n}" for s, n in sorted(statuses.items()))
                lines.append(
                    f"**{key}** — {data.get('epic_summary', '')}\n"
                    f"  Phase: {data.get('phase', '?')} | Children: {len(children)} | {status_str}"
                )
            return {"content": [{"type": "text", "text": "\n\n".join(lines)}]}

        @tool(
            "epic_get_children",
            "Get detailed list of children for an epic.",
            {"epic_key": str},
        )
        async def epic_get_children(args: dict[str, Any]) -> dict[str, Any]:
            if epic_coordinator is None:
                raise RuntimeError("epic_coordinator is not set")
            epic_key = args["epic_key"]
            state = epic_coordinator.get_epic_state(epic_key)
            if state is None:
                return {"content": [{"type": "text", "text": f"Epic {epic_key} not found."}]}
            lines: list[str] = [f"**{epic_key}** — {state.epic_summary} (phase: {state.phase})"]
            for child in state.children.values():
                deps = ", ".join(child.depends_on) if child.depends_on else "none"
                tags = ", ".join(child.tags) if child.tags else "none"
                lines.append(
                    f"- **{child.key}**: {child.summary}\n"
                    f"  Status: {child.status.value} | Tracker: {child.tracker_status} | "
                    f"Depends on: {deps} | Tags: {tags}"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "epic_set_plan",
            "Set dependency graph for epic children and activate ready ones. "
            'Args: epic_key (str), dependencies (JSON str: {"QR-52": ["QR-51"], "QR-51": []}).',
            {"epic_key": str, "dependencies": str},
        )
        async def epic_set_plan(args: dict[str, Any]) -> dict[str, Any]:
            if epic_coordinator is None:
                raise RuntimeError("epic_coordinator is not set")
            epic_key = args["epic_key"]
            deps_str = args["dependencies"]

            state = epic_coordinator.get_epic_state(epic_key)
            if state is None:
                return {"content": [{"type": "text", "text": f"Epic {epic_key} not found."}]}

            try:
                deps: dict[str, list[str]] = json.loads(deps_str)
            except (json.JSONDecodeError, TypeError) as e:
                return {"content": [{"type": "text", "text": f"Invalid JSON: {e}"}]}

            # Validate all keys and deps reference known children
            known = set(state.children.keys())
            unknown = set(deps.keys()) - known
            if unknown:
                return {
                    "content": [{"type": "text", "text": f"Unknown children in plan: {', '.join(sorted(unknown))}"}]
                }
            for parent_key, dep_list in deps.items():
                unknown_deps = set(dep_list) - known
                if unknown_deps:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Unknown dependencies for {parent_key}: {', '.join(sorted(unknown_deps))}",
                            }
                        ]
                    }

            # Fill in children not mentioned in the deps map with empty dependencies
            full_deps = {key: deps.get(key, []) for key in known}

            if not epic_coordinator.validate_acyclic(full_deps):
                return {"content": [{"type": "text", "text": "Dependency graph has cycles — rejected."}]}

            epic_coordinator.set_child_dependencies(epic_key, full_deps)
            await epic_coordinator._tag_ready_children(epic_key)

            # Report
            ready_count = sum(1 for c in state.children.values() if c.status.value in ("ready", "dispatched"))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Plan set for {epic_key}: {len(full_deps)} children configured, "
                        f"{ready_count} ready for dispatch.",
                    }
                ]
            }

        @tool(
            "epic_activate_child",
            "Force-activate a single epic child (tag with ai-task and set READY).",
            {"epic_key": str, "child_key": str},
        )
        async def epic_activate_child(args: dict[str, Any]) -> dict[str, Any]:
            if epic_coordinator is None:
                raise RuntimeError("epic_coordinator is not set")
            epic_key = args["epic_key"]
            child_key = args["child_key"]
            ok = await epic_coordinator.activate_child(epic_key, child_key)
            if ok:
                return {"content": [{"type": "text", "text": f"Child {child_key} activated in {epic_key}."}]}
            return {
                "content": [
                    {"type": "text", "text": f"Could not activate {child_key} — not found or already terminal."}
                ]
            }

        @tool(
            "epic_reset_child",
            "Reset a terminal epic child (COMPLETED/FAILED/CANCELLED) back to PENDING for re-dispatch. "
            "Use when a child was falsely completed or needs to be retried. "
            "Does NOT reset DISPATCHED or READY children (agent running or about to).",
            {"epic_key": str, "child_key": str},
        )
        async def epic_reset_child(args: dict[str, Any]) -> dict[str, Any]:
            if epic_coordinator is None:
                raise RuntimeError("epic_coordinator is not set")
            epic_key = args["epic_key"]
            child_key = args["child_key"]
            ok = await epic_coordinator.reset_child(epic_key, child_key)
            if ok:
                return {"content": [{"type": "text", "text": f"Child {child_key} reset to PENDING in {epic_key}."}]}
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Could not reset {child_key} — not found, not terminal, or epic unknown.",
                    }
                ]
            }

        epic_tools = [epic_list, epic_get_children, epic_set_plan, epic_activate_child, epic_reset_child]

    # Mailbox tools (read-only access for supervisor)
    mailbox_tools: list[Any] = []
    if mailbox is not None:

        @tool(
            "view_agent_messages",
            "View inter-agent communication messages. Shows all messages "
            "exchanged between worker agents — useful for debugging coordination "
            "issues and understanding agent decisions. "
            "Pass empty string for task_key to see all messages.",
            {"task_key": str},
        )
        async def view_agent_messages(args: dict[str, Any]) -> dict[str, Any]:
            if mailbox is None:
                raise RuntimeError("mailbox is not set")
            task_key = args["task_key"]
            messages = mailbox.get_all_messages(task_key=task_key or None)
            if not messages:
                return {"content": [{"type": "text", "text": "No inter-agent messages found."}]}
            lines = [f"**{len(messages)} message(s):**"]
            for m in messages:
                reply_info = f" → Reply: {m.reply_text}" if m.reply_text else ""
                lines.append(
                    f"\n[{m.status}|{m.msg_type}|{m.delivery_status}] "
                    f"**{m.sender_task_key}** → **{m.target_task_key}**\n  {m.text}{reply_info}"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "get_comm_stats",
            "Get inter-agent communication statistics: messages sent, delivered, "
            "queued, overflow-dropped, replied, and expired.",
            {},
        )
        async def get_comm_stats(args: dict[str, Any]) -> dict[str, Any]:
            if mailbox is None:
                raise RuntimeError("mailbox is not set")
            stats = mailbox.get_stats()
            return {"content": [{"type": "text", "text": json.dumps(stats, indent=2)}]}

        mailbox_tools = [view_agent_messages, get_comm_stats]

    # Kubernetes tools (only registered if K8sClient is provided and available)
    k8s_tools: list[Any] = []
    if k8s_client is not None:
        _MAX_LOG_CHARS = 50_000

        @tool(
            "k8s_list_pods",
            "List Kubernetes pods with status, containers, and restart counts. "
            "Args: namespace (str, optional — defaults to configured namespace).",
            {"namespace": str},
        )
        async def k8s_list_pods(args: dict[str, Any]) -> dict[str, Any]:
            if k8s_client is None:
                raise RuntimeError("k8s_client is not set")
            ns = args.get("namespace") or None
            pods = await asyncio.to_thread(k8s_client.list_pods, ns)
            if not pods:
                return {"content": [{"type": "text", "text": "No pods found."}]}
            lines: list[str] = []
            for p in pods:
                containers_str = ", ".join(
                    f"{c.name} ({'ready' if c.ready else c.state}"
                    + (f": {c.state_reason}" if c.state_reason else "")
                    + f", restarts={c.restart_count})"
                    for c in p.containers
                )
                lines.append(f"**{p.name}** [{p.phase}] — {containers_str or 'no containers'}")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "k8s_get_pod_logs",
            "Get logs from a Kubernetes pod. "
            "Args: pod_name (str), container (str, optional), tail_lines (int, default 100), "
            "since_seconds (int, optional), timestamps (bool, default false), "
            "previous (bool, default false — get logs from previous container instance), "
            "namespace (str, optional).",
            {
                "pod_name": str,
                "container": str,
                "tail_lines": int,
                "since_seconds": int,
                "timestamps": bool,
                "previous": bool,
                "namespace": str,
            },
        )
        async def k8s_get_pod_logs(args: dict[str, Any]) -> dict[str, Any]:
            if k8s_client is None:
                raise RuntimeError("k8s_client is not set")
            logs = await asyncio.to_thread(
                k8s_client.get_pod_logs,
                pod_name=args["pod_name"],
                container=args.get("container"),
                tail_lines=args.get("tail_lines", 100),
                since_seconds=args.get("since_seconds"),
                timestamps=args.get("timestamps", False),
                previous=args.get("previous", False),
                namespace=args.get("namespace"),
            )
            if not logs:
                return {"content": [{"type": "text", "text": "(empty logs)"}]}
            if len(logs) > _MAX_LOG_CHARS:
                logs = logs[-_MAX_LOG_CHARS:]
                logs = f"... (truncated to last {_MAX_LOG_CHARS} chars)\n" + logs
            return {"content": [{"type": "text", "text": logs}]}

        @tool(
            "k8s_get_pod_status",
            "Get detailed status of a Kubernetes pod (phase, conditions, container states, labels). "
            "Args: pod_name (str), namespace (str, optional).",
            {"pod_name": str, "namespace": str},
        )
        async def k8s_get_pod_status(args: dict[str, Any]) -> dict[str, Any]:
            if k8s_client is None:
                raise RuntimeError("k8s_client is not set")
            detail = await asyncio.to_thread(
                k8s_client.get_pod_status,
                pod_name=args["pod_name"],
                namespace=args.get("namespace"),
            )
            if detail is None:
                return {"content": [{"type": "text", "text": f"Pod {args['pod_name']} not found or error."}]}
            lines = [
                f"# {detail.name}",
                f"**Namespace:** {detail.namespace} | **Phase:** {detail.phase}",
                f"**Node:** {detail.node_name or 'N/A'} | **Start time:** {detail.start_time or 'N/A'}",
            ]
            if detail.labels:
                labels_str = ", ".join(f"{k}={v}" for k, v in sorted(detail.labels.items()))
                lines.append(f"**Labels:** {labels_str}")
            if detail.conditions:
                lines.append("\n**Conditions:**")
                for cond in detail.conditions:
                    lines.append(
                        f"- {cond['type']}: {cond['status']}"
                        + (f" ({cond['reason']})" if cond.get("reason") else "")
                        + (f" — {cond['message']}" if cond.get("message") else "")
                    )
            if detail.containers:
                lines.append("\n**Containers:**")
                for c in detail.containers:
                    lines.append(
                        f"- **{c.name}**: {c.state}"
                        + (f" ({c.state_reason})" if c.state_reason else "")
                        + f" | ready={c.ready} | restarts={c.restart_count} | image={c.image}"
                    )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        k8s_tools = [k8s_list_pods, k8s_get_pod_logs, k8s_get_pod_status]

    # Dependency management tools (only registered if DependencyManager is provided)
    dependency_tools: list[Any] = []
    if dependency_manager is not None:

        @tool(
            "list_deferred_tasks",
            "List all tasks deferred due to unresolved dependencies. "
            "Shows blocker keys, deferral source (auto/manual), and time deferred.",
            {},
        )
        async def list_deferred_tasks(args: dict[str, Any]) -> dict[str, Any]:
            if dependency_manager is None:
                raise RuntimeError("dependency_manager is not set")
            deferred = dependency_manager.get_deferred()
            if not deferred:
                return {"content": [{"type": "text", "text": "No deferred tasks."}]}
            lines: list[str] = [f"**{len(deferred)} deferred task(s):**"]
            for key, task in deferred.items():
                source = "manual" if task.manual else "auto"
                blockers_str = ", ".join(task.blockers)
                lines.append(f"\n**{key}**: {task.issue_summary}\n  Blockers: {blockers_str} | Source: {source}")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "approve_task_dispatch",
            "Force-dispatch a deferred task despite unresolved dependencies. "
            "Use when the dependency is no longer relevant or can be handled in parallel.",
            {"task_key": str},
        )
        async def approve_task_dispatch(args: dict[str, Any]) -> dict[str, Any]:
            if dependency_manager is None:
                raise RuntimeError("dependency_manager is not set")
            task_key = args["task_key"]
            ok = await dependency_manager.approve_dispatch(task_key)
            if ok:
                return {"content": [{"type": "text", "text": f"Task {task_key} approved for dispatch."}]}
            return {"content": [{"type": "text", "text": f"Task {task_key} is not in the deferred set."}]}

        @tool(
            "defer_task",
            "Manually defer a task due to semantic dependencies that can't be detected from Tracker links. "
            "The task won't be dispatched until approved via approve_task_dispatch.",
            {"task_key": str, "summary": str, "reason": str},
        )
        async def defer_task(args: dict[str, Any]) -> dict[str, Any]:
            if dependency_manager is None:
                raise RuntimeError("dependency_manager is not set")
            task_key = args["task_key"]
            summary = args["summary"]
            reason = args["reason"]
            ok = await dependency_manager.defer_task(task_key, summary, reason)
            if ok:
                return {"content": [{"type": "text", "text": f"Task {task_key} deferred: {reason}"}]}
            return {"content": [{"type": "text", "text": f"Task {task_key} is already deferred."}]}

        dependency_tools = [list_deferred_tasks, approve_task_dispatch, defer_task]

    # Preflight review tools (supervisor-gated dispatch)
    preflight_tools: list[Any] = []
    if preflight_checker is not None and dependency_manager is not None:

        @tool(
            "resolve_preflight",
            "Resolve a preflight review for a deferred task. "
            "Decision: 'dispatch' sends agent to work on the task, "
            "'skip' confirms the task is already done.",
            {"task_key": str, "decision": str},
        )
        async def resolve_preflight(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            task_key = args["task_key"]
            decision = args["decision"].strip().lower()

            if decision == "dispatch":
                if dependency_manager is not None:
                    await dependency_manager.approve_dispatch(
                        task_key,
                    )
                if preflight_checker is not None:
                    preflight_checker.approve_for_dispatch(
                        task_key,
                    )
                # Epic child: revert DISPATCHED -> READY
                if epic_coordinator is not None and epic_coordinator.is_epic_child(task_key):
                    epic_coordinator.revert_dispatched_to_ready(
                        task_key,
                    )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (f"{task_key} approved for dispatch. Agent will start on next poll."),
                        }
                    ]
                }

            if decision == "skip":
                # Check actual GitHub PR status instead of cached database state
                if storage is not None:
                    try:
                        # Get PR tracking data for this task (filtered query)
                        matching_prs = await storage.load_pr_tracking(task_key=task_key)

                        # If PR exists, check actual GitHub status for ALL PRs
                        if matching_prs:
                            # Check all PRs - reject skip if ANY has conflicts
                            for pr_data in matching_prs:
                                pr_url = pr_data.pr_url

                                # Parse PR URL to extract owner, repo, number
                                match = _PR_URL_PATTERN.match(pr_url)
                                if not match:
                                    logger.error(
                                        "Malformed PR URL for %s: %s - allowing skip (fail-open)",
                                        task_key,
                                        pr_url,
                                    )
                                    # Fail-open: allow skip despite malformed URL
                                    continue
                                if github is None:
                                    logger.warning(
                                        "GitHub client not configured, cannot verify PR state for %s - allowing skip (fail-open)",
                                        task_key,
                                    )
                                    # Fail-open: allow skip when GitHub not configured
                                    continue

                                owner = match.group("owner")
                                repo = match.group("repo")
                                pr_number = int(match.group("number"))

                                try:
                                    # Check actual GitHub PR status
                                    pr_status = await asyncio.to_thread(
                                        github.get_pr_status,
                                        owner,
                                        repo,
                                        pr_number,
                                    )

                                    if pr_status.state == "MERGED":
                                        # PR is merged - update cache and allow skip
                                        logger.info(
                                            "PR %s already merged for %s - updating cache and allowing skip",
                                            pr_url,
                                            task_key,
                                        )
                                        await storage.record_pr_merged(
                                            task_key,
                                            pr_url,
                                            time.time(),
                                        )
                                    elif pr_status.state == "OPEN" and pr_status.mergeable == "CONFLICTING":
                                        # PR has conflicts - reject skip immediately
                                        return {
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": (
                                                        f"Cannot skip {task_key}: PR {pr_url} has merge conflicts. "
                                                        f"Dispatch instead to let the agent resolve them."
                                                    ),
                                                }
                                            ]
                                        }
                                    elif pr_status.state == "OPEN":
                                        # PR is open but mergeable - warn but continue checking other PRs
                                        logger.warning(
                                            "Skipping %s with open PR %s (mergeable=%s) - supervisor confirmed",
                                            task_key,
                                            pr_url,
                                            pr_status.mergeable,
                                        )
                                    else:
                                        # PR is closed (not merged) - continue checking other PRs
                                        logger.info(
                                            "PR %s closed (not merged) for %s - allowing skip",
                                            pr_url,
                                            task_key,
                                        )

                                except Exception:
                                    logger.warning(
                                        "Error checking PR status for %s (PR %s) during skip - allowing skip (fail-open)",
                                        task_key,
                                        pr_url,
                                        exc_info=True,
                                    )
                                    # Fail-open: allow skip if GitHub is unreachable for this PR, but continue checking others

                    except Exception:
                        logger.warning(
                            "Error checking PR status for %s during skip - allowing skip (fail-open)",
                            task_key,
                            exc_info=True,
                        )
                        # Fail-open: supervisor explicitly decided to skip this task,
                        # so infrastructure errors (DB, GitHub API) should not block
                        # their intent. The old fail-closed behavior caused QR-266
                        # where stale cache blocked legitimate skips for 5+ retries.
                if dependency_manager is not None:
                    await dependency_manager.remove_deferred(
                        task_key,
                    )
                if mark_dispatched_callback is not None:
                    mark_dispatched_callback(task_key)
                if event_bus is not None:
                    await event_bus.publish(
                        Event(
                            type=EventType.TASK_SKIPPED,
                            task_key=task_key,
                            data={
                                "reason": ("Confirmed by supervisor"),
                                "source": "supervisor",
                            },
                        )
                    )
                if epic_coordinator is not None and epic_coordinator.is_epic_child(task_key):
                    await epic_coordinator.on_task_completed(
                        task_key,
                    )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (f"{task_key} confirmed as done. Skipped."),
                        }
                    ]
                }

            return {
                "content": [
                    {
                        "type": "text",
                        "text": (f"Invalid decision '{decision}'. Use 'dispatch' or 'skip'."),
                    }
                ]
            }

        @tool(
            "requeue_task",
            "Re-queue a previously skipped task for dispatch. "
            "Use when you change your mind about a skip decision. "
            "The task will be dispatched on next poll.",
            {"task_key": str},
        )
        async def requeue_task(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            task_key = args["task_key"]
            if remove_dispatched_callback is not None:
                remove_dispatched_callback(task_key)
            if clear_recovery_callback is not None:
                clear_recovery_callback(task_key)
            if preflight_checker is not None:
                preflight_checker.approve_for_dispatch(
                    task_key,
                )
            # Epic child: reset to READY if stuck
            if epic_coordinator is not None and epic_coordinator.is_epic_child(task_key):
                epic_key = epic_coordinator.get_parent_epic_key(
                    task_key,
                )
                if epic_key:
                    await epic_coordinator.reset_child(
                        epic_key,
                        task_key,
                    )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (f"{task_key} re-queued. Agent will start on next poll."),
                    }
                ]
            }

        preflight_tools = [resolve_preflight, requeue_task]

    # Diagnostic tools (require both callbacks)
    diagnostic_tools: list[Any] = []
    if get_state_callback is not None and get_task_events_callback is not None:

        @tool(
            "orchestrator_get_state",
            "Get a full snapshot of orchestrator internal state: "
            "dispatched set, active tasks, tracked PRs, "
            "tracked needs-info, proposals, epics, running "
            "sessions, on-demand sessions, supervisor chat, "
            "and config.",
            {},
        )
        async def orchestrator_get_state(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            state = get_state_callback()
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            state,
                            ensure_ascii=False,
                            indent=2,
                            default=str,
                        ),
                    }
                ]
            }

        diagnostic_tools.append(orchestrator_get_state)

        @tool(
            "orchestrator_get_task_events",
            "Get all EventBus events for a specific task key. "
            "Returns chronological event history "
            "(type, timestamp, data).",
            {"task_key": str},
        )
        async def orchestrator_get_task_events(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            task_key = args["task_key"]
            events = get_task_events_callback(task_key)
            if not events:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (f"No events found for {task_key}."),
                        }
                    ]
                }
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            events,
                            ensure_ascii=False,
                            indent=2,
                            default=str,
                        ),
                    }
                ]
            }

        diagnostic_tools.append(orchestrator_get_task_events)

        @tool(
            "orchestrator_diagnose_task",
            "Automated stuck-task diagnostics. Cross-references orchestrator "
            "state, epic child status, EventBus events, and deferred task "
            "status to detect stuck patterns (orphaned epic children, stale "
            "dispatched set, started-but-never-completed tasks).",
            {"task_key": str},
        )
        async def orchestrator_diagnose_task(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            task_key = args["task_key"]
            state = get_state_callback()
            events = get_task_events_callback(task_key)

            lines: list[str] = [f"Task: {task_key}"]
            diagnosis: list[str] = []

            # Orchestrator internal status
            dispatched = state.get("dispatched", [])
            in_dispatched = task_key in dispatched
            lines.append("\nOrchestrator:")
            in_disp = "YES" if in_dispatched else "NO"
            lines.append(f"  - In dispatched set: {in_disp}")

            # Check running sessions
            running = state.get("running_sessions", [])
            has_running = any(
                s.get("task_key") == task_key or s.get("issue_key") == task_key for s in running if isinstance(s, dict)
            )
            running_yn = "YES" if has_running else "NO"
            lines.append(f"  - Active running session: {running_yn}")

            # Check PR tracking
            tracked_prs = state.get("tracked_prs", {})
            pr_tracked = task_key in tracked_prs
            lines.append(f"  - PR tracked: {'YES' if pr_tracked else 'NO'}")
            if pr_tracked:
                pr_info = tracked_prs[task_key]
                lines.append(f"    PR URL: {pr_info.get('pr_url', 'N/A')}")

            # Check needs-info tracking
            tracked_ni = state.get("tracked_needs_info", {})
            ni_tracked = task_key in tracked_ni
            ni_yn = "YES" if ni_tracked else "NO"
            lines.append(f"  - Needs-info tracked: {ni_yn}")

            # Check on-demand sessions
            on_demand = state.get("on_demand_sessions", [])
            has_on_demand = task_key in on_demand
            od_yn = "YES" if has_on_demand else "NO"
            lines.append(f"  - On-demand session: {od_yn}")

            # Epic status
            epics = state.get("epics", {})
            is_epic_child = False
            parent_epic = None
            child_status = None
            child_deps: list[str] = []
            for epic_key, epic_data in epics.items():
                children = epic_data.get("children", {})
                if task_key in children:
                    is_epic_child = True
                    parent_epic = epic_key
                    child_info = children[task_key]
                    child_status = child_info.get("status")
                    child_deps = child_info.get("depends_on", [])
                    break

            lines.append("\nEpic:")
            if is_epic_child:
                lines.append(f"  - Is epic child: YES (parent: {parent_epic})")
                lines.append(f"  - Child status: {child_status}")
                deps_str = ", ".join(child_deps) if child_deps else "none"
                lines.append(f"  - Dependencies: {deps_str}")
            else:
                lines.append("  - Is epic child: NO")

            # Events
            lines.append(f"\nEvents ({len(events)} total, last 5 shown):")
            for ev in events[-5:]:
                ev_type = ev.get("type")
                ev_ts = ev.get("timestamp", "?")
                lines.append(f"  - {ev_type} @ {ev_ts}")

            # Deferred status
            deferred_tasks = state.get("deferred_tasks", {})
            is_deferred = task_key in deferred_tasks
            if is_deferred:
                lines.append("\nDeferred: YES")
                d_info = deferred_tasks[task_key]
                lines.append(f"  Blockers: {d_info.get('blockers', [])}")

            # Diagnosis patterns
            has_session = has_running or pr_tracked or ni_tracked or has_on_demand
            terminal_events = {
                "task_completed",
                "task_failed",
                "pr_tracked",
                "pr_merged",
            }
            has_started = any(ev.get("type") == "task_started" for ev in events)
            has_terminal = any(ev.get("type") in terminal_events for ev in events)

            is_orphaned_child = is_epic_child and child_status == "dispatched" and not in_dispatched and not has_session
            if is_orphaned_child:
                diagnosis.append(
                    "STUCK: Epic child is DISPATCHED but has "
                    "no active session and is not in the "
                    "dispatched set. The agent session was "
                    "lost (e.g., pod restart) and "
                    "reconciliation did not reset it."
                )

            if in_dispatched and not has_session:
                diagnosis.append(
                    "STUCK: Task is in dispatched set but has no active session. The dispatched set may be stale."
                )

            if has_started and not has_terminal:
                diagnosis.append(
                    "STUCK: Task has a task_started event "
                    "but no terminal event "
                    "(completed/failed/pr_tracked). "
                    "The agent may have been interrupted."
                )

            if is_deferred:
                diagnosis.append("BLOCKED: Task is deferred due to unresolved dependencies.")

            if diagnosis:
                lines.append("\nDiagnosis:")
                for d in diagnosis:
                    lines.append(f"  - {d}")
            else:
                lines.append("\nDiagnosis: No stuck patterns detected.")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        diagnostic_tools.append(orchestrator_diagnose_task)

    # Heartbeat tool (only registered if HeartbeatMonitor is provided)
    heartbeat_tools: list[Any] = []
    if heartbeat_monitor is not None:

        @tool(
            "get_agent_health",
            "Get current health report for all running agents. Shows stuck, long-running, and stale review agents.",
            {},
        )
        async def get_agent_health(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            if heartbeat_monitor is None:
                raise RuntimeError("heartbeat_monitor is not set")
            result = heartbeat_monitor.collect_health()
            lines = [
                f"**{result.total_agents}** agent(s), **{result.healthy_agents}** healthy",
            ]
            for r in result.all_agents:
                flags: list[str] = []
                if r.is_stuck:
                    flags.append("STUCK")
                if r.is_long_running:
                    flags.append("LONG")
                if r.is_review_stale:
                    flags.append("STALE")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                lines.append(
                    f"- **{r.task_key}** ({r.status}): "
                    f"{r.issue_summary} — "
                    f"elapsed={int(r.elapsed_seconds)}s, "
                    f"idle={int(r.idle_seconds)}s, "
                    f"compactions={r.compaction_count}"
                    f"{flag_str}"
                )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "\n".join(lines),
                    }
                ]
            }

        heartbeat_tools.append(get_agent_health)

    # GitHub merge tool (only registered if GitHubClient is provided)
    github_merge_tools: list[Any] = []
    if github is not None:

        @tool(
            "github_merge_pr",
            "Merge a GitHub PR (supervisor override). "
            "Checks readiness first, then attempts merge. "
            "Works independently of auto_merge_enabled config.",
            {"owner": str, "repo": str, "pr_number": int},
        )
        async def github_merge_pr(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            owner = args["owner"]
            repo = args["repo"]
            pr_number = args["pr_number"]

            readiness = await asyncio.to_thread(
                github.check_merge_readiness,
                owner,
                repo,
                pr_number,
            )
            if not readiness.is_ready:
                reasons = ", ".join(readiness.reasons)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"PR #{pr_number} is not ready to merge: {reasons}",
                        }
                    ]
                }

            merged = await asyncio.to_thread(
                github.merge_pr,
                owner,
                repo,
                pr_number,
            )
            if merged:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"PR #{pr_number} merged successfully.",
                        }
                    ]
                }
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Failed to merge PR #{pr_number}.",
                    }
                ]
            }

        github_merge_tools.append(github_merge_pr)

    # Epic create child tool (only registered if EpicCoordinator is provided)
    epic_create_tools: list[Any] = []
    if epic_coordinator is not None:

        @tool(
            "epic_create_child",
            "Create a subtask in Tracker and register it as an epic child. "
            "Atomic: creates in Tracker + registers in EpicCoordinator.",
            {
                "epic_key": str,
                "summary": str,
                "description": str,
                "component": str,
                "assignee": str,
            },
        )
        async def epic_create_child(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            if epic_coordinator is None:
                raise RuntimeError("epic_coordinator is not set")
            from orchestrator.epic_coordinator import (
                ChildStatus,
                ChildTask,
            )

            epic_key = args["epic_key"]
            state = epic_coordinator.get_epic_state(epic_key)
            if state is None:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Epic {epic_key} not found.",
                        }
                    ]
                }

            result = await asyncio.to_thread(
                lambda: client.create_issue(
                    queue=tracker_queue,
                    summary=args["summary"],
                    description=args["description"],
                    components=[args["component"]],
                    assignee=args["assignee"],
                    project_id=tracker_project_id,
                    boards=tracker_boards,
                    tags=[tracker_tag],
                    parent=epic_key,
                )
            )
            child_key = result["key"]

            child = ChildTask(
                key=child_key,
                summary=args["summary"],
                status=ChildStatus.PENDING,
                depends_on=[],
                tracker_status="open",
                tags=[tracker_tag],
            )
            registered = epic_coordinator.register_child(epic_key, child)
            if not registered:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Created {child_key} in Tracker but "
                                f"failed to register in epic {epic_key} "
                                f"(epic state missing). Manual intervention "
                                f"required."
                            ),
                        }
                    ]
                }
            on_task_created(child_key)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Created child {child_key} in epic {epic_key}.",
                    }
                ]
            }

        epic_create_tools.append(epic_create_child)

    # --- ADR (Architecture Decision Record) tools ---

    @tool(
        "create_adr",
        "Create an Architecture Decision Record (ADR) document.",
        {
            "title": str,
            "context": str,
            "decision": str,
            "consequences": str,
            "status": str,
        },
    )
    async def adr_create(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        from orchestrator.adr import create_adr as _create_adr

        status = args.get("status", "accepted")
        path = _create_adr(
            title=args["title"],
            context=args["context"],
            decision=args["decision"],
            consequences=args["consequences"],
            status=status,
        )
        return {"content": [{"type": "text", "text": f"Created ADR: {path}"}]}

    @tool(
        "list_adrs",
        "List all Architecture Decision Records.",
        {},
    )
    async def adr_list(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        from orchestrator.adr import list_adrs as _list_adrs

        adrs = _list_adrs()
        if not adrs:
            return {"content": [{"type": "text", "text": "No ADRs found."}]}
        lines = []
        for a in adrs:
            lines.append(f"- {a['date']} | {a['title']} [{a['status']}] ({a['filename']})")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "read_adr",
        "Read the content of an Architecture Decision Record.",
        {"filename": str},
    )
    async def adr_read(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        from orchestrator.adr import read_adr as _read_adr

        try:
            content = _read_adr(args["filename"])
        except FileNotFoundError as exc:
            return {"content": [{"type": "text", "text": str(exc)}]}
        return {"content": [{"type": "text", "text": content}]}

    adr_tools = [adr_create, adr_list, adr_read]

    all_tools = [
        search_issues,
        get_issue,
        get_comments,
        get_checklist,
        get_attachments,
        download_attachment,
        pending_proposals,
        recent_events,
        create_issue,
        update_issue,
        escalate_tool,
    ]
    all_tools.extend(agent_mgmt_tools)
    all_tools.extend(stats_tools)
    all_tools.extend(github_tools)
    all_tools.extend(memory_tools)
    all_tools.extend(epic_tools)
    all_tools.extend(mailbox_tools)
    all_tools.extend(k8s_tools)
    all_tools.extend(dependency_tools)
    all_tools.extend(preflight_tools)
    all_tools.extend(diagnostic_tools)
    all_tools.extend(heartbeat_tools)
    all_tools.extend(github_merge_tools)
    all_tools.extend(epic_create_tools)
    all_tools.extend(adr_tools)
    all_tools.extend(env_config_tools)

    return create_sdk_mcp_server(
        name=_SERVER_NAME,
        version=_SERVER_VERSION,
        tools=all_tools,
    )


def build_supervisor_allowed_tools(
    server_name: str,
    *,
    has_storage: bool = False,
    has_github: bool = False,
    has_memory: bool = False,
    has_agent_mgmt: bool = False,
    has_epics: bool = False,
    has_mailbox: bool = False,
    has_k8s: bool = False,
    has_dependencies: bool = False,
    has_preflight: bool = False,
    has_diagnostics: bool = False,
    has_heartbeat: bool = False,
) -> list[str]:
    """Build the allowed_tools list for a supervisor SDK session.

    Centralizes the tool list that must match the tools registered by
    build_supervisor_server(). Used by both auto-trigger supervisor and
    interactive chat sessions.
    """
    tools = [
        # Full Claude Code tools — supervisor/chat has unrestricted access
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "Task",
        # MCP tools — Tracker, proposals, events
        f"mcp__{server_name}__tracker_search_issues",
        f"mcp__{server_name}__tracker_get_issue",
        f"mcp__{server_name}__tracker_get_comments",
        f"mcp__{server_name}__tracker_get_checklist",
        f"mcp__{server_name}__tracker_get_attachments",
        f"mcp__{server_name}__tracker_download_attachment",
        f"mcp__{server_name}__get_pending_proposals",
        f"mcp__{server_name}__get_recent_events",
        f"mcp__{server_name}__tracker_create_issue",
        f"mcp__{server_name}__tracker_update_issue",
        f"mcp__{server_name}__escalate_to_human",
        f"mcp__{server_name}__create_adr",
        f"mcp__{server_name}__list_adrs",
        f"mcp__{server_name}__read_adr",
    ]
    if has_storage:
        tools.extend(
            [
                f"mcp__{server_name}__stats_query_summary",
                f"mcp__{server_name}__stats_query_costs",
                f"mcp__{server_name}__stats_query_errors",
                f"mcp__{server_name}__stats_query_custom",
                f"mcp__{server_name}__env_get",
                f"mcp__{server_name}__env_set",
                f"mcp__{server_name}__env_list",
            ]
        )
    if has_github:
        tools.extend(
            [
                f"mcp__{server_name}__github_get_pr",
                f"mcp__{server_name}__github_get_pr_diff",
                f"mcp__{server_name}__github_get_pr_files",
                f"mcp__{server_name}__github_get_pr_reviews",
                f"mcp__{server_name}__github_get_pr_checks",
                f"mcp__{server_name}__github_list_prs",
                f"mcp__{server_name}__github_check_pr_mergeability",
            ]
        )
    if has_memory:
        tools.extend(
            [
                f"mcp__{server_name}__memory_search",
                f"mcp__{server_name}__memory_get",
                f"mcp__{server_name}__memory_write",
                f"mcp__{server_name}__memory_list",
            ]
        )
    if has_agent_mgmt:
        tools.extend(
            [
                f"mcp__{server_name}__list_running_tasks",
                f"mcp__{server_name}__send_message_to_task",
                f"mcp__{server_name}__abort_task",
                f"mcp__{server_name}__cancel_task",
            ]
        )
    if has_epics:
        tools.extend(
            [
                f"mcp__{server_name}__epic_list",
                f"mcp__{server_name}__epic_get_children",
                f"mcp__{server_name}__epic_set_plan",
                f"mcp__{server_name}__epic_activate_child",
                f"mcp__{server_name}__epic_reset_child",
            ]
        )
    if has_mailbox:
        tools.extend(
            [
                f"mcp__{server_name}__view_agent_messages",
                f"mcp__{server_name}__get_comm_stats",
            ]
        )
    if has_k8s:
        tools.extend(
            [
                f"mcp__{server_name}__k8s_list_pods",
                f"mcp__{server_name}__k8s_get_pod_logs",
                f"mcp__{server_name}__k8s_get_pod_status",
            ]
        )
    if has_dependencies:
        tools.extend(
            [
                f"mcp__{server_name}__list_deferred_tasks",
                f"mcp__{server_name}__approve_task_dispatch",
                f"mcp__{server_name}__defer_task",
            ]
        )
    if has_preflight:
        tools.extend(
            [
                f"mcp__{server_name}__resolve_preflight",
                f"mcp__{server_name}__requeue_task",
            ]
        )
    if has_diagnostics:
        tools.extend(
            [
                f"mcp__{server_name}__orchestrator_get_state",
                f"mcp__{server_name}__orchestrator_get_task_events",
                f"mcp__{server_name}__orchestrator_diagnose_task",
            ]
        )
    if has_heartbeat:
        tools.append(f"mcp__{server_name}__get_agent_health")
    if has_github:
        tools.append(f"mcp__{server_name}__github_merge_pr")
    if has_epics:
        tools.append(f"mcp__{server_name}__epic_create_child")
    return tools
