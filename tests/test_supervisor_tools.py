"""Tests for supervisor MCP tools."""

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.tracker_client import TrackerIssue


def _build_tools_and_deps(
    proposals_data: list[dict[str, str]] | None = None,
    events_data: list[dict[str, object]] | None = None,
    on_task_created: object | None = None,
    tracker_queue: str = "QR",
    tracker_project_id: int = 13,
    tracker_boards: list[int] | None = None,
    tracker_tag: str = "ai-task",
    storage: object | None = None,
    github: object | None = None,
    list_running_tasks_callback: object | None = None,
    send_message_callback: object | None = None,
    abort_task_callback: object | None = None,
    cancel_task_callback: object | None = None,
    epic_coordinator: object | None = None,
    mailbox: object | None = None,
    k8s_client: object | None = None,
    dependency_manager: object | None = None,
    get_state_callback: object | None = None,
    get_task_events_callback: object | None = None,
    preflight_checker: object | None = None,
    mark_dispatched_callback: object | None = None,
    remove_dispatched_callback: object | None = None,
    clear_recovery_callback: object | None = None,
    event_bus: object | None = None,
) -> tuple[dict[str, Any], MagicMock, list[dict[str, str]], list[dict[str, object]]]:
    """Build supervisor tools and return (tools_by_name, tracker_mock, proposals, events).

    Extracts the actual tool functions from the `create_sdk_mcp_server` call
    so we can invoke them directly in tests.
    """
    from orchestrator.supervisor_tools import build_supervisor_server

    tracker = MagicMock()
    tracker.search.return_value = []
    tracker.get_comments.return_value = []
    tracker.get_checklist.return_value = []
    tracker.create_issue.return_value = {"key": "QR-99"}

    proposals: list[dict[str, str]] = proposals_data if proposals_data is not None else []
    events: list[dict[str, object]] = events_data if events_data is not None else []

    def get_proposals() -> list[dict[str, str]]:
        return proposals

    def get_events(count: int = 50) -> list[dict[str, object]]:
        return events[:count]

    if on_task_created is None:
        on_task_created = MagicMock()

    build_supervisor_server(
        client=tracker,
        get_pending_proposals=get_proposals,
        get_recent_events=get_events,
        on_task_created=on_task_created,
        tracker_queue=tracker_queue,
        tracker_project_id=tracker_project_id,
        tracker_boards=tracker_boards if tracker_boards is not None else [14],
        tracker_tag=tracker_tag,
        storage=storage,
        github=github,
        list_running_tasks_callback=list_running_tasks_callback,
        send_message_callback=send_message_callback,
        abort_task_callback=abort_task_callback,
        cancel_task_callback=cancel_task_callback,
        epic_coordinator=epic_coordinator,
        mailbox=mailbox,
        k8s_client=k8s_client,
        dependency_manager=dependency_manager,
        get_state_callback=get_state_callback,
        get_task_events_callback=get_task_events_callback,
        preflight_checker=preflight_checker,
        mark_dispatched_callback=mark_dispatched_callback,
        remove_dispatched_callback=remove_dispatched_callback,
        clear_recovery_callback=clear_recovery_callback,
        event_bus=event_bus,
    )

    # The mock conftest wraps @tool as a pass-through that sets _tool_name,
    # and create_sdk_mcp_server receives the list of tool functions via `tools=`.
    # Extract them from the create_sdk_mcp_server call args.
    import claude_agent_sdk

    call_args = claude_agent_sdk.create_sdk_mcp_server.call_args
    tool_fns = call_args[1]["tools"] if "tools" in (call_args[1] or {}) else call_args[0][2]

    tools_by_name: dict[str, Any] = {}
    for fn in tool_fns:
        tools_by_name[fn._tool_name] = fn

    return tools_by_name, tracker, proposals, events


