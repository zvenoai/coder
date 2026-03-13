"""Tests for inter-agent communication MCP tools."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.agent_mailbox import AgentInfo, AgentMailbox


@pytest.fixture
def mailbox() -> AgentMailbox:
    """Create a mailbox with mock callbacks."""
    mb = AgentMailbox()

    async def list_agents() -> list[AgentInfo]:
        return [
            AgentInfo(
                task_key="QR-1",
                task_summary="Backend API",
                status="running",
            ),
            AgentInfo(
                task_key="QR-2",
                task_summary="Frontend UI",
                status="running",
            ),
        ]

    async def interrupt_agent(task_key: str, message: str) -> None:
        pass

    mb.set_callbacks(
        list_agents=list_agents,
        interrupt_agent=interrupt_agent,
    )
    mb.register_agent("QR-1")
    mb.register_agent("QR-2")
    return mb


def _build_tools(
    mailbox: AgentMailbox,
    task_key: str = "QR-1",
    summary: str = "Backend API",
) -> dict:
    """Build comm server and return {tool_name: tool_callable}."""
    from orchestrator.comm_tools import build_comm_server

    build_comm_server(mailbox, task_key, summary)

    from claude_agent_sdk import create_sdk_mcp_server

    call_kwargs = create_sdk_mcp_server.call_args
    tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get(
        "tools",
        call_kwargs[0][2] if len(call_kwargs[0]) > 2 else [],
    )
    return {t._tool_name: t for t in tools}


class TestToolRegistration:
    def test_creates_five_tools(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox)
        assert len(tools) == 5

    def test_correct_tool_names(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox)
        assert set(tools.keys()) == {
            "list_running_agents",
            "send_message_to_agent",
            "send_request_to_agent",
            "reply_to_message",
            "check_messages",
        }

    def test_server_name_is_comm(self, mailbox: AgentMailbox) -> None:
        from orchestrator.comm_tools import build_comm_server

        build_comm_server(mailbox, "QR-1", "Backend API")

        from claude_agent_sdk import create_sdk_mcp_server

        call_kwargs = create_sdk_mcp_server.call_args
        name = call_kwargs.kwargs.get("name") or call_kwargs[0][0]
        assert name == "comm"


class TestListRunningAgents:
    @pytest.mark.asyncio
    async def test_excludes_self(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox, "QR-1")
        result = await tools["list_running_agents"]({})

        text = result["content"][0]["text"]
        assert "QR-2" in text
        assert "QR-1" not in text

    @pytest.mark.asyncio
    async def test_empty_when_no_peers(self) -> None:
        mb = AgentMailbox()

        async def list_agents() -> list[AgentInfo]:
            return [
                AgentInfo(
                    task_key="QR-1",
                    task_summary="Only me",
                    status="running",
                )
            ]

        async def interrupt_agent(task_key: str, message: str) -> None:
            pass

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=interrupt_agent,
        )
        mb.register_agent("QR-1")

        tools = _build_tools(mb, "QR-1", "Only me")
        result = await tools["list_running_agents"]({})

        text = result["content"][0]["text"]
        assert "No other agents" in text


class TestSendMessageTool:
    @pytest.mark.asyncio
    async def test_send_success(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox, "QR-1")
        result = await tools["send_message_to_agent"]({"target_task_key": "QR-2", "message": "What API?"})

        text = result["content"][0]["text"]
        assert "sent" in text.lower() or "Message" in text

    @pytest.mark.asyncio
    async def test_send_to_self_error(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox, "QR-1")
        result = await tools["send_message_to_agent"]({"target_task_key": "QR-1", "message": "Hello me"})

        text = result["content"][0]["text"]
        assert "Error" in text or "error" in text


class TestReplyTool:
    @pytest.mark.asyncio
    async def test_reply_success(self, mailbox: AgentMailbox) -> None:
        msg = await mailbox.send_message("QR-1", "Backend", "QR-2", "Question?")
        mailbox.get_unread_messages("QR-2")

        tools = _build_tools(mailbox, "QR-2", "Frontend UI")
        result = await tools["reply_to_message"]({"message_id": msg.id, "reply_text": "Answer!"})

        text = result["content"][0]["text"]
        assert "Reply sent" in text or "reply" in text.lower()

    @pytest.mark.asyncio
    async def test_reply_error(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox, "QR-2", "Frontend UI")
        result = await tools["reply_to_message"]({"message_id": "bad-id", "reply_text": "Answer"})

        text = result["content"][0]["text"]
        assert "Error" in text or "error" in text


class TestCheckMessages:
    @pytest.mark.asyncio
    async def test_check_with_unread(self, mailbox: AgentMailbox) -> None:
        await mailbox.send_message("QR-1", "Backend", "QR-2", "Hello!")

        tools = _build_tools(mailbox, "QR-2", "Frontend UI")
        result = await tools["check_messages"]({})

        text = result["content"][0]["text"]
        assert "QR-1" in text
        assert "Hello!" in text

    @pytest.mark.asyncio
    async def test_check_empty_inbox(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox, "QR-2", "Frontend UI")
        result = await tools["check_messages"]({})

        text = result["content"][0]["text"]
        assert "No unread" in text or "no unread" in text

    @pytest.mark.asyncio
    async def test_check_messages_includes_msg_type(self, mailbox: AgentMailbox) -> None:
        from orchestrator.agent_mailbox import MessageType

        await mailbox.send_message("QR-1", "Backend", "QR-2", "Data", msg_type=MessageType.ARTIFACT)
        tools = _build_tools(mailbox, "QR-2", "Frontend UI")
        result = await tools["check_messages"]({})
        text = result["content"][0]["text"]
        assert "artifact" in text


class TestSendRequestToAgentTool:
    @pytest.mark.asyncio
    async def test_send_request_tool_blocks_until_reply(self, mailbox: AgentMailbox) -> None:
        async def reply_after_delay() -> None:
            await asyncio.sleep(0.05)
            msgs = mailbox.get_unread_messages("QR-2")
            if msgs:
                await mailbox.reply_to_message(msgs[0].id, "POST /api/v1/login", "QR-2")

        reply_task = asyncio.create_task(reply_after_delay())

        tools = _build_tools(mailbox, "QR-1", "Backend API")
        result = await tools["send_request_to_agent"](
            {"target_task_key": "QR-2", "message": "What endpoint?", "timeout_seconds": 5}
        )
        await reply_task

        text = result["content"][0]["text"]
        assert "POST /api/v1/login" in text

    @pytest.mark.asyncio
    async def test_send_request_tool_timeout_returns_message(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox, "QR-1", "Backend API")
        # timeout_seconds=0 is reliable: send_message runs outside asyncio.timeout,
        # so the message IS sent, then the timeout fires immediately on _reply_event.wait().
        result = await tools["send_request_to_agent"](
            {"target_task_key": "QR-2", "message": "No reply?", "timeout_seconds": 0}
        )
        text = result["content"][0]["text"]
        assert "Timeout" in text or "timeout" in text

    @pytest.mark.asyncio
    async def test_send_request_to_self_error(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox, "QR-1", "Backend API")
        result = await tools["send_request_to_agent"](
            {"target_task_key": "QR-1", "message": "Self?", "timeout_seconds": 1}
        )
        text = result["content"][0]["text"]
        assert "Error" in text or "error" in text


class TestListRunningAgentsWithMetadata:
    @pytest.mark.asyncio
    async def test_list_agents_includes_component(self) -> None:
        mb = AgentMailbox()

        async def list_agents() -> list[AgentInfo]:
            return [
                AgentInfo(
                    task_key="QR-2",
                    task_summary="Frontend",
                    status="running",
                    component="Фронтенд",
                    repo="org/frontend",
                )
            ]

        async def interrupt_agent(task_key: str, message: str) -> None:
            pass

        mb.set_callbacks(list_agents=list_agents, interrupt_agent=interrupt_agent)
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        tools = _build_tools(mb, "QR-1", "Backend")
        result = await tools["list_running_agents"]({})
        text = result["content"][0]["text"]
        assert "Фронтенд" in text
        assert "org/frontend" in text

    @pytest.mark.asyncio
    async def test_list_agents_without_metadata(self, mailbox: AgentMailbox) -> None:
        tools = _build_tools(mailbox, "QR-1")
        result = await tools["list_running_agents"]({})
        text = result["content"][0]["text"]
        # No brackets for empty metadata
        assert "QR-2" in text
