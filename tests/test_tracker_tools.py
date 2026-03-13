"""Tests for tracker_tools module."""

from unittest.mock import MagicMock

import requests


def _build_tools(
    mock_sdk,
    client=None,
    issue_key="QR-125",
    **kwargs,
) -> dict:
    """Build tracker server and return {tool_name: tool_callable}."""
    from orchestrator.tracker_tools import build_tracker_server

    if client is None:
        client = MagicMock()
    build_tracker_server(client, issue_key, **kwargs)
    tools = mock_sdk.create_sdk_mcp_server.call_args[1]["tools"]
    return {t._tool_name: t for t in tools}


class TestBuildTrackerServer:
    def test_returns_server(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import build_tracker_server

        client = MagicMock()
        build_tracker_server(client, "QR-125")

        mock_sdk.create_sdk_mcp_server.assert_called_once()
        call_kwargs = mock_sdk.create_sdk_mcp_server.call_args[1]
        assert call_kwargs["name"] == "tracker"
        assert len(call_kwargs["tools"]) == 13

    def test_tool_names(self, mock_sdk) -> None:
        tools = _build_tools(mock_sdk)
        assert set(tools.keys()) == {
            "tracker_get_issue",
            "tracker_add_comment",
            "tracker_get_comments",
            "tracker_get_checklist",
            "tracker_request_info",
            "tracker_signal_blocked",
            "tracker_create_subtask",
            "propose_improvement",
            "tracker_get_attachments",
            "tracker_download_attachment",
            "tracker_create_workpad",
            "tracker_update_workpad",
            "tracker_mark_complete",
        }


class TestGetIssueTool:
    async def test_returns_formatted_issue(self, mock_sdk) -> None:
        from orchestrator.tracker_client import TrackerIssue

        client = MagicMock()
        client.get_issue.return_value = TrackerIssue(
            key="QR-125",
            summary="Test",
            description="Description",
            components=["Бекенд"],
            tags=["ai-task"],
            status="open",
        )

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_get_issue"]({})
        assert "QR-125" in result["content"][0]["text"]
        assert "Test" in result["content"][0]["text"]
        client.get_issue.assert_called_once_with("QR-125")


class TestAddCommentTool:
    async def test_adds_comment(self, mock_sdk) -> None:
        client = MagicMock()
        tools = _build_tools(mock_sdk, client)

        result = await tools["tracker_add_comment"]({"text": "Done!"})
        assert "Comment added" in result["content"][0]["text"]
        client.add_comment.assert_called_once_with("QR-125", "Done!")


class TestGetCommentsTool:
    async def test_returns_formatted_comments(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_comments.return_value = [
            {
                "createdBy": {"display": "John"},
                "text": "Hello",
                "createdAt": "2025-01-01",
            },
        ]

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_get_comments"]({})
        assert "John" in result["content"][0]["text"]
        assert "Hello" in result["content"][0]["text"]


class TestRequestInfoTool:
    async def test_sets_tool_state(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        tool_state = ToolState()
        tools = _build_tools(mock_sdk, client, tool_state=tool_state)

        result = await tools["tracker_request_info"]({"text": "Need clarification on API design"})
        assert tool_state.needs_info_requested is True
        assert tool_state.needs_info_text == "Need clarification on API design"
        assert "Information request submitted" in result["content"][0]["text"]

    async def test_calls_transition_and_comment(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        tool_state = ToolState()
        tools = _build_tools(mock_sdk, client, tool_state=tool_state)

        await tools["tracker_request_info"]({"text": "Blocked on X"})
        client.add_comment.assert_called_once_with("QR-125", "Blocked on X")
        client.transition_to_needs_info.assert_called_once_with("QR-125")

    async def test_works_without_tool_state(self, mock_sdk) -> None:
        client = MagicMock()
        tools = _build_tools(mock_sdk, client)

        result = await tools["tracker_request_info"]({"text": "Blocked"})
        assert "Information request submitted" in result["content"][0]["text"]
        client.add_comment.assert_called_once()

    async def test_logs_transition_error_with_exc_info(self, mock_sdk, caplog) -> None:
        import logging

        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.transition_to_needs_info.side_effect = requests.ConnectionError("Transition API down")
        tool_state = ToolState()
        tools = _build_tools(mock_sdk, client, tool_state=tool_state)

        with caplog.at_level(
            logging.WARNING,
            logger="orchestrator.tracker_tools",
        ):
            await tools["tracker_request_info"]({"text": "Need info"})

        assert tool_state.needs_info_requested is True
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1
        assert "QR-125" in warning_records[0].message
        assert warning_records[0].exc_info is not None
        assert warning_records[0].exc_info[1] is not None

    def test_description_mentions_commit_and_push(self, mock_sdk) -> None:
        tools = _build_tools(mock_sdk)
        desc = tools["tracker_request_info"]._tool_desc.lower()
        assert "commit" in desc
        assert "push" in desc


class TestSignalBlockedTool:
    async def test_sets_tool_state_fields(self, mock_sdk) -> None:
        """Test that signal_blocked sets all required tool_state fields."""
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        tool_state = ToolState()
        tools = _build_tools(mock_sdk, client, tool_state=tool_state)

        result = await tools["tracker_signal_blocked"](
            {"blocking_agent": "QR-123", "reason": "Waiting for API contract"}
        )

        assert tool_state.blocked_by_agent == "QR-123"
        assert tool_state.blocking_reason == "Waiting for API contract"
        assert tool_state.needs_info_requested is True
        # CRITICAL: needs_info_text must be set for NEEDS_INFO event
        assert tool_state.needs_info_text == "Заблокировано агентом QR-123. Причина: Waiting for API contract"
        assert "blocked by QR-123" in result["content"][0]["text"]

    async def test_calls_add_comment_and_link(self, mock_sdk) -> None:
        """Test that signal_blocked creates comment and dependency link."""
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        tool_state = ToolState()
        tools = _build_tools(mock_sdk, client, tool_state=tool_state)

        await tools["tracker_signal_blocked"]({"blocking_agent": "QR-456", "reason": "Need endpoint implementation"})

        # Should add comment
        client.add_comment.assert_called_once()
        call_args = client.add_comment.call_args[0]
        assert call_args[0] == "QR-125"
        assert "QR-456" in call_args[1]
        assert "Need endpoint implementation" in call_args[1]

        # Should create dependency link
        client.add_link.assert_called_once_with("QR-125", "QR-456", "depends on")

        # Should transition to needs-info
        client.transition_to_needs_info.assert_called_once_with("QR-125")

    async def test_works_without_tool_state(self, mock_sdk) -> None:
        """Test that signal_blocked works when tool_state is None."""
        client = MagicMock()
        tools = _build_tools(mock_sdk, client)

        result = await tools["tracker_signal_blocked"]({"blocking_agent": "QR-789", "reason": "Testing"})

        assert "blocked by QR-789" in result["content"][0]["text"]
        client.add_comment.assert_called_once()
        client.add_link.assert_called_once()


class TestGetChecklistTool:
    async def test_returns_formatted_checklist(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_checklist.return_value = [
            {"text": "Write tests", "checked": True},
            {"text": "Implement", "checked": False},
        ]

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_get_checklist"]({})
        text = result["content"][0]["text"]
        assert "[x] Write tests" in text
        assert "[ ] Implement" in text


class TestCreateSubtaskTool:
    async def test_creates_subtask_with_ai_tag(self, mock_sdk) -> None:
        from orchestrator.config import Config
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.create_subtask.return_value = {"key": "QR-200"}
        tool_state = ToolState()
        config = Config(
            tracker_token="t",
            tracker_org_id="o",
            tracker_queue="QR",
            tracker_tag="ai-task",
            tracker_project_id=13,
            tracker_boards=[14],
        )

        tools = _build_tools(
            mock_sdk,
            client,
            tool_state=tool_state,
            config=config,
            issue_components=["Backend"],
        )
        result = await tools["tracker_create_subtask"](
            {
                "summary": "Follow-up",
                "description": "Remaining work",
            }
        )
        assert "QR-200" in result["content"][0]["text"]
        client.create_subtask.assert_called_once_with(
            parent_key="QR-125",
            queue="QR",
            summary="Follow-up",
            description="Remaining work",
            components=["Backend"],
            project_id=13,
            boards=[14],
            tags=["ai-task"],
        )
        assert tool_state.created_subtasks == ["QR-200"]

    async def test_surfaces_link_failed_warning(self, mock_sdk) -> None:
        """Test that link_failed flag from client is surfaced to agent."""
        from orchestrator.config import Config
        from orchestrator.tracker_tools import ToolState, build_tracker_server

        client = MagicMock()
        client.create_subtask.return_value = {"key": "QR-201", "link_failed": True}
        tool_state = ToolState()
        config = Config(
            tracker_token="t",
            tracker_org_id="o",
            tracker_queue="QR",
            tracker_tag="ai-task",
            tracker_project_id=13,
            tracker_boards=[14],
        )

        build_tracker_server(client, "QR-125", tool_state=tool_state, config=config, issue_components=["Backend"])
        tools = mock_sdk.create_sdk_mcp_server.call_args[1]["tools"]
        create_subtask = next(t for t in tools if t._tool_name == "tracker_create_subtask")

        result = await create_subtask({"summary": "Follow-up", "description": "Remaining work"})
        text = result["content"][0]["text"]
        assert "QR-201" in text
        assert "WARNING" in text
        assert "Failed to link as subtask to parent" in text
        assert tool_state.created_subtasks == ["QR-201"]


class TestGetAttachmentsTool:
    async def test_returns_formatted_attachments(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_attachments.return_value = [
            {
                "id": 71,
                "name": "sitemap.xml",
                "mimetype": "text/xml",
                "size": 1024,
            }
        ]

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_get_attachments"]({})
        text = result["content"][0]["text"]
        assert "sitemap.xml" in text
        assert "71" in text
        assert "text/xml" in text
        client.get_attachments.assert_called_once_with("QR-125")

    async def test_returns_no_attachments_message(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_attachments.return_value = []

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_get_attachments"]({})
        assert "No attachments" in result["content"][0]["text"]


class TestDownloadAttachmentTool:
    async def test_downloads_text_file(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_attachments.return_value = [{"id": "71", "name": "file.xml"}]
        client.download_attachment.return_value = (
            b"<xml>data</xml>",
            "text/xml",
        )

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_download_attachment"]({"attachment_id": 71})
        text = result["content"][0]["text"]
        assert "<xml>data</xml>" in text
        client.download_attachment.assert_called_once_with(71)

    async def test_mime_type_with_charset_parameter(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_attachments.return_value = [{"id": "71", "name": "file.json"}]
        client.download_attachment.return_value = (
            b'{"key": "value"}',
            "application/json; charset=utf-8",
        )

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_download_attachment"]({"attachment_id": 71})
        text = result["content"][0]["text"]
        assert '{"key": "value"}' in text
        assert "Binary file" not in text

    async def test_binary_file_returns_metadata_only(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_attachments.return_value = [{"id": "99", "name": "image.png"}]
        client.download_attachment.return_value = (
            b"\x89PNG\r\n\x1a\n",
            "image/png",
        )

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_download_attachment"]({"attachment_id": 99})
        text = result["content"][0]["text"]
        assert "Binary file" in text
        assert "image/png" in text
        assert "PNG" not in text

    async def test_size_limit_error(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_attachments.return_value = [{"id": "123", "name": "large.bin"}]
        client.download_attachment.side_effect = ValueError("File too large: 10485760 bytes (limit: 5242880)")

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_download_attachment"]({"attachment_id": 123})
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "too large" in text

    async def test_refuses_attachment_from_different_issue(self, mock_sdk) -> None:
        client = MagicMock()
        client.get_attachments.return_value = [
            {
                "id": "71",
                "name": "file1.xml",
                "mimetype": "text/xml",
                "size": 100,
            },
            {
                "id": "72",
                "name": "file2.json",
                "mimetype": "application/json",
                "size": 200,
            },
        ]
        client.download_attachment.return_value = (
            b"secret data",
            "text/plain",
        )

        tools = _build_tools(mock_sdk, client)
        result = await tools["tracker_download_attachment"]({"attachment_id": 999})
        text = result["content"][0]["text"]
        assert "Error" in text or "not found" in text.lower() or "not belong" in text.lower()
        client.download_attachment.assert_not_called()


class TestCreateWorkpadNew:
    async def test_creates_comment_with_marker(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.get_comments.return_value = []
        client.add_comment.return_value = {"id": 101}
        tool_state = ToolState()

        tools = _build_tools(mock_sdk, client, tool_state=tool_state)
        result = await tools["tracker_create_workpad"]({"text": "## Plan\n- Step 1"})

        text = result["content"][0]["text"]
        assert "created" in text.lower()
        assert tool_state.workpad_comment_id == 101

        call_text = client.add_comment.call_args[0][1]
        assert call_text.startswith("<!-- workpad:QR-125 -->")
        assert "## Plan" in call_text


class TestCreateWorkpadIdempotent:
    async def test_reuses_existing_workpad(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.get_comments.return_value = [
            {
                "id": 50,
                "text": "<!-- workpad:QR-125 -->\nold",
            }
        ]
        tool_state = ToolState()

        tools = _build_tools(mock_sdk, client, tool_state=tool_state)
        result = await tools["tracker_create_workpad"]({"text": "new content"})

        text = result["content"][0]["text"]
        assert "found" in text.lower()
        assert tool_state.workpad_comment_id == 50
        client.update_comment.assert_called_once()
        client.add_comment.assert_not_called()


class TestCreateWorkpadIdempotentMultiple:
    async def test_picks_newest_marker_comment(self, mock_sdk) -> None:
        """Multiple marker comments: picks newest (last via reversed)."""
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.get_comments.return_value = [
            {
                "id": 10,
                "text": "<!-- workpad:QR-125 -->\nfirst",
            },
            {
                "id": 20,
                "text": "<!-- workpad:QR-125 -->\nsecond",
            },
        ]
        tool_state = ToolState()

        tools = _build_tools(mock_sdk, client, tool_state=tool_state)
        await tools["tracker_create_workpad"]({"text": "update"})

        assert tool_state.workpad_comment_id == 20
        client.update_comment.assert_called_once_with(
            "QR-125",
            20,
            "<!-- workpad:QR-125 -->\nupdate",
        )


class TestCreateWorkpadIgnoresHumanComment:
    async def test_ignores_human_comment_without_marker(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.get_comments.return_value = [
            {"id": 30, "text": "## Workpad\nhuman notes"},
        ]
        client.add_comment.return_value = {"id": 102}
        tool_state = ToolState()

        tools = _build_tools(mock_sdk, client, tool_state=tool_state)
        await tools["tracker_create_workpad"]({"text": "agent workpad"})

        assert tool_state.workpad_comment_id == 102
        client.add_comment.assert_called_once()


class TestCreateWorkpadIgnoresOtherTaskMarker:
    async def test_ignores_marker_for_different_issue(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.get_comments.return_value = [
            {
                "id": 40,
                "text": "<!-- workpad:QR-999 -->\nother",
            },
        ]
        client.add_comment.return_value = {"id": 103}
        tool_state = ToolState()

        tools = _build_tools(mock_sdk, client, tool_state=tool_state)
        await tools["tracker_create_workpad"]({"text": "my workpad"})

        assert tool_state.workpad_comment_id == 103
        client.add_comment.assert_called_once()


class TestUpdateWorkpad:
    async def test_updates_via_stored_id(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        tool_state = ToolState()
        tool_state.workpad_comment_id = 55

        tools = _build_tools(mock_sdk, client, tool_state=tool_state)
        result = await tools["tracker_update_workpad"]({"text": "## Progress\n- Done"})

        text = result["content"][0]["text"]
        assert "updated" in text.lower()
        client.update_comment.assert_called_once_with(
            "QR-125",
            55,
            "<!-- workpad:QR-125 -->\n## Progress\n- Done",
        )


class TestUpdateWorkpadDiscovery:
    async def test_discovers_workpad_when_id_missing(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.get_comments.return_value = [
            {
                "id": 60,
                "text": "<!-- workpad:QR-125 -->\nold",
            },
        ]
        tool_state = ToolState()

        tools = _build_tools(mock_sdk, client, tool_state=tool_state)
        result = await tools["tracker_update_workpad"]({"text": "new content"})

        text = result["content"][0]["text"]
        assert "updated" in text.lower()
        assert tool_state.workpad_comment_id == 60
        client.update_comment.assert_called_once()


class TestUpdateWorkpadNoWorkpad:
    async def test_returns_error_when_no_workpad(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        client.get_comments.return_value = []
        tool_state = ToolState()

        tools = _build_tools(mock_sdk, client, tool_state=tool_state)
        result = await tools["tracker_update_workpad"]({"text": "content"})

        text = result["content"][0]["text"]
        assert "no workpad" in text.lower()


class TestWorkpadToolStateNone:
    async def test_create_workpad_returns_error(self, mock_sdk) -> None:
        client = MagicMock()
        tools = _build_tools(mock_sdk, client)

        result = await tools["tracker_create_workpad"]({"text": "content"})
        text = result["content"][0]["text"]
        assert "not available" in text.lower()

    async def test_update_workpad_returns_error(self, mock_sdk) -> None:
        client = MagicMock()
        tools = _build_tools(mock_sdk, client)

        result = await tools["tracker_update_workpad"]({"text": "content"})
        text = result["content"][0]["text"]
        assert "not available" in text.lower()


class TestMarkComplete:
    async def test_sets_task_complete_flag(self, mock_sdk) -> None:
        from orchestrator.tracker_tools import ToolState

        client = MagicMock()
        tool_state = ToolState()
        tools = _build_tools(mock_sdk, client, tool_state=tool_state)

        result = await tools["tracker_mark_complete"]({})
        assert tool_state.task_complete is True
        text = result["content"][0]["text"]
        assert "complete" in text.lower()

    async def test_returns_error_without_tool_state(
        self,
        mock_sdk,
    ) -> None:
        client = MagicMock()
        tools = _build_tools(mock_sdk, client)

        result = await tools["tracker_mark_complete"]({})
        text = result["content"][0]["text"]
        assert "not available" in text.lower()