class TestTrackerSearchIssues:
    async def test_no_results(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        result = await tools["tracker_search_issues"]({"query": "Queue: QR"})
        assert result["content"][0]["text"] == "No issues found."
        tracker.search.assert_called_once_with("Queue: QR")

    async def test_with_results(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.search.return_value = [
            TrackerIssue(
                key="QR-1",
                summary="Test task",
                description="Description",
                components=["Backend"],
                tags=["ai-task"],
                status="open",
            ),
        ]
        result = await tools["tracker_search_issues"]({"query": "Queue: QR"})
        text = result["content"][0]["text"]
        assert "QR-1" in text
        assert "Test task" in text


class TestTrackerGetIssue:
    async def test_returns_formatted_issue(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.get_issue.return_value = TrackerIssue(
            key="QR-5",
            summary="Important task",
            description="Do something",
            components=["Frontend"],
            tags=[],
            status="inProgress",
        )
        result = await tools["tracker_get_issue"]({"issue_key": "QR-5"})
        text = result["content"][0]["text"]
        assert "QR-5" in text
        assert "Important task" in text
        tracker.get_issue.assert_called_once_with("QR-5")


class TestTrackerGetComments:
    async def test_no_comments(self) -> None:
        tools, _tracker, _, _ = _build_tools_and_deps()
        result = await tools["tracker_get_comments"]({"issue_key": "QR-1"})
        assert result["content"][0]["text"] == "No comments."

    async def test_with_comments(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.get_comments.return_value = [
            {"createdBy": {"display": "John"}, "text": "Hello", "createdAt": "2025-01-01"},
        ]
        result = await tools["tracker_get_comments"]({"issue_key": "QR-1"})
        text = result["content"][0]["text"]
        assert "John" in text
        assert "Hello" in text


class TestTrackerGetChecklist:
    async def test_no_items(self) -> None:
        tools, _tracker, _, _ = _build_tools_and_deps()
        result = await tools["tracker_get_checklist"]({"issue_key": "QR-1"})
        assert result["content"][0]["text"] == "No checklist items."

    async def test_with_items(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.get_checklist.return_value = [
            {"text": "Step 1", "checked": True},
            {"text": "Step 2", "checked": False},
        ]
        result = await tools["tracker_get_checklist"]({"issue_key": "QR-1"})
        text = result["content"][0]["text"]
        assert "[x] Step 1" in text
        assert "[ ] Step 2" in text


class TestTrackerGetAttachments:
    async def test_returns_attachments(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.get_attachments.return_value = [{"id": 71, "name": "sitemap.xml", "mimetype": "text/xml", "size": 1024}]
        result = await tools["tracker_get_attachments"]({"issue_key": "QR-173"})
        text = result["content"][0]["text"]
        assert "sitemap.xml" in text
        assert "71" in text
        tracker.get_attachments.assert_called_once_with("QR-173")

    async def test_no_attachments(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.get_attachments.return_value = []
        result = await tools["tracker_get_attachments"]({"issue_key": "QR-1"})
        assert "No attachments" in result["content"][0]["text"]


class TestTrackerDownloadAttachment:
    async def test_downloads_text_file(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.download_attachment.return_value = (b"<xml>content</xml>", "text/xml")
        result = await tools["tracker_download_attachment"]({"attachment_id": 71})
        text = result["content"][0]["text"]
        assert "<xml>content</xml>" in text
        tracker.download_attachment.assert_called_once_with(71)

    async def test_binary_file_metadata_only(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.download_attachment.return_value = (b"\x89PNG", "image/png")
        result = await tools["tracker_download_attachment"]({"attachment_id": 99})
        text = result["content"][0]["text"]
        assert "Binary file" in text
        assert "image/png" in text

    async def test_validates_attachment_belongs_to_issue(self) -> None:
        """Test that download_attachment validates attachment belongs to specified issue."""
        tools, tracker, _, _ = _build_tools_and_deps()
        # Setup: issue QR-1 has attachment 71, but we try to download attachment 99
        tracker.get_attachments.return_value = [{"id": 71, "name": "file1.txt", "mimetype": "text/plain", "size": 100}]
        tracker.download_attachment.return_value = (b"content", "text/plain")

        # Try to download attachment 99 claiming it belongs to QR-1 (it doesn't)
        result = await tools["tracker_download_attachment"]({"attachment_id": 99, "issue_key": "QR-1"})
        text = result["content"][0]["text"]

        # Should reject the download
        assert "Error" in text
        assert "does not belong to issue" in text or "not found in issue" in text
        tracker.download_attachment.assert_not_called()

    async def test_allows_download_when_attachment_belongs_to_issue(self) -> None:
        """Test that download succeeds when attachment belongs to the specified issue."""
        tools, tracker, _, _ = _build_tools_and_deps()
        # Setup: issue QR-1 has attachment 71
        tracker.get_attachments.return_value = [{"id": 71, "name": "file1.txt", "mimetype": "text/plain", "size": 100}]
        tracker.download_attachment.return_value = (b"file content", "text/plain")

        # Download attachment 71 from QR-1 (valid)
        result = await tools["tracker_download_attachment"]({"attachment_id": 71, "issue_key": "QR-1"})
        text = result["content"][0]["text"]

        # Should succeed
        assert "file content" in text
        tracker.download_attachment.assert_called_once_with(71)

    async def test_download_without_issue_key_works(self) -> None:
        """Test that download works without issue_key parameter (optional)."""
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.download_attachment.return_value = (b"content", "text/plain")

        # Download without issue_key (supervisor has unrestricted access)
        result = await tools["tracker_download_attachment"]({"attachment_id": 99})
        text = result["content"][0]["text"]

        # Should succeed without validation
        assert "content" in text
        tracker.download_attachment.assert_called_once_with(99)
        # get_attachments should NOT be called when issue_key is not provided
        tracker.get_attachments.assert_not_called()

    async def test_type_normalization_in_attachment_validation(self) -> None:
        """Test that attachment ID comparison works with mixed int/str types."""
        tools, tracker, _, _ = _build_tools_and_deps()
        # API returns int IDs
        tracker.get_attachments.return_value = [{"id": 71, "name": "file.txt", "mimetype": "text/plain", "size": 100}]
        tracker.download_attachment.return_value = (b"content", "text/plain")

        # Download with int attachment_id matching int ID from API
        result = await tools["tracker_download_attachment"]({"attachment_id": 71, "issue_key": "QR-1"})
        text = result["content"][0]["text"]

        # Should succeed - types are normalized to string for comparison
        assert "content" in text
        tracker.download_attachment.assert_called_once_with(71)


class TestGetPendingProposals:
    async def test_empty(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        result = await tools["get_pending_proposals"]({})
        assert result["content"][0]["text"] == "No pending proposals."

    async def test_with_proposals(self) -> None:
        proposals = [
            {
                "summary": "Add retry logic",
                "description": "Tracker API sometimes times out",
                "component": "backend",
                "category": "tooling",
            },
        ]
        tools, _, _, _ = _build_tools_and_deps(proposals_data=proposals)
        result = await tools["get_pending_proposals"]({})
        text = result["content"][0]["text"]
        assert "Add retry logic" in text
        assert "Tracker API sometimes times out" in text


class TestGetRecentEvents:
    async def test_empty(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        result = await tools["get_recent_events"]({"count": 10})
        assert result["content"][0]["text"] == "No recent events."

    async def test_with_events(self) -> None:
        events: list[dict[str, object]] = [
            {
                "type": "task_completed",
                "task_key": "QR-1",
                "timestamp": 1000.0,
                "data": {},
            },
        ]
        tools, _, _, _ = _build_tools_and_deps(events_data=events)
        result = await tools["get_recent_events"]({"count": 50})
        text = result["content"][0]["text"]
        assert "task_completed" in text
        assert "QR-1" in text

    async def test_respects_count(self) -> None:
        events: list[dict[str, object]] = [
            {"type": "task_completed", "task_key": f"QR-{i}", "timestamp": float(i), "data": {}} for i in range(5)
        ]
        tools, _, _, _ = _build_tools_and_deps(events_data=events)
        result = await tools["get_recent_events"]({"count": 2})
        text = result["content"][0]["text"]
        # Should only contain first 2 events
        assert "QR-0" in text
        assert "QR-1" in text
        assert "QR-2" not in text


class TestNoUnusedDataclasses:
    def test_prlifecycle_not_in_stats_models(self) -> None:
        """PRLifecycle was dead code — it should have been removed."""
        import ast
        from pathlib import Path

        source = Path("orchestrator/stats_models.py").read_text()
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "PRLifecycle" not in class_names, "PRLifecycle is dead code and should be removed"


class TestNoPrivateImports:
    def test_no_private_imports_from_tracker_tools(self) -> None:
        """supervisor_tools must not import private (_prefixed) names from tracker_tools."""
        import ast
        from pathlib import Path

        source = Path("orchestrator/supervisor_tools.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "orchestrator.tracker_tools":
                for alias in node.names:
                    assert not alias.name.startswith("_"), (
                        f"Private import '{alias.name}' from tracker_tools violates code quality rules"
                    )


class TestAsyncTrackerCalls:
    """PR review: sync tracker methods in async handlers block the event loop.

    All tracker client calls (search, get_issue, get_comments, get_checklist)
    must be wrapped in asyncio.to_thread to avoid blocking the event loop.
    """

    def test_supervisor_tools_use_to_thread(self) -> None:
        """All tracker calls in supervisor_tools.py must use asyncio.to_thread."""
        import ast
        from pathlib import Path

        source = Path("orchestrator/supervisor_tools.py").read_text()
        tree = ast.parse(source)

        # Find all async function definitions inside build_supervisor_server
        blocking_methods = {
            "search",
            "get_issue",
            "get_comments",
            "get_checklist",
            "get_attachments",
            "download_attachment",
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            # Check if any direct call to client.<blocking_method> exists
            # (should be wrapped in to_thread instead)
            for child in ast.walk(node):
                if isinstance(child, ast.Await) and isinstance(child.value, ast.Call):
                    func = child.value.func
                    # Look for: await asyncio.to_thread(client.method, ...)
                    # This is OK — it means the call IS wrapped
                    continue
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Attribute) and func.attr in blocking_methods:
                        # Direct call to client.search() etc. — check it's inside to_thread
                        # Walk up to find if it's an arg to asyncio.to_thread
                        # Simpler: check the parent Await wraps a to_thread call
                        parent_is_to_thread = False
                        for parent_node in ast.walk(node):
                            if isinstance(parent_node, ast.Call):
                                pfunc = parent_node.func
                                if isinstance(pfunc, ast.Attribute) and pfunc.attr == "to_thread":
                                    for arg in parent_node.args:
                                        if arg is func or (isinstance(arg, ast.Attribute) and arg.attr == func.attr):
                                            parent_is_to_thread = True
                        if not parent_is_to_thread:
                            raise AssertionError(
                                f"Direct blocking call to client.{func.attr}() in async function "
                                f"{node.name} — must use asyncio.to_thread()"
                            )


class TestTrackerCreateIssue:
    async def test_creates_issue_and_invokes_callback(self) -> None:
        callback = MagicMock()
        tools, tracker, _, _ = _build_tools_and_deps(on_task_created=callback)
        tracker.create_issue.return_value = {"key": "QR-42"}

        result = await tools["tracker_create_issue"](
            {
                "summary": "Fix bug",
                "description": "Something is broken",
                "component": "Бекенд",
                "assignee": "john.doe",
            }
        )

        text = result["content"][0]["text"]
        assert "QR-42" in text
        tracker.create_issue.assert_called_once_with(
            queue="QR",
            summary="Fix bug",
            description="Something is broken",
            components=["Бекенд"],
            assignee="john.doe",
            project_id=13,
            boards=[14],
            tags=["ai-task"],
        )
        callback.assert_called_once_with("QR-42")

    async def test_returns_created_key(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.create_issue.return_value = {"key": "QR-55"}

        result = await tools["tracker_create_issue"](
            {
                "summary": "Add feature",
                "description": "Details",
                "component": "Фронтенд",
                "assignee": "jane.doe",
            }
        )

        assert "QR-55" in result["content"][0]["text"]


class TestTrackerUpdateIssue:
    def test_schema_only_requires_issue_key(self) -> None:
        """summary, description, tags must be optional — only issue_key is required."""
        tools, _, _, _ = _build_tools_and_deps()
        schema = tools["tracker_update_issue"]._tool_schema  # type: ignore[attr-defined]
        assert set(schema.keys()) == {"issue_key"}

    async def test_updates_tags(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.update_issue.return_value = {}

        result = await tools["tracker_update_issue"]({"issue_key": "QR-1", "tags": ["ai-task", "urgent"]})

        text = result["content"][0]["text"]
        assert "QR-1" in text
        assert "tags=" in text
        tracker.update_issue.assert_called_once_with("QR-1", summary=None, description=None, tags=["ai-task", "urgent"])

    async def test_updates_description(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.update_issue.return_value = {}

        result = await tools["tracker_update_issue"]({"issue_key": "QR-2", "description": "new desc"})

        text = result["content"][0]["text"]
        assert "description" in text
        tracker.update_issue.assert_called_once_with("QR-2", summary=None, description="new desc", tags=None)

    async def test_updates_summary(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.update_issue.return_value = {}

        result = await tools["tracker_update_issue"]({"issue_key": "QR-3", "summary": "new title"})

        text = result["content"][0]["text"]
        assert "summary" in text
        tracker.update_issue.assert_called_once_with("QR-3", summary="new title", description=None, tags=None)

    async def test_updates_multiple_fields(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.update_issue.return_value = {}

        await tools["tracker_update_issue"]({"issue_key": "QR-4", "summary": "new title", "tags": ["foo"]})

        tracker.update_issue.assert_called_once_with("QR-4", summary="new title", description=None, tags=["foo"])

    async def test_no_fields_returns_error(self) -> None:
        tools, tracker, _, _ = _build_tools_and_deps()
        tracker.update_issue.side_effect = ValueError("At least one field must be provided.")

        result = await tools["tracker_update_issue"]({"issue_key": "QR-5"})

        assert "Error:" in result["content"][0]["text"]


class TestBuildSupervisorServer:
    def test_creates_server_with_all_tools(self) -> None:
        github = MagicMock()
        k8s = _make_k8s_client()
        tools, _, _, _ = _build_tools_and_deps(github=github, k8s_client=k8s)
        expected_tools = {
            "tracker_search_issues",
            "tracker_get_issue",
            "tracker_get_comments",
            "tracker_get_checklist",
            "tracker_get_attachments",
            "tracker_download_attachment",
            "get_pending_proposals",
            "get_recent_events",
            "tracker_create_issue",
            "tracker_update_issue",
            "escalate_to_human",
            "github_get_pr",
            "github_get_pr_diff",
            "github_get_pr_files",
            "github_get_pr_reviews",
            "github_get_pr_checks",
            "github_list_prs",
            "github_check_pr_mergeability",
            "github_merge_pr",
            "k8s_list_pods",
            "k8s_get_pod_logs",
            "k8s_get_pod_status",
            "create_adr",
            "list_adrs",
            "read_adr",
        }
        assert set(tools.keys()) == expected_tools

    def test_no_github_tools_when_github_is_none(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(github=None)
        expected_tools = {
            "tracker_search_issues",
            "tracker_get_issue",
            "tracker_get_comments",
            "tracker_get_checklist",
            "tracker_get_attachments",
            "tracker_download_attachment",
            "get_pending_proposals",
            "get_recent_events",
            "tracker_create_issue",
            "tracker_update_issue",
            "escalate_to_human",
            "create_adr",
            "list_adrs",
            "read_adr",
        }
        assert set(tools.keys()) == expected_tools

    def test_includes_stats_tools_when_storage_provided(self) -> None:
        storage = AsyncMock()
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        stats_tool_names = {
            "stats_query_summary",
            "stats_query_costs",
            "stats_query_errors",
            "stats_query_custom",
        }
        assert stats_tool_names.issubset(set(tools.keys()))

    def test_no_stats_tools_when_storage_is_none(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(storage=None)
        assert "stats_query_summary" not in tools
        assert "stats_query_costs" not in tools
        assert "stats_query_errors" not in tools
        assert "stats_query_custom" not in tools


class TestStatsQuerySummary:
    async def test_returns_summary(self) -> None:
        storage = AsyncMock()
        storage.get_summary.return_value = {
            "total_tasks": 10,
            "success_rate": 80.0,
            "total_cost": 5.0,
            "avg_duration": 120.0,
            "days": 7,
        }
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_summary"]({"days": 7})
        text = result["content"][0]["text"]
        assert "10" in text
        assert "80.0" in text
        storage.get_summary.assert_called_once_with(days=7)

    async def test_uses_default_days(self) -> None:
        storage = AsyncMock()
        storage.get_summary.return_value = {"total_tasks": 0}
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        await tools["stats_query_summary"]({})
        storage.get_summary.assert_called_once_with(days=7)


class TestStatsQueryCosts:
    async def test_returns_costs_by_model(self) -> None:
        storage = AsyncMock()
        storage.get_costs.return_value = [
            {"group": "sonnet", "total_cost": 3.0, "count": 5},
        ]
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_costs"]({"group_by": "model", "days": 7, "limit": 10})
        text = result["content"][0]["text"]
        assert "sonnet" in text
        assert "3.0" in text

    async def test_empty_costs(self) -> None:
        storage = AsyncMock()
        storage.get_costs.return_value = []
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_costs"]({"group_by": "model"})
        assert "No cost data" in result["content"][0]["text"]


class TestStatsQueryErrors:
    async def test_returns_error_stats(self) -> None:
        storage = AsyncMock()
        storage.get_error_stats.return_value = [
            {"category": "timeout", "count": 3, "retryable_count": 2},
        ]
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_errors"]({"days": 7})
        text = result["content"][0]["text"]
        assert "timeout" in text

    async def test_no_errors(self) -> None:
        storage = AsyncMock()
        storage.get_error_stats.return_value = []
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_errors"]({"days": 7})
        assert "No errors" in result["content"][0]["text"]


class TestStatsQueryCustom:
    async def test_valid_select(self) -> None:
        storage = AsyncMock()
        storage.execute_readonly.return_value = [{"val": 42}]
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_custom"]({"query": "SELECT 42 AS val"})
        text = result["content"][0]["text"]
        assert "42" in text

    async def test_rejected_write(self) -> None:
        storage = AsyncMock()
        storage.execute_readonly.side_effect = ValueError("Only SELECT statements are allowed")
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_custom"]({"query": "DROP TABLE task_runs"})
        text = result["content"][0]["text"]
        assert "rejected" in text.lower()

    async def test_no_results(self) -> None:
        storage = AsyncMock()
        storage.execute_readonly.return_value = []
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_custom"]({"query": "SELECT * FROM task_runs WHERE 0"})
        assert "No results" in result["content"][0]["text"]

    async def test_timeout_handled_gracefully(self) -> None:
        """TimeoutError from execute_readonly should be caught and reported, not crash."""
        storage = AsyncMock()
        storage.execute_readonly.side_effect = TimeoutError("query timed out")
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_custom"]({"query": "SELECT * FROM task_runs"})
        text = result["content"][0]["text"]
        assert "timed out" in text.lower() or "timeout" in text.lower()

    async def test_operational_error_handled_gracefully(self) -> None:
        """sqlite3.OperationalError (bad SQL syntax) should be caught, not crash."""
        import sqlite3

        storage = AsyncMock()
        storage.execute_readonly.side_effect = sqlite3.OperationalError("no such table: foo")
        tools, _, _, _ = _build_tools_and_deps(storage=storage)
        result = await tools["stats_query_custom"]({"query": "SELECT * FROM foo"})
        text = result["content"][0]["text"]
        assert "error" in text.lower() or "no such table" in text.lower()


class TestGitHubGetPR:
    async def test_returns_pr_details(self) -> None:
        from orchestrator.github_client import PRDetails

        github = MagicMock()
        github.get_pr_details.return_value = PRDetails(
            title="Add feature",
            body="Some description",
            author="dev1",
            base_branch="main",
            head_branch="feat/foo",
            state="OPEN",
            review_decision="APPROVED",
            additions=50,
            deletions=10,
            changed_files=3,
        )
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr"]({"owner": "o", "repo": "r", "pr_number": 1})
        text = result["content"][0]["text"]
        assert "Add feature" in text
        assert "dev1" in text
        assert "OPEN" in text
        assert "+50" in text
        assert "-10" in text


class TestGitHubGetPRDiff:
    async def test_returns_diff(self) -> None:
        github = MagicMock()
        github.get_pr_diff.return_value = "diff --git a/file.py b/file.py\n+new"
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr_diff"]({"owner": "o", "repo": "r", "pr_number": 1})
        text = result["content"][0]["text"]
        assert "diff --git" in text

    async def test_empty_diff(self) -> None:
        github = MagicMock()
        github.get_pr_diff.return_value = ""
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr_diff"]({"owner": "o", "repo": "r", "pr_number": 1})
        assert result["content"][0]["text"] == "(empty diff)"


class TestGitHubGetPRFiles:
    async def test_returns_files(self) -> None:
        from orchestrator.github_client import PRFile

        github = MagicMock()
        github.get_pr_files.return_value = [
            PRFile(filename="src/main.py", status="modified", additions=10, deletions=2, patch="@@ +code"),
        ]
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr_files"]({"owner": "o", "repo": "r", "pr_number": 1})
        text = result["content"][0]["text"]
        assert "src/main.py" in text
        assert "modified" in text

    async def test_empty_files(self) -> None:
        github = MagicMock()
        github.get_pr_files.return_value = []
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr_files"]({"owner": "o", "repo": "r", "pr_number": 1})
        assert result["content"][0]["text"] == "No changed files."


class TestGitHubGetPRReviews:
    async def test_returns_threads(self) -> None:
        from orchestrator.github_client import ReviewThread, ThreadComment

        github = MagicMock()
        github.get_review_threads.return_value = [
            ReviewThread(
                id="T_1",
                is_resolved=False,
                path="src/main.py",
                line=42,
                comments=[ThreadComment(author="reviewer", body="Fix this", created_at="2025-01-01")],
            ),
            ReviewThread(
                id="T_2",
                is_resolved=True,
                path="src/utils.py",
                line=10,
                comments=[ThreadComment(author="dev", body="Done", created_at="2025-01-02")],
            ),
        ]
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr_reviews"]({"owner": "o", "repo": "r", "pr_number": 1})
        text = result["content"][0]["text"]
        assert "UNRESOLVED" in text
        assert "resolved" in text
        assert "Fix this" in text

    async def test_empty_threads(self) -> None:
        github = MagicMock()
        github.get_review_threads.return_value = []
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr_reviews"]({"owner": "o", "repo": "r", "pr_number": 1})
        assert result["content"][0]["text"] == "No review threads."


class TestGitHubGetPRChecks:
    async def test_returns_checks(self) -> None:
        from orchestrator.github_client import CheckResult

        github = MagicMock()
        github.get_all_checks.return_value = [
            CheckResult(name="tests", status="COMPLETED", conclusion="SUCCESS", details_url=None, summary=None),
            CheckResult(
                name="lint",
                status="COMPLETED",
                conclusion="FAILURE",
                details_url="https://ci.example.com",
                summary="Failed",
            ),
        ]
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr_checks"]({"owner": "o", "repo": "r", "pr_number": 1})
        text = result["content"][0]["text"]
        assert "tests" in text
        assert "SUCCESS" in text
        assert "lint" in text
        assert "FAILURE" in text

    async def test_empty_checks(self) -> None:
        github = MagicMock()
        github.get_all_checks.return_value = []
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_get_pr_checks"]({"owner": "o", "repo": "r", "pr_number": 1})
        assert result["content"][0]["text"] == "No CI checks found."


class TestGitHubListPRs:
    async def test_returns_prs(self) -> None:
        github = MagicMock()
        github.list_prs.return_value = [
            {
                "number": 1,
                "title": "First",
                "state": "open",
                "author": "dev1",
                "head_branch": "feat/a",
                "base_branch": "main",
            },
        ]
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_list_prs"]({"owner": "o", "repo": "r", "state": "open"})
        text = result["content"][0]["text"]
        assert "#1" in text
        assert "First" in text

    async def test_empty_prs(self) -> None:
        github = MagicMock()
        github.list_prs.return_value = []
        tools, _, _, _ = _build_tools_and_deps(github=github)
        result = await tools["github_list_prs"]({"owner": "o", "repo": "r", "state": "open"})
        assert result["content"][0]["text"] == "No PRs found."


# ---------------------------------------------------------------------------
# Memory tools tests (new: memory_search, memory_get, memory_write)
# ---------------------------------------------------------------------------


async def _build_memory_tools(
    memory_dir: Path,
    index_path: Path,
) -> tuple[dict[str, Any], MagicMock, Any]:
    """Build supervisor tools with real MemoryIndex and mock embedder.

    Returns (tools_dict, mock_embedder, memory_index) — memory_index is
    the initialized instance captured by the tool closures.
    """
    from orchestrator.supervisor_memory import EmbeddingClient, MemoryIndex

    memory_index = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
    await memory_index.initialize()
    embedder = MagicMock(spec=EmbeddingClient)
    embedder.embed.return_value = [0.1] * 768

    from orchestrator.supervisor_tools import build_supervisor_server

    tracker = MagicMock()
    tracker.search.return_value = []
    tracker.get_comments.return_value = []
    tracker.get_checklist.return_value = []
    tracker.create_issue.return_value = {"key": "QR-99"}

    build_supervisor_server(
        client=tracker,
        get_pending_proposals=list,
        get_recent_events=lambda count: [],
        on_task_created=MagicMock(),
        tracker_queue="QR",
        tracker_project_id=13,
        tracker_boards=[14],
        tracker_tag="ai-task",
        memory_index=memory_index,
        embedder=embedder,
    )

    import claude_agent_sdk

    call_args = claude_agent_sdk.create_sdk_mcp_server.call_args
    tool_fns = call_args[1]["tools"] if "tools" in (call_args[1] or {}) else call_args[0][2]
    tools = {fn._tool_name: fn for fn in tool_fns}
    return tools, embedder, memory_index


class TestMemorySearchTool:
    """Tests for memory_search tool."""

    async def test_returns_results(self, tmp_path: Path) -> None:
        """memory_search should find indexed content via the tool."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("# Knowledge\n\nWe use FastAPI for the web server.\n")
        index_path = tmp_path / ".index.sqlite"

        tools, embedder, memory_index = await _build_memory_tools(memory_dir, index_path)

        # Sync the index so search has data
        await memory_index.sync(embedder)

        result = await tools["memory_search"]({"query": "FastAPI", "max_results": 5, "min_score": 0.0})
        text = result["content"][0]["text"]
        assert "Found" in text
        assert "FastAPI" in text

    async def test_no_results(self, tmp_path: Path) -> None:
        """memory_search with no indexed data returns no results message."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)

        result = await tools["memory_search"]({"query": "anything", "max_results": 5, "min_score": 0.0})
        text = result["content"][0]["text"]
        assert "No relevant memories" in text


class TestMemoryGetTool:
    """Tests for memory_get tool."""

    async def test_reads_file(self, tmp_path: Path) -> None:
        """memory_get should read contents of a memory file."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("Line 1\nLine 2\nLine 3\n")
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)
        result = await tools["memory_get"]({"path": "MEMORY.md"})
        text = result["content"][0]["text"]
        assert "Line 1" in text
        assert "Line 2" in text
        assert "Line 3" in text

    async def test_rejects_non_md(self, tmp_path: Path) -> None:
        """memory_get should reject non-.md files."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)
        result = await tools["memory_get"]({"path": "secret.txt"})
        assert "Only .md files" in result["content"][0]["text"]

    async def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        """memory_get should reject path traversal attempts."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)
        result = await tools["memory_get"]({"path": "../../../etc/passwd.md"})
        assert "Path traversal" in result["content"][0]["text"]

    async def test_file_not_found(self, tmp_path: Path) -> None:
        """memory_get should report file not found."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)
        result = await tools["memory_get"]({"path": "nonexistent.md"})
        assert "not found" in result["content"][0]["text"].lower()

    async def test_from_line_and_lines_params(self, tmp_path: Path) -> None:
        """memory_get should support from_line and lines parameters."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n")
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)
        result = await tools["memory_get"]({"path": "MEMORY.md", "from_line": 2, "lines": 2})
        text = result["content"][0]["text"]
        assert "Line 2" in text
        assert "Line 3" in text
        assert "Line 1" not in text
        assert "Line 4" not in text


class TestMemoryWriteTool:
    """Tests for memory_write tool."""

    async def test_creates_new_file(self, tmp_path: Path) -> None:
        """memory_write should create a new file if it doesn't exist."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)

        result = await tools["memory_write"]({"path": "2026-02-16.md", "content": "## Today\n\nNew entry."})
        text = result["content"][0]["text"]
        assert "Written to 2026-02-16.md" in text

        # Verify file was created
        created = memory_dir / "2026-02-16.md"
        assert created.exists()
        content = created.read_text()
        assert "## Today" in content
        assert "New entry" in content

    async def test_appends_to_existing_file(self, tmp_path: Path) -> None:
        """memory_write should append to existing file."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("# Knowledge\n\nExisting content.\n")
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)
        result = await tools["memory_write"]({"path": "MEMORY.md", "content": "\n## New Section\n\nAppended."})
        text = result["content"][0]["text"]
        assert "Written to MEMORY.md" in text

        content = (memory_dir / "MEMORY.md").read_text()
        assert "Existing content" in content
        assert "New Section" in content
        assert "Appended" in content

    async def test_rejects_non_md(self, tmp_path: Path) -> None:
        """memory_write should reject non-.md files."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)
        result = await tools["memory_write"]({"path": "secret.py", "content": "import os"})
        assert "Only .md files" in result["content"][0]["text"]

    async def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        """memory_write should reject path traversal."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index_path = tmp_path / ".index.sqlite"

        tools, _, _ = await _build_memory_tools(memory_dir, index_path)
        result = await tools["memory_write"]({"path": "../../etc/hacked.md", "content": "bad"})
        assert "Path traversal" in result["content"][0]["text"]

    async def test_reindex_failure_reports_write_success(self, tmp_path: Path) -> None:
        """If file write succeeds but reindex fails, the tool should report write success (not error)."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index_path = tmp_path / ".index.sqlite"

        tools, _, memory_index = await _build_memory_tools(memory_dir, index_path)

        # Make reindex_file raise an exception (e.g. embedding API timeout)
        with patch.object(memory_index, "reindex_file", side_effect=RuntimeError("embedding API timeout")):
            result = await tools["memory_write"]({"path": "2026-02-16.md", "content": "## Today\n\nNew entry."})

        text = result["content"][0]["text"]
        # The file was written — tool should report success, not "Error writing"
        assert "Written to 2026-02-16.md" in text

        # Verify the file was actually created on disk
        created = memory_dir / "2026-02-16.md"
        assert created.exists()
        assert "New entry" in created.read_text()


class TestBuildSupervisorAllowedTools:
    """Tests for the build_supervisor_allowed_tools() pure function."""

    def test_base_tools_always_present(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor")
        # Filesystem tools
        assert "Read" in tools
        assert "Glob" in tools
        assert "Grep" in tools
        # Web search tools
        assert "WebSearch" in tools
        assert "WebFetch" in tools
        # Tracker MCP tools
        assert "mcp__supervisor__tracker_search_issues" in tools
        assert "mcp__supervisor__tracker_get_issue" in tools
        assert "mcp__supervisor__tracker_get_comments" in tools
        assert "mcp__supervisor__tracker_get_checklist" in tools
        assert "mcp__supervisor__tracker_get_attachments" in tools
        assert "mcp__supervisor__tracker_download_attachment" in tools
        assert "mcp__supervisor__get_pending_proposals" in tools
        assert "mcp__supervisor__get_recent_events" in tools
        assert "mcp__supervisor__tracker_create_issue" in tools
        # ADR tools
        assert "mcp__supervisor__create_adr" in tools
        assert "mcp__supervisor__list_adrs" in tools
        assert "mcp__supervisor__read_adr" in tools

    def test_stats_tools_when_storage(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_storage=True)
        assert "mcp__supervisor__stats_query_summary" in tools
        assert "mcp__supervisor__stats_query_costs" in tools
        assert "mcp__supervisor__stats_query_errors" in tools
        assert "mcp__supervisor__stats_query_custom" in tools

    def test_no_stats_tools_when_no_storage(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_storage=False)
        assert "mcp__supervisor__stats_query_summary" not in tools
        assert "mcp__supervisor__stats_query_costs" not in tools
        assert "mcp__supervisor__stats_query_errors" not in tools
        assert "mcp__supervisor__stats_query_custom" not in tools

    def test_github_tools_when_github(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_github=True)
        assert "mcp__supervisor__github_get_pr" in tools
        assert "mcp__supervisor__github_get_pr_diff" in tools
        assert "mcp__supervisor__github_get_pr_files" in tools
        assert "mcp__supervisor__github_get_pr_reviews" in tools
        assert "mcp__supervisor__github_get_pr_checks" in tools
        assert "mcp__supervisor__github_list_prs" in tools
        assert "mcp__supervisor__github_check_pr_mergeability" in tools

    def test_no_github_tools_when_no_github(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_github=False)
        assert "mcp__supervisor__github_get_pr" not in tools
        assert "mcp__supervisor__github_list_prs" not in tools
        assert "mcp__supervisor__github_check_pr_mergeability" not in tools

    def test_memory_tools_when_memory(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_memory=True)
        assert "mcp__supervisor__memory_search" in tools
        assert "mcp__supervisor__memory_get" in tools
        assert "mcp__supervisor__memory_write" in tools

    def test_no_memory_tools_when_no_memory(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_memory=False)
        assert "mcp__supervisor__memory_search" not in tools
        assert "mcp__supervisor__memory_get" not in tools
        assert "mcp__supervisor__memory_write" not in tools


class TestMemoryToolsRegistration:
    """Tests for memory tools registration."""

    def test_memory_tools_included_when_both_index_and_embedder_provided(self) -> None:
        """Memory tools should be registered when both memory_index and embedder are provided."""
        from orchestrator.supervisor_memory import EmbeddingClient, MemoryIndex

        memory_index = MagicMock(spec=MemoryIndex)
        embedder = MagicMock(spec=EmbeddingClient)

        from orchestrator.supervisor_tools import build_supervisor_server

        tracker = MagicMock()
        build_supervisor_server(
            client=tracker,
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            on_task_created=MagicMock(),
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
            storage=AsyncMock(),
            github=MagicMock(),
            memory_index=memory_index,
            embedder=embedder,
        )

        import claude_agent_sdk

        call_args = claude_agent_sdk.create_sdk_mcp_server.call_args
        tool_fns = call_args[1]["tools"] if "tools" in (call_args[1] or {}) else call_args[0][2]
        tool_names = {fn._tool_name for fn in tool_fns}

        assert "memory_search" in tool_names
        assert "memory_get" in tool_names
        assert "memory_write" in tool_names
        assert "memory_list" in tool_names

    def test_memory_tools_excluded_when_only_index_provided(self) -> None:
        """Memory tools should NOT be registered when only memory_index is provided (no embedder)."""
        from orchestrator.supervisor_memory import MemoryIndex

        memory_index = MagicMock(spec=MemoryIndex)

        from orchestrator.supervisor_tools import build_supervisor_server

        tracker = MagicMock()
        build_supervisor_server(
            client=tracker,
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            on_task_created=MagicMock(),
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
            storage=AsyncMock(),
            github=MagicMock(),
            memory_index=memory_index,
            embedder=None,
        )

        import claude_agent_sdk

        call_args = claude_agent_sdk.create_sdk_mcp_server.call_args
        tool_fns = call_args[1]["tools"] if "tools" in (call_args[1] or {}) else call_args[0][2]
        tool_names = {fn._tool_name for fn in tool_fns}

        assert "memory_search" not in tool_names
        assert "memory_get" not in tool_names
        assert "memory_write" not in tool_names


class TestListRunningTasks:
    async def test_no_running_tasks(self) -> None:
        callback = MagicMock(return_value=[])
        tools, _, _, _ = _build_tools_and_deps(list_running_tasks_callback=callback)
        result = await tools["list_running_tasks"]({})
        assert result["content"][0]["text"] == "No running tasks."
        callback.assert_called_once()

    async def test_with_running_tasks(self) -> None:
        tasks = [
            {"task_key": "QR-1", "status": "running"},
            {"task_key": "QR-2", "status": "in_review", "pr_url": "https://github.com/org/repo/pull/1"},
        ]
        callback = MagicMock(return_value=tasks)
        tools, _, _, _ = _build_tools_and_deps(list_running_tasks_callback=callback)
        result = await tools["list_running_tasks"]({})
        import json

        parsed = json.loads(result["content"][0]["text"])
        assert len(parsed) == 2
        assert parsed[0]["task_key"] == "QR-1"
        assert parsed[1]["status"] == "in_review"

    async def test_not_registered_without_callback(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        assert "list_running_tasks" not in tools


class TestSendMessageToTask:
    async def test_send_success(self) -> None:
        callback = AsyncMock()
        tools, _, _, _ = _build_tools_and_deps(send_message_callback=callback)
        result = await tools["send_message_to_task"]({"task_key": "QR-5", "message": "focus on tests"})
        assert "Message sent to QR-5" in result["content"][0]["text"]
        callback.assert_awaited_once_with("QR-5", "focus on tests")

    async def test_send_error(self) -> None:
        callback = AsyncMock(side_effect=ValueError("No running session for QR-99"))
        tools, _, _, _ = _build_tools_and_deps(send_message_callback=callback)
        result = await tools["send_message_to_task"]({"task_key": "QR-99", "message": "hi"})
        assert "Error" in result["content"][0]["text"]

    async def test_not_registered_without_callback(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        assert "send_message_to_task" not in tools


class TestAbortTask:
    async def test_abort_success(self) -> None:
        callback = AsyncMock()
        tools, _, _, _ = _build_tools_and_deps(abort_task_callback=callback)
        result = await tools["abort_task"]({"task_key": "QR-10"})
        assert "QR-10 aborted" in result["content"][0]["text"]
        callback.assert_awaited_once_with("QR-10")

    async def test_abort_error(self) -> None:
        callback = AsyncMock(side_effect=ValueError("No running session for QR-10"))
        tools, _, _, _ = _build_tools_and_deps(abort_task_callback=callback)
        result = await tools["abort_task"]({"task_key": "QR-10"})
        assert "Error aborting task" in result["content"][0]["text"]

    async def test_not_registered_without_callback(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        assert "abort_task" not in tools


class TestCancelTask:
    async def test_cancel_success(self) -> None:
        callback = AsyncMock()
        tools, _, _, _ = _build_tools_and_deps(cancel_task_callback=callback)
        result = await tools["cancel_task"]({"task_key": "QR-7", "reason": "no longer needed"})
        assert "QR-7 cancelled" in result["content"][0]["text"]
        callback.assert_awaited_once_with("QR-7", "no longer needed")

    async def test_cancel_default_reason(self) -> None:
        callback = AsyncMock()
        tools, _, _, _ = _build_tools_and_deps(cancel_task_callback=callback)
        result = await tools["cancel_task"]({"task_key": "QR-7"})
        assert "QR-7 cancelled" in result["content"][0]["text"]
        callback.assert_awaited_once_with("QR-7", "Cancelled by supervisor")

    async def test_cancel_error(self) -> None:
        callback = AsyncMock(side_effect=RuntimeError("Tracker unavailable"))
        tools, _, _, _ = _build_tools_and_deps(cancel_task_callback=callback)
        result = await tools["cancel_task"]({"task_key": "QR-7", "reason": "test"})
        assert "Error cancelling task" in result["content"][0]["text"]

    async def test_not_registered_without_callback(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        assert "cancel_task" not in tools


class TestAgentMgmtToolRegistration:
    def test_all_agent_mgmt_tools_registered(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(
            list_running_tasks_callback=MagicMock(return_value=[]),
            send_message_callback=AsyncMock(),
            abort_task_callback=AsyncMock(),
            cancel_task_callback=AsyncMock(),
        )
        assert "list_running_tasks" in tools
        assert "send_message_to_task" in tools
        assert "abort_task" in tools
        assert "cancel_task" in tools

    def test_partial_registration(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(
            list_running_tasks_callback=MagicMock(return_value=[]),
            abort_task_callback=AsyncMock(),
        )
        assert "list_running_tasks" in tools
        assert "abort_task" in tools
        assert "send_message_to_task" not in tools
        assert "cancel_task" not in tools

    def test_allowed_tools_includes_agent_mgmt(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_agent_mgmt=True)
        assert "mcp__supervisor__list_running_tasks" in tools
        assert "mcp__supervisor__send_message_to_task" in tools
        assert "mcp__supervisor__abort_task" in tools
        assert "mcp__supervisor__cancel_task" in tools

    def test_allowed_tools_excludes_agent_mgmt_by_default(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor")
        assert "mcp__supervisor__list_running_tasks" not in tools
        assert "mcp__supervisor__abort_task" not in tools

    def test_epic_tools_when_has_epics(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_epics=True)
        assert "mcp__supervisor__epic_list" in tools
        assert "mcp__supervisor__epic_get_children" in tools
        assert "mcp__supervisor__epic_set_plan" in tools
        assert "mcp__supervisor__epic_activate_child" in tools
        assert "mcp__supervisor__epic_reset_child" in tools

    def test_no_epic_tools_when_no_epics(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_epics=False)
        assert "mcp__supervisor__epic_list" not in tools
        assert "mcp__supervisor__epic_set_plan" not in tools
        assert "mcp__supervisor__epic_reset_child" not in tools


# ---------------------------------------------------------------------------
# Epic management tools tests
# ---------------------------------------------------------------------------


def _make_epic_coordinator(
    epics: dict | None = None,
) -> MagicMock:
    """Create a mock EpicCoordinator for tool tests."""
    coordinator = MagicMock()
    coordinator._tag_ready_children = AsyncMock()
    coordinator.activate_child = AsyncMock(return_value=True)

    if epics is None:
        coordinator.get_state.return_value = {}
        coordinator.get_epic_state.return_value = None
    else:
        coordinator.get_state.return_value = epics

    return coordinator


class TestEpicList:
    async def test_no_active_epics(self) -> None:
        coordinator = _make_epic_coordinator()
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)
        result = await tools["epic_list"]({})
        assert "No active epics" in result["content"][0]["text"]

    async def test_with_epics(self) -> None:
        epics = {
            "QR-50": {
                "epic_key": "QR-50",
                "epic_summary": "Big feature",
                "phase": "awaiting_plan",
                "created_at": 1000.0,
                "children": {
                    "QR-51": {
                        "key": "QR-51",
                        "summary": "A",
                        "status": "pending",
                        "depends_on": [],
                        "tracker_status": "open",
                        "tags": [],
                    },
                    "QR-52": {
                        "key": "QR-52",
                        "summary": "B",
                        "status": "completed",
                        "depends_on": [],
                        "tracker_status": "Done",
                        "tags": [],
                    },
                },
            }
        }
        coordinator = _make_epic_coordinator(epics)
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)
        result = await tools["epic_list"]({})
        text = result["content"][0]["text"]
        assert "QR-50" in text
        assert "Big feature" in text
        assert "awaiting_plan" in text


class TestEpicGetChildren:
    async def test_returns_children_details(self) -> None:
        from orchestrator.epic_coordinator import ChildStatus, ChildTask, EpicState

        coordinator = _make_epic_coordinator()
        coordinator.get_epic_state.return_value = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "Task A", ChildStatus.PENDING, [], "open", tags=[]),
                "QR-52": ChildTask("QR-52", "Task B", ChildStatus.COMPLETED, ["QR-51"], "Done", tags=["ai-task"]),
            },
        )
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)
        result = await tools["epic_get_children"]({"epic_key": "QR-50"})
        text = result["content"][0]["text"]
        assert "QR-51" in text
        assert "QR-52" in text
        assert "Task A" in text
        assert "pending" in text

    async def test_epic_not_found(self) -> None:
        coordinator = _make_epic_coordinator()
        coordinator.get_epic_state.return_value = None
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)
        result = await tools["epic_get_children"]({"epic_key": "QR-99"})
        text = result["content"][0]["text"]
        assert "not found" in text.lower()


class TestEpicSetPlan:
    async def test_sets_dependencies_and_activates(self) -> None:
        from orchestrator.epic_coordinator import ChildStatus, ChildTask, EpicState

        coordinator = _make_epic_coordinator()
        state = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "A", ChildStatus.PENDING, [], "open"),
                "QR-52": ChildTask("QR-52", "B", ChildStatus.PENDING, [], "open"),
            },
        )
        coordinator.get_epic_state.return_value = state
        coordinator.validate_acyclic.return_value = True
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)

        deps = '{"QR-52": ["QR-51"], "QR-51": []}'
        result = await tools["epic_set_plan"]({"epic_key": "QR-50", "dependencies": deps})
        text = result["content"][0]["text"]
        assert "configured" in text.lower() or "set" in text.lower() or "plan" in text.lower()
        coordinator.set_child_dependencies.assert_called_once()

    async def test_rejects_cyclic_deps(self) -> None:
        from orchestrator.epic_coordinator import ChildStatus, ChildTask, EpicState

        coordinator = _make_epic_coordinator()
        state = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "A", ChildStatus.PENDING, [], "open"),
                "QR-52": ChildTask("QR-52", "B", ChildStatus.PENDING, [], "open"),
            },
        )
        coordinator.get_epic_state.return_value = state
        coordinator.validate_acyclic.return_value = False
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)

        deps = '{"QR-51": ["QR-52"], "QR-52": ["QR-51"]}'
        result = await tools["epic_set_plan"]({"epic_key": "QR-50", "dependencies": deps})
        text = result["content"][0]["text"]
        assert "cycl" in text.lower()
        coordinator.set_child_dependencies.assert_not_called()

    async def test_rejects_invalid_json(self) -> None:
        from orchestrator.epic_coordinator import EpicState

        coordinator = _make_epic_coordinator()
        coordinator.get_epic_state.return_value = EpicState(
            epic_key="QR-50", epic_summary="Epic", phase="awaiting_plan", children={}
        )
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)

        result = await tools["epic_set_plan"]({"epic_key": "QR-50", "dependencies": "not json"})
        text = result["content"][0]["text"]
        assert "invalid" in text.lower() or "error" in text.lower() or "json" in text.lower()

    async def test_rejects_unknown_children(self) -> None:
        from orchestrator.epic_coordinator import ChildStatus, ChildTask, EpicState

        coordinator = _make_epic_coordinator()
        state = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            phase="awaiting_plan",
            children={
                "QR-51": ChildTask("QR-51", "A", ChildStatus.PENDING, [], "open"),
            },
        )
        coordinator.get_epic_state.return_value = state
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)

        deps = '{"QR-99": ["QR-51"]}'
        result = await tools["epic_set_plan"]({"epic_key": "QR-50", "dependencies": deps})
        text = result["content"][0]["text"]
        assert "unknown" in text.lower()

    async def test_epic_not_found(self) -> None:
        coordinator = _make_epic_coordinator()
        coordinator.get_epic_state.return_value = None
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)

        result = await tools["epic_set_plan"]({"epic_key": "QR-99", "dependencies": "{}"})
        text = result["content"][0]["text"]
        assert "not found" in text.lower()


class TestEpicActivateChild:
    async def test_activates_child(self) -> None:
        coordinator = _make_epic_coordinator()
        coordinator.activate_child = AsyncMock(return_value=True)
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)

        result = await tools["epic_activate_child"]({"epic_key": "QR-50", "child_key": "QR-51"})
        text = result["content"][0]["text"]
        assert "activated" in text.lower() or "QR-51" in text
        coordinator.activate_child.assert_awaited_once_with("QR-50", "QR-51")

    async def test_cannot_activate_completed(self) -> None:
        coordinator = _make_epic_coordinator()
        coordinator.activate_child = AsyncMock(return_value=False)
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)

        result = await tools["epic_activate_child"]({"epic_key": "QR-50", "child_key": "QR-51"})
        text = result["content"][0]["text"]
        assert "failed" in text.lower() or "could not" in text.lower() or "cannot" in text.lower()


class TestEpicToolRegistration:
    def test_epic_tools_registered_when_coordinator_provided(self) -> None:
        coordinator = _make_epic_coordinator()
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=coordinator)
        assert "epic_list" in tools
        assert "epic_get_children" in tools
        assert "epic_set_plan" in tools
        assert "epic_activate_child" in tools
        assert "epic_reset_child" in tools

    def test_no_epic_tools_when_coordinator_none(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(epic_coordinator=None)
        assert "epic_list" not in tools
        assert "epic_get_children" not in tools
        assert "epic_set_plan" not in tools
        assert "epic_activate_child" not in tools
        assert "epic_reset_child" not in tools


# ---------------------------------------------------------------------------
# Mailbox (view_agent_messages) tool tests
# ---------------------------------------------------------------------------


def _make_mailbox() -> MagicMock:
    """Create a mock mailbox for supervisor tool tests."""
    return MagicMock()


class TestViewAgentMessages:
    """Test the view_agent_messages supervisor tool."""

    async def test_returns_messages(self) -> None:
        mailbox = _make_mailbox()
        msg_mock = MagicMock()
        msg_mock.status = "replied"
        msg_mock.sender_task_key = "QR-1"
        msg_mock.target_task_key = "QR-2"
        msg_mock.text = "What endpoint?"
        msg_mock.reply_text = "POST /api/v1/auth"
        mailbox.get_all_messages.return_value = [msg_mock]

        tools, _, _, _ = _build_tools_and_deps(mailbox=mailbox)
        result = await tools["view_agent_messages"]({"task_key": "QR-1"})

        text = result["content"][0]["text"]
        assert "1 message" in text
        assert "QR-1" in text
        assert "QR-2" in text
        assert "What endpoint?" in text
        assert "POST /api/v1/auth" in text

    async def test_no_messages(self) -> None:
        mailbox = _make_mailbox()
        mailbox.get_all_messages.return_value = []

        tools, _, _, _ = _build_tools_and_deps(mailbox=mailbox)
        result = await tools["view_agent_messages"]({"task_key": ""})

        text = result["content"][0]["text"]
        assert "No inter-agent messages" in text

    async def test_empty_task_key_passes_none(self) -> None:
        mailbox = _make_mailbox()
        mailbox.get_all_messages.return_value = []

        tools, _, _, _ = _build_tools_and_deps(mailbox=mailbox)
        await tools["view_agent_messages"]({"task_key": ""})

        mailbox.get_all_messages.assert_called_once_with(task_key=None)


class TestMailboxToolRegistration:
    """Test view_agent_messages tool registration."""

    def test_registered_when_mailbox_provided(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(mailbox=_make_mailbox())
        assert "view_agent_messages" in tools

    def test_not_registered_when_mailbox_none(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(mailbox=None)
        assert "view_agent_messages" not in tools


# ---------------------------------------------------------------------------
# K8s tools tests
# ---------------------------------------------------------------------------


def _make_k8s_client(
    pods: list | None = None,
    logs: str = "log line 1\nlog line 2",
    pod_detail: object | None = None,
) -> MagicMock:
    """Create a mock K8sClient for supervisor tool tests."""
    from orchestrator.k8s_client import ContainerInfo, PodDetail, PodInfo

    k8s = MagicMock()
    k8s.available = True
    k8s.namespace = "dev"

    if pods is None:
        pods = [
            PodInfo(
                name="coder-abc",
                namespace="dev",
                phase="Running",
                containers=[ContainerInfo("app", "coder:latest", True, 0, "running")],
            ),
        ]
    k8s.list_pods.return_value = pods
    k8s.get_pod_logs.return_value = logs

    if pod_detail is None:
        pod_detail = PodDetail(
            name="coder-abc",
            namespace="dev",
            phase="Running",
            containers=[ContainerInfo("app", "coder:latest", True, 0, "running")],
            conditions=[{"type": "Ready", "status": "True", "reason": "", "message": ""}],
            labels={"app": "coder"},
            node_name="node-1",
            start_time="2026-01-01T00:00:00Z",
        )
    k8s.get_pod_status.return_value = pod_detail

    return k8s


class TestK8sListPods:
    async def test_returns_pod_list(self) -> None:
        k8s = _make_k8s_client()
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        result = await tools["k8s_list_pods"]({})
        text = result["content"][0]["text"]
        assert "coder-abc" in text
        assert "Running" in text

    async def test_custom_namespace(self) -> None:
        k8s = _make_k8s_client()
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        await tools["k8s_list_pods"]({"namespace": "staging"})
        k8s.list_pods.assert_called_once_with("staging")

    async def test_empty_pods(self) -> None:
        k8s = _make_k8s_client(pods=[])
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        result = await tools["k8s_list_pods"]({})
        assert "No pods found" in result["content"][0]["text"]

    async def test_shows_container_state_and_restarts(self) -> None:
        from orchestrator.k8s_client import ContainerInfo, PodInfo

        pods = [
            PodInfo(
                name="crash-pod",
                namespace="dev",
                phase="Running",
                containers=[
                    ContainerInfo("web", "nginx:1.25", False, 5, "waiting", "CrashLoopBackOff"),
                ],
            ),
        ]
        k8s = _make_k8s_client(pods=pods)
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        result = await tools["k8s_list_pods"]({})
        text = result["content"][0]["text"]
        assert "crash-pod" in text
        assert "CrashLoopBackOff" in text
        assert "restarts=5" in text


class TestK8sGetPodLogs:
    async def test_returns_logs(self) -> None:
        k8s = _make_k8s_client(logs="line1\nline2\nline3")
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        result = await tools["k8s_get_pod_logs"]({"pod_name": "my-pod"})
        text = result["content"][0]["text"]
        assert "line1" in text
        assert "line3" in text

    async def test_passes_all_params(self) -> None:
        k8s = _make_k8s_client()
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        await tools["k8s_get_pod_logs"](
            {
                "pod_name": "my-pod",
                "container": "sidecar",
                "tail_lines": 50,
                "since_seconds": 300,
                "timestamps": True,
                "previous": True,
                "namespace": "prod",
            }
        )
        k8s.get_pod_logs.assert_called_once_with(
            pod_name="my-pod",
            container="sidecar",
            tail_lines=50,
            since_seconds=300,
            timestamps=True,
            previous=True,
            namespace="prod",
        )

    async def test_empty_logs(self) -> None:
        k8s = _make_k8s_client(logs="")
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        result = await tools["k8s_get_pod_logs"]({"pod_name": "my-pod"})
        assert "(empty logs)" in result["content"][0]["text"]

    async def test_truncates_long_logs(self) -> None:
        long_logs = "x" * 60_000
        k8s = _make_k8s_client(logs=long_logs)
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        result = await tools["k8s_get_pod_logs"]({"pod_name": "my-pod"})
        text = result["content"][0]["text"]
        assert "truncated" in text
        assert len(text) < 60_000


class TestK8sGetPodStatus:
    async def test_returns_detail(self) -> None:
        k8s = _make_k8s_client()
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        result = await tools["k8s_get_pod_status"]({"pod_name": "coder-abc"})
        text = result["content"][0]["text"]
        assert "coder-abc" in text
        assert "Running" in text
        assert "node-1" in text
        assert "Ready" in text

    async def test_not_found(self) -> None:
        k8s = _make_k8s_client()
        k8s.get_pod_status.return_value = None
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        result = await tools["k8s_get_pod_status"]({"pod_name": "missing"})
        text = result["content"][0]["text"]
        assert "not found" in text or "error" in text.lower()

    async def test_custom_namespace(self) -> None:
        k8s = _make_k8s_client()
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        await tools["k8s_get_pod_status"]({"pod_name": "my-pod", "namespace": "prod"})
        k8s.get_pod_status.assert_called_once_with(pod_name="my-pod", namespace="prod")


class TestK8sToolRegistration:
    def test_k8s_tools_registered_when_client_provided(self) -> None:
        k8s = _make_k8s_client()
        tools, _, _, _ = _build_tools_and_deps(k8s_client=k8s)
        assert "k8s_list_pods" in tools
        assert "k8s_get_pod_logs" in tools
        assert "k8s_get_pod_status" in tools

    def test_no_k8s_tools_when_client_none(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(k8s_client=None)
        assert "k8s_list_pods" not in tools
        assert "k8s_get_pod_logs" not in tools
        assert "k8s_get_pod_status" not in tools

    def test_allowed_tools_includes_k8s(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_k8s=True)
        assert "mcp__supervisor__k8s_list_pods" in tools
        assert "mcp__supervisor__k8s_get_pod_logs" in tools
        assert "mcp__supervisor__k8s_get_pod_status" in tools

    def test_allowed_tools_excludes_k8s_by_default(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor")
        assert "mcp__supervisor__k8s_list_pods" not in tools
        assert "mcp__supervisor__k8s_get_pod_logs" not in tools
        assert "mcp__supervisor__k8s_get_pod_status" not in tools


# ---------------------------------------------------------------------------
# Dependency management tools tests
# ---------------------------------------------------------------------------


def _make_dependency_manager(
    deferred: dict | None = None,
) -> MagicMock:
    """Create a mock DependencyManager for tool tests."""
    manager = MagicMock()
    if deferred is None:
        manager.get_deferred.return_value = {}
    else:
        manager.get_deferred.return_value = deferred
    manager.approve_dispatch = AsyncMock(return_value=True)
    manager.defer_task = AsyncMock(return_value=True)
    return manager


class TestListDeferredTasks:
    async def test_empty(self) -> None:
        manager = _make_dependency_manager()
        tools, _, _, _ = _build_tools_and_deps(dependency_manager=manager)
        result = await tools["list_deferred_tasks"]({})
        assert "No deferred tasks" in result["content"][0]["text"]

    async def test_with_entries(self) -> None:
        from orchestrator.dependency_manager import DeferredTask

        deferred = {
            "QR-204": DeferredTask(
                issue_key="QR-204",
                issue_summary="Implement feature",
                blockers=["QR-203"],
                deferred_at=1000.0,
            ),
        }
        manager = _make_dependency_manager(deferred)
        tools, _, _, _ = _build_tools_and_deps(dependency_manager=manager)
        result = await tools["list_deferred_tasks"]({})
        text = result["content"][0]["text"]
        assert "QR-204" in text
        assert "QR-203" in text
        assert "Implement feature" in text


class TestApproveTaskDispatch:
    async def test_success(self) -> None:
        manager = _make_dependency_manager()
        tools, _, _, _ = _build_tools_and_deps(dependency_manager=manager)
        result = await tools["approve_task_dispatch"]({"task_key": "QR-204"})
        text = result["content"][0]["text"]
        assert "approved" in text.lower()
        manager.approve_dispatch.assert_called_once_with("QR-204")

    async def test_not_deferred(self) -> None:
        manager = _make_dependency_manager()
        manager.approve_dispatch.return_value = False
        tools, _, _, _ = _build_tools_and_deps(dependency_manager=manager)
        result = await tools["approve_task_dispatch"]({"task_key": "QR-999"})
        text = result["content"][0]["text"]
        assert "not in the deferred set" in text


class TestDeferTaskTool:
    async def test_success(self) -> None:
        manager = _make_dependency_manager()
        tools, _, _, _ = _build_tools_and_deps(dependency_manager=manager)
        result = await tools["defer_task"](
            {"task_key": "QR-205", "summary": "API design", "reason": "Waiting for spec"}
        )
        text = result["content"][0]["text"]
        assert "deferred" in text.lower()
        manager.defer_task.assert_called_once_with("QR-205", "API design", "Waiting for spec")

    async def test_already_deferred(self) -> None:
        manager = _make_dependency_manager()
        manager.defer_task.return_value = False
        tools, _, _, _ = _build_tools_and_deps(dependency_manager=manager)
        result = await tools["defer_task"]({"task_key": "QR-205", "summary": "API design", "reason": "reason"})
        text = result["content"][0]["text"]
        assert "already deferred" in text


class TestDependencyToolRegistration:
    def test_registered_when_dependency_manager_provided(self) -> None:
        manager = _make_dependency_manager()
        tools, _, _, _ = _build_tools_and_deps(dependency_manager=manager)
        assert "list_deferred_tasks" in tools
        assert "approve_task_dispatch" in tools
        assert "defer_task" in tools

    def test_not_registered_when_none(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(dependency_manager=None)
        assert "list_deferred_tasks" not in tools
        assert "approve_task_dispatch" not in tools
        assert "defer_task" not in tools

    def test_allowed_tools_includes_dependency_tools(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor", has_dependencies=True)
        assert "mcp__supervisor__list_deferred_tasks" in tools
        assert "mcp__supervisor__approve_task_dispatch" in tools
        assert "mcp__supervisor__defer_task" in tools

    def test_allowed_tools_excludes_dependency_tools_by_default(self) -> None:
        from orchestrator.supervisor_tools import build_supervisor_allowed_tools

        tools = build_supervisor_allowed_tools("supervisor")
        assert "mcp__supervisor__list_deferred_tasks" not in tools
        assert "mcp__supervisor__approve_task_dispatch" not in tools
        assert "mcp__supervisor__defer_task" not in tools


# ---------------------------------------------------------------------------
# Diagnostic tools tests
# ---------------------------------------------------------------------------


def _make_state(
    dispatched: list[str] | None = None,
    running_sessions: list[dict[str, object]] | None = None,
    tracked_prs: dict | None = None,
    tracked_needs_info: dict | None = None,
    on_demand_sessions: list[str] | None = None,
    epics: dict | None = None,
    deferred_tasks: dict | None = None,
) -> dict[str, object]:
    """Build a mock orchestrator state dict."""
    return {
        "dispatched": dispatched or [],
        "active_tasks": [],
        "tracked_prs": tracked_prs or {},
        "tracked_needs_info": tracked_needs_info or {},
        "proposals": {},
        "supervisor": None,
        "running_sessions": running_sessions or [],
        "on_demand_sessions": on_demand_sessions or [],
        "epics": epics or {},
        "deferred_tasks": deferred_tasks or {},
        "supervisor_chat": None,
        "config": {
            "queue": "QR",
            "tag": "ai-task",
            "max_agents": 4,
        },
    }


class TestOrchestratorGetState:
    async def test_returns_formatted_state(self) -> None:
        state = _make_state(dispatched=["QR-1", "QR-2"])
        callback = MagicMock(return_value=state)
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=callback,
            get_task_events_callback=MagicMock(return_value=[]),
        )
        result = await tools["orchestrator_get_state"]({})
        text = result["content"][0]["text"]
        assert "QR-1" in text
        assert "QR-2" in text
        callback.assert_called_once()

    async def test_not_registered_without_callback(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        assert "orchestrator_get_state" not in tools


class TestOrchestratorGetTaskEvents:
    async def test_returns_events(self) -> None:
        events: list[dict[str, object]] = [
            {
                "type": "task_started",
                "task_key": "QR-10",
                "timestamp": 1000.0,
                "data": {"summary": "Test"},
            },
            {
                "type": "task_completed",
                "task_key": "QR-10",
                "timestamp": 1100.0,
                "data": {},
            },
        ]
        callback = MagicMock(return_value=events)
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value={}),
            get_task_events_callback=callback,
        )
        result = await tools["orchestrator_get_task_events"]({"task_key": "QR-10"})
        text = result["content"][0]["text"]
        assert "task_started" in text
        assert "task_completed" in text
        callback.assert_called_once_with("QR-10")

    async def test_no_events(self) -> None:
        callback = MagicMock(return_value=[])
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value={}),
            get_task_events_callback=callback,
        )
        result = await tools["orchestrator_get_task_events"]({"task_key": "QR-99"})
        text = result["content"][0]["text"]
        assert "No events found" in text

    async def test_not_registered_without_callback(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        assert "orchestrator_get_task_events" not in tools


class TestOrchestratorDiagnoseTask:
    async def test_detects_orphaned_epic_child(self) -> None:
        """Epic child DISPATCHED but no session and not in dispatched set."""
        state = _make_state(
            epics={
                "QR-200": {
                    "epic_summary": "Big epic",
                    "phase": "executing",
                    "children": {
                        "QR-211": {
                            "status": "dispatched",
                            "depends_on": [],
                        },
                    },
                },
            },
        )
        events: list[dict[str, object]] = [
            {
                "type": "task_started",
                "task_key": "QR-211",
                "timestamp": 1000.0,
                "data": {},
            },
        ]
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value=state),
            get_task_events_callback=MagicMock(return_value=events),
        )
        result = await tools["orchestrator_diagnose_task"]({"task_key": "QR-211"})
        text = result["content"][0]["text"]
        assert "STUCK" in text
        assert "Epic child is DISPATCHED" in text
        assert "QR-200" in text

    async def test_detects_stale_dispatched(self) -> None:
        """Task in dispatched set but no active session."""
        state = _make_state(dispatched=["QR-50"])
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value=state),
            get_task_events_callback=MagicMock(return_value=[]),
        )
        result = await tools["orchestrator_diagnose_task"]({"task_key": "QR-50"})
        text = result["content"][0]["text"]
        assert "STUCK" in text
        assert "dispatched set" in text

    async def test_detects_started_but_no_terminal(self) -> None:
        """Task has task_started but no terminal event."""
        state = _make_state()
        events: list[dict[str, object]] = [
            {
                "type": "task_started",
                "task_key": "QR-30",
                "timestamp": 1000.0,
                "data": {},
            },
        ]
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value=state),
            get_task_events_callback=MagicMock(return_value=events),
        )
        result = await tools["orchestrator_diagnose_task"]({"task_key": "QR-30"})
        text = result["content"][0]["text"]
        assert "STUCK" in text
        assert "task_started" in text
        assert "no terminal event" in text

    async def test_no_stuck_patterns(self) -> None:
        """Task with terminal event — no issues detected."""
        state = _make_state()
        events: list[dict[str, object]] = [
            {
                "type": "task_started",
                "task_key": "QR-40",
                "timestamp": 1000.0,
                "data": {},
            },
            {
                "type": "task_completed",
                "task_key": "QR-40",
                "timestamp": 1100.0,
                "data": {},
            },
        ]
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value=state),
            get_task_events_callback=MagicMock(return_value=events),
        )
        result = await tools["orchestrator_diagnose_task"]({"task_key": "QR-40"})
        text = result["content"][0]["text"]
        assert "No stuck patterns detected" in text

    async def test_shows_pr_tracking(self) -> None:
        """Task with tracked PR is shown in diagnostics."""
        state = _make_state(
            tracked_prs={
                "QR-60": {
                    "pr_url": "https://github.com/org/repo/pull/42",
                    "issue_key": "QR-60",
                    "last_check": 1000.0,
                },
            },
        )
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value=state),
            get_task_events_callback=MagicMock(return_value=[]),
        )
        result = await tools["orchestrator_diagnose_task"]({"task_key": "QR-60"})
        text = result["content"][0]["text"]
        assert "PR tracked: YES" in text
        assert "github.com" in text

    async def test_shows_running_session(self) -> None:
        """Task with active running session is detected."""
        state = _make_state(
            dispatched=["QR-80"],
            running_sessions=[
                {"task_key": "QR-80", "issue_key": "QR-80"},
            ],
        )
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value=state),
            get_task_events_callback=MagicMock(return_value=[]),
        )
        result = await tools["orchestrator_diagnose_task"]({"task_key": "QR-80"})
        text = result["content"][0]["text"]
        assert "Active running session: YES" in text
        # Should NOT be stuck — has session
        assert "STUCK" not in text or "dispatched set" not in text

    async def test_shows_needs_info_tracking(self) -> None:
        """Task in needs-info tracking is shown."""
        state = _make_state(
            tracked_needs_info={
                "QR-90": {
                    "issue_key": "QR-90",
                    "last_check": 2000.0,
                },
            },
        )
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value=state),
            get_task_events_callback=MagicMock(return_value=[]),
        )
        result = await tools["orchestrator_diagnose_task"]({"task_key": "QR-90"})
        text = result["content"][0]["text"]
        assert "Needs-info tracked: YES" in text

    async def test_detects_deferred_task(self) -> None:
        """Deferred task is reported in diagnostics."""
        state = _make_state(
            deferred_tasks={
                "QR-70": {
                    "blockers": ["QR-69"],
                    "summary": "Blocked task",
                    "manual": False,
                },
            },
        )
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value=state),
            get_task_events_callback=MagicMock(return_value=[]),
        )
        result = await tools["orchestrator_diagnose_task"]({"task_key": "QR-70"})
        text = result["content"][0]["text"]
        assert "Deferred: YES" in text
        assert "QR-69" in text
        assert "BLOCKED" in text

    async def test_not_registered_without_both_callbacks(
        self,
    ) -> None:
        """All diagnostic tools require both callbacks."""
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value={}),
        )
        assert "orchestrator_get_state" not in tools
        assert "orchestrator_get_task_events" not in tools
        assert "orchestrator_diagnose_task" not in tools

        tools2, _, _, _ = _build_tools_and_deps(
            get_task_events_callback=MagicMock(return_value=[]),
        )
        assert "orchestrator_get_state" not in tools2
        assert "orchestrator_get_task_events" not in tools2
        assert "orchestrator_diagnose_task" not in tools2


class TestDiagnosticToolRegistration:
    def test_registered_when_callbacks_provided(self) -> None:
        tools, _, _, _ = _build_tools_and_deps(
            get_state_callback=MagicMock(return_value={}),
            get_task_events_callback=MagicMock(return_value=[]),
        )
        assert "orchestrator_get_state" in tools
        assert "orchestrator_get_task_events" in tools
        assert "orchestrator_diagnose_task" in tools

    def test_not_registered_without_callbacks(self) -> None:
        tools, _, _, _ = _build_tools_and_deps()
        assert "orchestrator_get_state" not in tools
        assert "orchestrator_get_task_events" not in tools
        assert "orchestrator_diagnose_task" not in tools

    def test_allowed_tools_includes_diagnostics(self) -> None:
        from orchestrator.supervisor_tools import (
            build_supervisor_allowed_tools,
        )

        tools = build_supervisor_allowed_tools("supervisor", has_diagnostics=True)
        assert "mcp__supervisor__orchestrator_get_state" in tools
        assert "mcp__supervisor__orchestrator_get_task_events" in tools
        assert "mcp__supervisor__orchestrator_diagnose_task" in tools

    def test_allowed_tools_excludes_diagnostics_by_default(self) -> None:
        from orchestrator.supervisor_tools import (
            build_supervisor_allowed_tools,
        )

        tools = build_supervisor_allowed_tools("supervisor")
        assert "mcp__supervisor__orchestrator_get_state" not in tools
        assert "mcp__supervisor__orchestrator_get_task_events" not in tools
        assert "mcp__supervisor__orchestrator_diagnose_task" not in tools


# ===================================================================
# Bug #6: epic_create_child handles register_child failure
# ===================================================================


class TestEpicCreateChild:
    async def test_epic_create_child_success(self) -> None:
        """epic_create_child creates task and registers child."""
        coordinator = _make_epic_coordinator()
        coordinator.register_child = MagicMock(return_value=True)

        from orchestrator.epic_coordinator import EpicState

        coordinator.get_epic_state.return_value = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            children={},
        )
        on_task_created = MagicMock()
        tools, tracker, _, _ = _build_tools_and_deps(
            epic_coordinator=coordinator,
            on_task_created=on_task_created,
        )

        tracker.create_issue.return_value = {"key": "QR-99"}

        result = await tools["epic_create_child"](
            {
                "epic_key": "QR-50",
                "summary": "New subtask",
                "description": "Do something",
                "component": "Бекенд",
                "assignee": "john.doe",
            }
        )
        text = result["content"][0]["text"]
        assert "QR-99" in text
        assert "Created child" in text
        on_task_created.assert_called_once_with("QR-99")

    async def test_epic_create_child_error_when_register_fails(
        self,
    ) -> None:
        """epic_create_child returns error and skips on_task_created when register fails."""
        coordinator = _make_epic_coordinator()
        coordinator.register_child = MagicMock(return_value=False)

        from orchestrator.epic_coordinator import EpicState

        coordinator.get_epic_state.return_value = EpicState(
            epic_key="QR-50",
            epic_summary="Epic",
            children={},
        )
        on_task_created = MagicMock()
        tools, tracker, _, _ = _build_tools_and_deps(
            epic_coordinator=coordinator,
            on_task_created=on_task_created,
        )

        tracker.create_issue.return_value = {"key": "QR-99"}

        result = await tools["epic_create_child"](
            {
                "epic_key": "QR-50",
                "summary": "New subtask",
                "description": "Do something",
                "component": "Бекенд",
                "assignee": "john.doe",
            }
        )
        text = result["content"][0]["text"]
        assert "failed to register" in text.lower()
        on_task_created.assert_not_called()


class TestResolvePreflight:
    """Tests for resolve_preflight tool."""

    def _make_preflight_deps(
        self,
        pr_tracking_data: list | None = None,
    ) -> tuple[
        dict[str, Any],
        MagicMock,
        MagicMock,
        MagicMock,
        MagicMock,
    ]:
        """Build tools with preflight support.

        Returns (tools, dep_mgr, preflight, storage, event_bus).

        Args:
            pr_tracking_data: List of PRTrackingData to return from
                load_pr_tracking. Defaults to empty list (no tracked PRs).
        """
        dep_mgr = MagicMock()
        dep_mgr.approve_dispatch = AsyncMock()
        dep_mgr.remove_deferred = AsyncMock()

        preflight = MagicMock()
        preflight.approve_for_dispatch = MagicMock()

        storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(return_value=pr_tracking_data if pr_tracking_data is not None else [])

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        mark_dispatched = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )
        return tools, dep_mgr, preflight, storage, event_bus

    def _make_skip_deps(
        self,
        *,
        pr_tracking_data: list | None = None,
        github: object | None = None,
        storage: object | None = None,
    ) -> tuple[dict[str, Any], MagicMock]:
        """Build tools for skip decision tests with configurable setup.

        Returns (tools, dep_mgr).

        Args:
            pr_tracking_data: PR tracking data for load_pr_tracking.
            github: GitHub client (or None to create a fresh mock).
            storage: Storage mock (or None to create a fresh mock).
        """
        dep_mgr = MagicMock()
        dep_mgr.remove_deferred = AsyncMock()

        # Create fresh mocks if not provided (avoids mutable default arguments)
        if github is None:
            github = MagicMock()
        if storage is None:
            storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(return_value=pr_tracking_data if pr_tracking_data is not None else [])

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        preflight = MagicMock()
        mark_dispatched = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            github=github,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )
        return tools, dep_mgr

    async def test_skip_blocked_when_unmerged_pr_exists(
        self,
    ) -> None:
        """Bug QR-248/QR-266: skip must refuse when there's an
        unmerged PR for the task (updated to check GitHub API)."""
        from orchestrator.github_client import PRStatus
        from orchestrator.stats_models import PRTrackingData

        dep_mgr = MagicMock()
        dep_mgr.remove_deferred = AsyncMock()

        storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(
            return_value=[
                PRTrackingData(
                    task_key="QR-248",
                    pr_url="https://github.com/org/repo/pull/42",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
        )

        github = MagicMock()
        github.get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",  # PR is still open
                review_decision="",
                mergeable="CONFLICTING",  # Has conflicts
                head_sha="abc123",
            ),
        )

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        preflight = MagicMock()
        mark_dispatched = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            github=github,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-248", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        assert "cannot skip" in text.lower() or "conflict" in text.lower()
        # Must NOT mark as completed
        dep_mgr.remove_deferred.assert_not_called()

    async def test_skip_succeeds_when_no_unmerged_pr(
        self,
    ) -> None:
        """Skip with no unmerged PR proceeds normally.

        This test should exercise the happy path where load_pr_tracking
        returns an empty list, not the fail-open error recovery path.
        """
        tools, dep_mgr, _pf, storage, _event_bus = self._make_preflight_deps()

        result = await tools["resolve_preflight"](
            {"task_key": "QR-248", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        assert "confirmed as done" in text.lower() or "skipped" in text.lower()
        dep_mgr.remove_deferred.assert_called_once()
        # Verify load_pr_tracking was called (not relying on fail-open)
        storage.load_pr_tracking.assert_called_once_with(task_key="QR-248")

    async def test_skip_succeeds_when_no_storage(self) -> None:
        """Skip with no storage (can't check PR state)
        proceeds normally — fail-open."""
        dep_mgr = MagicMock()
        dep_mgr.approve_dispatch = AsyncMock()
        dep_mgr.remove_deferred = AsyncMock()

        preflight = MagicMock()
        preflight.approve_for_dispatch = MagicMock()

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        mark_dispatched = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=None,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-248", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        assert "skipped" in text.lower()

    async def test_dispatch_not_affected_by_unmerged_pr(
        self,
    ) -> None:
        """Dispatch decision should work regardless of PR state."""
        tools, dep_mgr, _preflight, _storage, _event_bus = self._make_preflight_deps()

        result = await tools["resolve_preflight"](
            {"task_key": "QR-248", "decision": "dispatch"},
        )

        text = result["content"][0]["text"]
        assert "approved" in text.lower()
        dep_mgr.approve_dispatch.assert_called_once()

    async def test_skip_graceful_on_storage_error(
        self,
    ) -> None:
        """Bug QR-248/QR-266: if load_pr_tracking fails, fail-open and allow skip
        (updated to use new GitHub API check with fail-open strategy)."""
        dep_mgr = MagicMock()
        dep_mgr.remove_deferred = AsyncMock()

        storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(
            side_effect=RuntimeError("DB error"),
        )

        github = MagicMock()

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        preflight = MagicMock()
        mark_dispatched = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            github=github,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-248", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        # Should allow skip on error (fail-open for unexpected errors)
        assert "skipped" in text.lower() or "confirmed" in text.lower()
        dep_mgr.remove_deferred.assert_called_once()

    async def test_skip_checks_github_api_when_pr_merged(
        self,
    ) -> None:
        """Bug QR-266: check actual GitHub PR status, allow skip when merged."""
        from orchestrator.github_client import PRStatus
        from orchestrator.stats_models import PRTrackingData

        dep_mgr = MagicMock()
        dep_mgr.remove_deferred = AsyncMock()

        storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(
            return_value=[
                PRTrackingData(
                    task_key="QR-266",
                    pr_url="https://github.com/org/repo/pull/123",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
        )
        storage.record_pr_merged = AsyncMock()

        github = MagicMock()
        github.get_pr_status = MagicMock(
            return_value=PRStatus(
                state="MERGED",
                review_decision="",
                mergeable="",
                head_sha="abc123",
            ),
        )

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        mark_dispatched = MagicMock()

        preflight = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            github=github,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-266", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        assert "skipped" in text.lower() or "confirmed" in text.lower()
        # Should have called GitHub API
        github.get_pr_status.assert_called_once_with("org", "repo", 123)
        # Should have updated cache
        storage.record_pr_merged.assert_called_once()
        # Should have proceeded with skip
        dep_mgr.remove_deferred.assert_called_once()

    async def test_skip_rejects_when_pr_open_conflicting(
        self,
    ) -> None:
        """Bug QR-266: reject skip when PR has merge conflicts."""
        from orchestrator.github_client import PRStatus
        from orchestrator.stats_models import PRTrackingData

        dep_mgr = MagicMock()
        dep_mgr.remove_deferred = AsyncMock()

        storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(
            return_value=[
                PRTrackingData(
                    task_key="QR-266",
                    pr_url="https://github.com/org/repo/pull/123",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
        )

        github = MagicMock()
        github.get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="CONFLICTING",
                head_sha="abc123",
            ),
        )

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        preflight = MagicMock()
        mark_dispatched = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            github=github,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-266", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        assert "cannot skip" in text.lower() or "conflict" in text.lower()
        # Should NOT have proceeded with skip
        dep_mgr.remove_deferred.assert_not_called()

    async def test_skip_allows_when_pr_open_mergeable(
        self,
    ) -> None:
        """Bug QR-266: allow skip (with warning) when PR is open but mergeable."""
        from orchestrator.github_client import PRStatus
        from orchestrator.stats_models import PRTrackingData

        dep_mgr = MagicMock()
        dep_mgr.remove_deferred = AsyncMock()

        storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(
            return_value=[
                PRTrackingData(
                    task_key="QR-266",
                    pr_url="https://github.com/org/repo/pull/123",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
        )

        github = MagicMock()
        github.get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="",
                mergeable="MERGEABLE",
                head_sha="abc123",
            ),
        )

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        mark_dispatched = MagicMock()

        preflight = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            github=github,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-266", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        assert "skipped" in text.lower() or "confirmed" in text.lower()
        # Should have proceeded with skip
        dep_mgr.remove_deferred.assert_called_once()

    @pytest.mark.parametrize(
        ("scenario", "github_setup", "expect_github_called"),
        [
            (
                "no_pr_tracked",
                "mock",  # GitHub client available
                False,  # Should NOT call GitHub API
            ),
            (
                "no_github_client",
                None,  # No GitHub client
                False,
            ),
            (
                "github_api_fails",
                "api_error",  # GitHub client raises exception
                True,  # GitHub API is called but fails
            ),
            (
                "pr_url_malformed",
                "mock",  # GitHub client available
                False,  # Malformed URL prevents GitHub call
            ),
        ],
    )
    async def test_skip_allows_fail_open_scenarios(
        self,
        scenario: str,
        github_setup: str | None,
        expect_github_called: bool,
    ) -> None:
        """Bug QR-266: skip succeeds (fail-open) in error scenarios.

        Parametrized test covering:
        - no_pr_tracked: no PR ever created
        - no_github_client: GitHub client not configured
        - github_api_fails: GitHub API unreachable
        - pr_url_malformed: invalid PR URL
        """
        import requests

        from orchestrator.stats_models import PRTrackingData

        # Scenario-specific PR data
        pr_data_map = {
            "no_pr_tracked": [],
            "no_github_client": [
                PRTrackingData(
                    task_key="QR-266",
                    pr_url="https://github.com/org/repo/pull/123",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
            "github_api_fails": [
                PRTrackingData(
                    task_key="QR-266",
                    pr_url="https://github.com/org/repo/pull/123",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
            "pr_url_malformed": [
                PRTrackingData(
                    task_key="QR-266",
                    pr_url="invalid-url",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
        }
        pr_data = pr_data_map[scenario]

        # Build GitHub client based on setup
        if github_setup is None:
            github = None
        elif github_setup == "api_error":
            github = MagicMock()
            github.get_pr_status = MagicMock(
                side_effect=requests.RequestException("API error"),
            )
        else:  # "mock"
            github = MagicMock()

        tools, dep_mgr = self._make_skip_deps(
            pr_tracking_data=pr_data,
            github=github,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-266", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        assert "skipped" in text.lower() or "confirmed" in text.lower()
        dep_mgr.remove_deferred.assert_called_once()

        # Verify GitHub API call expectations
        if github is not None and expect_github_called:
            github.get_pr_status.assert_called()
        elif github is not None and not expect_github_called:
            github.get_pr_status.assert_not_called()

    async def test_skip_rejects_when_any_pr_has_conflicts(
        self,
    ) -> None:
        """Bug QR-266 review: reject skip if ANY PR has conflicts (not just first)."""
        from orchestrator.stats_models import PRTrackingData

        dep_mgr = MagicMock()
        dep_mgr.remove_deferred = AsyncMock()

        storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(
            return_value=[
                PRTrackingData(
                    task_key="QR-266",
                    pr_url="https://github.com/owner/repo/pull/100",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
                PRTrackingData(
                    task_key="QR-266",
                    pr_url="https://github.com/owner/repo/pull/101",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
        )
        storage.record_pr_merged = AsyncMock()

        github = MagicMock()

        def mock_get_pr_status(owner: str, repo: str, pr_number: int):
            if pr_number == 100:
                # First PR is merged
                from orchestrator.github_client import PRStatus

                return PRStatus(state="MERGED", review_decision="", mergeable=None, head_sha="abc123")
            if pr_number == 101:
                # Second PR has conflicts
                from orchestrator.github_client import PRStatus

                return PRStatus(state="OPEN", review_decision="", mergeable="CONFLICTING", head_sha="def456")
            raise ValueError(f"Unexpected PR number: {pr_number}")

        github.get_pr_status = MagicMock(side_effect=mock_get_pr_status)

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        mark_dispatched = MagicMock()

        preflight = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            github=github,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-266", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        assert "cannot skip" in text.lower()
        assert "conflict" in text.lower()
        assert "pull/101" in text
        # Should NOT have removed from deferred (skip rejected)
        dep_mgr.remove_deferred.assert_not_called()

    async def test_skip_with_non_requests_exception_continues_checking_remaining_prs(
        self,
    ) -> None:
        """Non-requests exception on one PR should not stop checking other PRs.

        Bug: inner except only catches requests.RequestException. If get_pr_status
        raises a non-requests exception (e.g., KeyError from malformed GraphQL
        response), it bypasses the per-PR fail-open handler and hits the outer
        except Exception, which stops checking ALL remaining PRs. This means a
        transient parsing error on one PR can cause the loop to stop, potentially
        missing a CONFLICTING PR that would correctly reject the skip.
        """
        from orchestrator.stats_models import PRTrackingData

        dep_mgr = MagicMock()
        dep_mgr.approve_dispatch = AsyncMock()
        dep_mgr.remove_deferred = AsyncMock()

        storage = MagicMock()
        storage.has_unmerged_pr = AsyncMock(return_value=True)
        storage.load_pr_tracking = AsyncMock(
            return_value=[
                PRTrackingData(
                    task_key="QR-BUG",
                    pr_url="https://github.com/owner/repo/pull/100",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
                PRTrackingData(
                    task_key="QR-BUG",
                    pr_url="https://github.com/owner/repo/pull/101",
                    issue_summary="Test issue",
                    seen_thread_ids=[],
                    seen_failed_checks=[],
                ),
            ],
        )

        github = MagicMock()

        def mock_get_pr_status(owner: str, repo: str, pr_number: int):
            if pr_number == 100:
                # First PR raises non-requests exception (e.g., KeyError from malformed response)
                raise KeyError("missing field in GraphQL response")
            if pr_number == 101:
                # Second PR has conflicts - should reject skip
                from orchestrator.github_client import PRStatus

                return PRStatus(
                    state="OPEN",
                    review_decision="",
                    mergeable="CONFLICTING",
                    head_sha="def456",
                )
            raise ValueError(f"Unexpected PR number: {pr_number}")

        github.get_pr_status = MagicMock(side_effect=mock_get_pr_status)

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        mark_dispatched = MagicMock()

        preflight = MagicMock()

        tools, _, _, _ = _build_tools_and_deps(
            dependency_manager=dep_mgr,
            preflight_checker=preflight,
            storage=storage,
            github=github,
            event_bus=event_bus,
            mark_dispatched_callback=mark_dispatched,
        )

        result = await tools["resolve_preflight"](
            {"task_key": "QR-BUG", "decision": "skip"},
        )

        text = result["content"][0]["text"]
        # Should reject skip due to conflicting PR 101
        assert "cannot skip" in text.lower()
        assert "conflict" in text.lower()
        assert "pull/101" in text
        # Should NOT have removed from deferred (skip rejected)
        dep_mgr.remove_deferred.assert_not_called()


# ===================================================================
# requeue_task
# ===================================================================


class TestRequeueTask:
    """Tests for requeue_task tool."""

    def _make_requeue_deps(
        self,
        clear_recovery: object | None = None,
    ) -> tuple[dict[str, Any], MagicMock, MagicMock, MagicMock]:
        """Build tools with requeue_task support.

        Returns (tools, preflight, remove_dispatched, dep_mgr).
        """
        preflight = MagicMock()
        preflight.approve_for_dispatch = MagicMock()
        remove_dispatched = MagicMock()

        dep_mgr = MagicMock()
        dep_mgr.approve_dispatch = AsyncMock()
        dep_mgr.remove_deferred = AsyncMock()

        storage = MagicMock()
        storage.load_pr_tracking = AsyncMock(return_value=[])

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        tools, _, _, _ = _build_tools_and_deps(
            preflight_checker=preflight,
            dependency_manager=dep_mgr,
            remove_dispatched_callback=remove_dispatched,
            clear_recovery_callback=clear_recovery,
            storage=storage,
            event_bus=event_bus,
        )
        return tools, preflight, remove_dispatched, dep_mgr

    @pytest.mark.asyncio
    async def test_requeue_clears_recovery_state(self) -> None:
        """requeue_task must call clear_recovery_callback to reset retry counter."""
        clear_recovery = MagicMock()
        tools, preflight, remove_dispatched, _ = self._make_requeue_deps(
            clear_recovery=clear_recovery,
        )

        result = await tools["requeue_task"]({"task_key": "QR-273"})

        clear_recovery.assert_called_once_with("QR-273")
        remove_dispatched.assert_called_once_with("QR-273")
        preflight.approve_for_dispatch.assert_called_once_with("QR-273")
        assert "re-queued" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_requeue_without_recovery_callback(self) -> None:
        """requeue_task works when clear_recovery_callback is None."""
        tools, preflight, remove_dispatched, _ = self._make_requeue_deps()

        result = await tools["requeue_task"]({"task_key": "QR-100"})

        remove_dispatched.assert_called_once_with("QR-100")
        preflight.approve_for_dispatch.assert_called_once_with("QR-100")
        assert "re-queued" in result["content"][0]["text"]


# ===================================================================
# Structural: every @tool in supervisor_tools must be in allowed_tools
# ===================================================================


class TestAllRegisteredToolsAreAllowed:
    """Prevents the class of bug where a tool is registered in
    build_supervisor_server but missing from build_supervisor_allowed_tools.

    Parses all @tool("name", ...) decorators from the source file and
    verifies that build_supervisor_allowed_tools (with all flags on)
    covers every one.
    """

    def test_no_tool_left_behind(self) -> None:
        from orchestrator.supervisor_tools import (
            build_supervisor_allowed_tools,
        )

        src = Path("orchestrator/supervisor_tools.py").read_text()
        # Extract all tool names from @tool("name", ...) decorators
        registered = set(
            re.findall(
                r'@tool\(\s*"([^"]+)"',
                src,
            )
        )
        assert registered, "Failed to parse any @tool decorators"

        # Build allowed list with every feature flag enabled
        allowed = build_supervisor_allowed_tools(
            "supervisor",
            has_storage=True,
            has_github=True,
            has_memory=True,
            has_agent_mgmt=True,
            has_epics=True,
            has_mailbox=True,
            has_k8s=True,
            has_dependencies=True,
            has_preflight=True,
            has_diagnostics=True,
            has_heartbeat=True,
        )
        # Strip server prefix for comparison
        allowed_names = {t.replace("mcp__supervisor__", "") for t in allowed if t.startswith("mcp__supervisor__")}

        missing = registered - allowed_names
        assert not missing, (
            f"Tools registered in build_supervisor_server but missing from build_supervisor_allowed_tools: {missing}"
        )
