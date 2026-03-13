"""Tests for agent_runner module (SDK-based)."""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from orchestrator.config import Config
from orchestrator.tracker_client import TrackerClient, TrackerIssue


class _RaisingAsyncGen:
    """Async iterator that raises on first iteration."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        raise self._error


def _make_config(**overrides) -> Config:
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_issue(**overrides) -> TrackerIssue:
    defaults = dict(
        key="QR-1",
        summary="Test task",
        description="Do something",
        components=["Бекенд"],
        tags=["ai-task"],
        status="open",
    )
    defaults.update(overrides)
    return TrackerIssue(**defaults)


class TestBuildOptions:
    def test_builds_options(self, mock_sdk, tmp_path) -> None:
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        opts = runner._build_options(_make_issue(), cwd="/tmp/wt/QR-1")
        assert opts is not None

    def test_no_security_hook(self, mock_sdk, tmp_path) -> None:
        """_build_options must not pass security hooks — agents are unrestricted."""
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        runner._build_options(_make_issue(), cwd="/tmp/wt/QR-1")
        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        # hooks should be empty dict — no security whitelist
        assert call_kwargs.get("hooks") == {}

    def test_model_override(self, mock_sdk, tmp_path) -> None:
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        opts = runner._build_options(_make_issue(), model="claude-opus-4-6", cwd="/tmp")
        assert opts is not None
        # Verify ClaudeAgentOptions was called with the override model
        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert call_kwargs["model"] == "claude-opus-4-6"

    def test_model_default_when_none(self, mock_sdk, tmp_path) -> None:
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        opts = runner._build_options(_make_issue(), model=None, cwd="/tmp")
        assert opts is not None
        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert call_kwargs["model"] == config.agent_model

    def test_workspace_server_adds_tools(self, mock_sdk, tmp_path) -> None:
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        ws_server = MagicMock()
        opts = runner._build_options(_make_issue(), workspace_server=ws_server, cwd="/tmp")
        assert opts is not None
        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert "workspace" in call_kwargs["mcp_servers"]
        assert "mcp__workspace__list_available_repos" in call_kwargs["allowed_tools"]
        assert "mcp__workspace__request_worktree" in call_kwargs["allowed_tools"]

    def test_no_workspace_server(self, mock_sdk, tmp_path) -> None:
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        opts = runner._build_options(_make_issue(), cwd="/tmp")
        assert opts is not None
        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert "workspace" not in call_kwargs["mcp_servers"]
        assert "mcp__workspace__list_available_repos" not in call_kwargs["allowed_tools"]


class TestPRUrlExtraction:
    def test_extracts_pr_url(self) -> None:
        from orchestrator.agent_runner import PR_URL_PATTERN

        text = "Created PR: https://github.com/org/repo/pull/42 done"
        match = PR_URL_PATTERN.search(text)
        assert match is not None
        assert match.group(0) == "https://github.com/org/repo/pull/42"

    def test_no_pr_url(self) -> None:
        from orchestrator.agent_runner import PR_URL_PATTERN

        text = "No PR was created"
        assert PR_URL_PATTERN.search(text) is None

    def test_pr_url_with_dots_in_name(self) -> None:
        from orchestrator.agent_runner import PR_URL_PATTERN

        text = "PR: https://github.com/my.org/my-repo.js/pull/123"
        match = PR_URL_PATTERN.search(text)
        assert match is not None
        assert match.group(0) == "https://github.com/my.org/my-repo.js/pull/123"


class TestAgentSession:
    async def test_send_success(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()

        async def mock_receive():
            return
            yield  # make it an async generator

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")
        assert result.success is True
        assert result.duration_seconds is not None

    async def test_send_extracts_pr_url(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        AssistantMessage = mock_sdk.AssistantMessage
        TextBlock = mock_sdk.TextBlock

        mock_client = AsyncMock()

        msg = MagicMock(spec=AssistantMessage)
        msg.__class__ = AssistantMessage
        msg.error = None
        block = MagicMock(spec=TextBlock)
        block.__class__ = TextBlock
        block.text = "Created https://github.com/org/repo/pull/5"
        msg.content = [block]

        async def mock_receive():
            yield msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")
        assert result.success is True
        assert result.pr_url == "https://github.com/org/repo/pull/5"

    async def test_send_failure(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.query = AsyncMock(side_effect=RuntimeError("connection lost"))

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")
        assert result.success is False
        assert "connection lost" in result.output

    async def test_close(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.__aexit__ = AsyncMock(return_value=None)

        session = AgentSession(mock_client, "QR-1")
        await session.close()
        mock_client.__aexit__.assert_awaited_once()

    async def test_send_publishes_agent_result_with_duration(self, mock_sdk) -> None:
        """AGENT_RESULT event should include both cost and duration_ms."""
        from orchestrator.agent_runner import AgentSession
        from orchestrator.event_bus import EventBus

        ResultMessage = mock_sdk.ResultMessage

        mock_client = AsyncMock()
        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 1.23

        async def mock_receive():
            yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        event_bus = EventBus()
        session = AgentSession(mock_client, "QR-1", event_bus=event_bus)
        await session.send("do something")

        # Find the agent_result event in task history
        history = event_bus.get_task_history("QR-1")
        result_events = [e for e in history if e.type == "agent_result"]
        assert len(result_events) == 1
        assert result_events[0].data["cost"] == 1.23
        assert "duration_ms" in result_events[0].data
        assert isinstance(result_events[0].data["duration_ms"], float)
        assert result_events[0].data["duration_ms"] > 0

    async def test_send_detects_rate_limit_from_sdk(self, mock_sdk) -> None:
        """AgentSession.send() should set is_rate_limited when AssistantMessage.error == 'rate_limit'."""
        from orchestrator.agent_runner import AgentSession

        AssistantMessage = mock_sdk.AssistantMessage
        TextBlock = mock_sdk.TextBlock

        mock_client = AsyncMock()

        # Create message with rate_limit error
        msg = MagicMock(spec=AssistantMessage)
        msg.__class__ = AssistantMessage
        msg.error = "rate_limit"  # SDK error signal
        block = MagicMock(spec=TextBlock)
        block.__class__ = TextBlock
        block.text = "Rate limit hit"
        msg.content = [block]

        async def mock_receive():
            yield msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")

        assert result.success is True
        assert result.is_rate_limited is True

    async def test_send_no_rate_limit_when_error_none(self, mock_sdk) -> None:
        """AgentSession.send() should leave is_rate_limited=False when error is None."""
        from orchestrator.agent_runner import AgentSession

        AssistantMessage = mock_sdk.AssistantMessage
        TextBlock = mock_sdk.TextBlock

        mock_client = AsyncMock()

        msg = MagicMock(spec=AssistantMessage)
        msg.__class__ = AssistantMessage
        msg.error = None  # No error
        block = MagicMock(spec=TextBlock)
        block.__class__ = TextBlock
        block.text = "Normal output"
        msg.content = [block]

        async def mock_receive():
            yield msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")

        assert result.success is True
        assert result.is_rate_limited is False

    async def test_send_ignores_other_errors(self, mock_sdk) -> None:
        """AgentSession.send() should not set is_rate_limited for other error types."""
        from orchestrator.agent_runner import AgentSession

        AssistantMessage = mock_sdk.AssistantMessage
        TextBlock = mock_sdk.TextBlock

        mock_client = AsyncMock()

        msg = MagicMock(spec=AssistantMessage)
        msg.__class__ = AssistantMessage
        msg.error = "server_error"  # Different error type
        block = MagicMock(spec=TextBlock)
        block.__class__ = TextBlock
        block.text = "Server error occurred"
        msg.content = [block]

        async def mock_receive():
            yield msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")

        assert result.success is True
        assert result.is_rate_limited is False


class TestInterruptWithMessage:
    async def test_interrupt_puts_message_and_calls_interrupt(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.interrupt = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        await session.interrupt_with_message("stop and do X")

        assert session.has_pending_messages()
        assert session.get_pending_message() == "stop and do X"
        mock_client.interrupt.assert_awaited_once()

    async def test_no_pending_messages_initially(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        session = AgentSession(mock_client, "QR-1")

        assert not session.has_pending_messages()
        assert session.get_pending_message() is None

    async def test_multiple_messages_fifo(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.interrupt = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        await session.interrupt_with_message("first")
        await session.interrupt_with_message("second")

        assert session.get_pending_message() == "first"
        assert session.get_pending_message() == "second"
        assert not session.has_pending_messages()

    async def test_get_pending_message_returns_none_when_empty(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.interrupt = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        await session.interrupt_with_message("msg")
        session.get_pending_message()  # consume it

        assert session.get_pending_message() is None


class TestInterrupt:
    async def test_interrupt_calls_client_interrupt(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.interrupt = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        await session.interrupt()

        mock_client.interrupt.assert_awaited_once()

    async def test_interrupt_does_not_queue_message(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.interrupt = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        await session.interrupt()

        assert not session.has_pending_messages()


class TestCreateSession:
    async def test_creates_session(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentRunner, AgentSession

        config = _make_config()
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_sdk.ClaudeSDKClient.return_value = mock_client

        session = await runner.create_session(_make_issue(), cwd="/tmp/wt/QR-1")
        assert isinstance(session, AgentSession)
        mock_client.__aenter__.assert_awaited_once()


class TestTokenTracking:
    """Tests for token usage tracking in AgentResult and AgentSession."""

    async def test_send_tracks_token_usage(self, mock_sdk) -> None:
        """AgentSession.send() should extract input/output tokens from ResultMessage."""
        from orchestrator.agent_runner import AgentSession

        ResultMessage = mock_sdk.ResultMessage

        mock_client = AsyncMock()
        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 0.5
        # SDK provides usage dict with input/output tokens
        result_msg.usage = {"input_tokens": 1500, "output_tokens": 800}

        async def mock_receive():
            yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("analyze code")

        assert result.success is True
        assert result.input_tokens == 1500
        assert result.output_tokens == 800
        assert result.total_tokens == 2300

    async def test_send_handles_missing_usage(self, mock_sdk) -> None:
        """AgentSession.send() should handle missing usage gracefully."""
        from orchestrator.agent_runner import AgentSession

        ResultMessage = mock_sdk.ResultMessage

        mock_client = AsyncMock()
        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 0.5
        # No usage field

        async def mock_receive():
            yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("analyze code")

        assert result.success is True
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.total_tokens == 0

    async def test_agent_result_has_token_fields(self) -> None:
        """AgentResult should have input_tokens, output_tokens, total_tokens fields."""
        from orchestrator.agent_runner import AgentResult

        result = AgentResult(
            success=True,
            output="done",
            input_tokens=1000,
            output_tokens=500,
        )

        assert result.input_tokens == 1000
        assert result.output_tokens == 500
        assert result.total_tokens == 1500


class TestSessionId:
    """Tests for session_id capture on AgentSession."""

    async def test_session_id_none_initially(self, mock_sdk) -> None:
        """AgentSession.session_id should be None before any send()."""
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        session = AgentSession(mock_client, "QR-1")
        assert session.session_id is None

    async def test_send_captures_session_id(self, mock_sdk) -> None:
        """AgentSession.send() should capture session_id from ResultMessage."""
        from orchestrator.agent_runner import AgentSession

        ResultMessage = mock_sdk.ResultMessage

        mock_client = AsyncMock()
        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 0.5
        result_msg.session_id = "session-abc-123"

        async def mock_receive():
            yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        await session.send("do something")

        assert session.session_id == "session-abc-123"

    async def test_send_handles_missing_session_id(self, mock_sdk) -> None:
        """AgentSession.send() should handle ResultMessage without session_id."""
        from orchestrator.agent_runner import AgentSession

        ResultMessage = mock_sdk.ResultMessage

        mock_client = AsyncMock()
        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 0.5
        # No session_id attribute

        async def mock_receive():
            yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        await session.send("do something")

        assert session.session_id is None


class TestSendSkipsUnknownMessageTypes:
    """send() should skip unknown SDK message types instead of failing."""

    async def test_send_skips_rate_limit_event(self, mock_sdk) -> None:
        """When receive_response() raises for unknown message type,
        send() should retry and succeed."""
        from orchestrator.agent_runner import AgentSession

        AssistantMessage = mock_sdk.AssistantMessage
        ResultMessage = mock_sdk.ResultMessage
        TextBlock = mock_sdk.TextBlock

        mock_client = AsyncMock()

        # Create messages
        text_block = MagicMock(spec=TextBlock)
        text_block.__class__ = TextBlock
        text_block.text = "Working on it..."

        assistant_msg = MagicMock(spec=AssistantMessage)
        assistant_msg.__class__ = AssistantMessage
        assistant_msg.error = None
        assistant_msg.content = [text_block]

        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 0.42

        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield assistant_msg
                raise Exception("Unknown message type: rate_limit_event")
            else:
                yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")

        assert result.success is True
        assert "Working on it..." in result.output
        assert result.cost_usd == 0.42

    async def test_send_preserves_output_across_retries(self, mock_sdk) -> None:
        """Output collected before the crash is preserved."""
        from orchestrator.agent_runner import AgentSession

        AssistantMessage = mock_sdk.AssistantMessage
        ResultMessage = mock_sdk.ResultMessage
        TextBlock = mock_sdk.TextBlock

        mock_client = AsyncMock()

        block1 = MagicMock(spec=TextBlock)
        block1.__class__ = TextBlock
        block1.text = "Part 1"

        msg1 = MagicMock(spec=AssistantMessage)
        msg1.__class__ = AssistantMessage
        msg1.error = None
        msg1.content = [block1]

        block2 = MagicMock(spec=TextBlock)
        block2.__class__ = TextBlock
        block2.text = "Part 2"

        msg2 = MagicMock(spec=AssistantMessage)
        msg2.__class__ = AssistantMessage
        msg2.error = None
        msg2.content = [block2]

        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 0.1

        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield msg1
                raise Exception("Unknown message type: rate_limit_event")
            else:
                yield msg2
                yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")

        assert result.success is True
        assert "Part 1" in result.output
        assert "Part 2" in result.output

    async def test_send_fails_on_non_parse_error(self, mock_sdk) -> None:
        """Non-parse errors are NOT retried — they fail immediately."""
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()

        mock_client.receive_response = lambda: _RaisingAsyncGen(
            ConnectionError("transport closed"),
        )
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")

        assert result.success is False
        assert "transport closed" in result.output

    async def test_send_fails_after_max_retries(self, mock_sdk) -> None:
        """If unknown message types exceed retry limit, send fails."""
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()

        mock_client.receive_response = lambda: _RaisingAsyncGen(
            Exception("Unknown message type: rate_limit_event"),
        )
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")

        assert result.success is False
        assert "Unknown message type" in result.output

    async def test_send_skips_multiple_sequential_unknown_types(self, mock_sdk) -> None:
        """Multiple unknown messages scattered among real ones."""
        from orchestrator.agent_runner import AgentSession

        AssistantMessage = mock_sdk.AssistantMessage
        ResultMessage = mock_sdk.ResultMessage
        TextBlock = mock_sdk.TextBlock

        mock_client = AsyncMock()

        block = MagicMock(spec=TextBlock)
        block.__class__ = TextBlock
        block.text = "output"

        assistant_msg = MagicMock(spec=AssistantMessage)
        assistant_msg.__class__ = AssistantMessage
        assistant_msg.error = None
        assistant_msg.content = [block]

        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 0.5

        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield assistant_msg
                raise Exception("Unknown message type: rate_limit_event")
            elif call_count == 2:
                raise Exception("Unknown message type: rate_limit_event")
            else:
                yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        result = await session.send("do something")

        assert result.success is True
        assert "output" in result.output
        assert result.cost_usd == 0.5
        assert call_count == 3


class TestMergeResults:
    """Tests for merge_results (public API)."""

    def test_merge_accumulates_cost(self) -> None:
        from orchestrator.agent_runner import AgentResult, merge_results

        base = AgentResult(
            success=True,
            output="base",
            cost_usd=1.0,
            duration_seconds=10.0,
        )
        update = AgentResult(
            success=True,
            output="update",
            cost_usd=0.5,
            duration_seconds=5.0,
        )
        merged = merge_results(base, update)
        assert merged.cost_usd == 1.5
        assert merged.duration_seconds == 15.0

    def test_merge_sticky_success(self) -> None:
        from orchestrator.agent_runner import AgentResult, merge_results

        base = AgentResult(success=True, output="ok")
        update = AgentResult(success=False, output="fail")
        merged = merge_results(base, update)
        assert merged.success is True

    def test_merge_latest_pr_url(self) -> None:
        from orchestrator.agent_runner import AgentResult, merge_results

        base = AgentResult(
            success=True,
            output="",
            pr_url="http://pr/1",
        )
        update = AgentResult(
            success=True,
            output="",
            pr_url="http://pr/2",
        )
        merged = merge_results(base, update)
        assert merged.pr_url == "http://pr/2"

    def test_merge_sticky_is_rate_limited(self) -> None:
        """is_rate_limited is a sticky flag: once True, stays True."""
        from orchestrator.agent_runner import AgentResult, merge_results

        # Base has rate limit, update doesn't
        base = AgentResult(success=True, output="", is_rate_limited=True)
        update = AgentResult(success=True, output="", is_rate_limited=False)
        merged = merge_results(base, update)
        assert merged.is_rate_limited is True

        # Base doesn't have rate limit, update does
        base2 = AgentResult(success=True, output="", is_rate_limited=False)
        update2 = AgentResult(success=True, output="", is_rate_limited=True)
        merged2 = merge_results(base2, update2)
        assert merged2.is_rate_limited is True

        # Both False
        base3 = AgentResult(success=True, output="", is_rate_limited=False)
        update3 = AgentResult(success=True, output="", is_rate_limited=False)
        merged3 = merge_results(base3, update3)
        assert merged3.is_rate_limited is False


class TestDrainPendingMessages:
    """Tests for AgentSession.drain_pending_messages()."""

    async def test_drain_sends_all_pending(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentResult, AgentSession

        mock_client = AsyncMock()

        async def mock_receive():
            return
            yield

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        # Queue 2 pending messages
        session._pending_messages.put_nowait("msg1")
        session._pending_messages.put_nowait("msg2")

        base = AgentResult(
            success=True,
            output="base",
            cost_usd=0.0,
            duration_seconds=0.0,
        )
        result = await session.drain_pending_messages(base)
        assert result.success is True
        # Both messages should have been sent
        assert mock_client.query.await_count == 2
        assert not session.has_pending_messages()

    async def test_drain_noop_when_empty(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentResult, AgentSession

        mock_client = AsyncMock()
        session = AgentSession(mock_client, "QR-1")

        base = AgentResult(
            success=True,
            output="base",
            cost_usd=1.0,
            duration_seconds=5.0,
        )
        result = await session.drain_pending_messages(base)
        # Same object returned, no sends
        assert result is base
        assert mock_client.query.await_count == 0

    async def test_drain_catches_exceptions(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentResult, AgentSession

        mock_client = AsyncMock()
        mock_client.query = AsyncMock(side_effect=RuntimeError("connection lost"))

        session = AgentSession(mock_client, "QR-1")
        session._pending_messages.put_nowait("msg1")

        base = AgentResult(
            success=True,
            output="base",
            cost_usd=1.0,
            duration_seconds=5.0,
        )
        # Should not raise — catches exception
        result = await session.drain_pending_messages(base)
        assert result.success is True  # base preserved


class TestTransferPendingMessages:
    """Tests for AgentSession.transfer_pending_messages()."""

    async def test_transfer_moves_all_messages(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        old = AgentSession(mock_client, "QR-1")
        new = AgentSession(mock_client, "QR-1")

        old._pending_messages.put_nowait("msg1")
        old._pending_messages.put_nowait("msg2")

        count = old.transfer_pending_messages(new)

        assert count == 2
        assert not old.has_pending_messages()
        assert new.has_pending_messages()
        assert new.get_pending_message() == "msg1"
        assert new.get_pending_message() == "msg2"

    async def test_transfer_noop_when_empty(self, mock_sdk) -> None:
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        old = AgentSession(mock_client, "QR-1")
        new = AgentSession(mock_client, "QR-1")

        count = old.transfer_pending_messages(new)

        assert count == 0
        assert not new.has_pending_messages()


class TestBuildOptionsWithResume:
    """Tests for resume_session_id in _build_options and create_session."""

    def test_build_options_with_resume(self, mock_sdk, tmp_path) -> None:
        """_build_options should pass resume and fork_session when resume_session_id is given."""
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        runner._build_options(_make_issue(), resume_session_id="ses-xyz", cwd="/tmp")

        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert call_kwargs["resume"] == "ses-xyz"
        assert call_kwargs["fork_session"] is True

    def test_build_options_without_resume(self, mock_sdk, tmp_path) -> None:
        """_build_options should not pass resume/fork_session when resume_session_id is None."""
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        runner._build_options(_make_issue(), resume_session_id=None, cwd="/tmp")

        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert call_kwargs.get("resume") is None
        assert call_kwargs.get("fork_session") is None

    async def test_create_session_passes_resume(self, mock_sdk, tmp_path) -> None:
        """create_session should forward resume_session_id to _build_options."""
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_sdk.ClaudeSDKClient.return_value = mock_client

        await runner.create_session(_make_issue(), resume_session_id="ses-abc", cwd="/tmp")

        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert call_kwargs["resume"] == "ses-abc"
        assert call_kwargs["fork_session"] is True


class TestTrackerToolsInAllowedList:
    """Tests that all tracker tools are properly included in allowed_tools."""

    def test_tracker_signal_blocked_in_allowed_tools(self, mock_sdk, tmp_path) -> None:
        """tracker_signal_blocked tool must be in allowed_tools list."""
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        opts = runner._build_options(_make_issue(), cwd="/tmp")
        assert opts is not None
        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert "mcp__tracker__tracker_signal_blocked" in call_kwargs["allowed_tools"]


class TestAgentResultTokensInEvent:
    """AGENT_RESULT event must include input/output tokens."""

    async def test_agent_result_event_includes_tokens(
        self,
        mock_sdk,
    ) -> None:
        """AGENT_RESULT event data has input_tokens and output_tokens."""
        from orchestrator.agent_runner import AgentSession
        from orchestrator.event_bus import EventBus

        ResultMessage = mock_sdk.ResultMessage

        mock_client = AsyncMock()
        result_msg = MagicMock(spec=ResultMessage)
        result_msg.__class__ = ResultMessage
        result_msg.total_cost_usd = 0.5
        result_msg.usage = {
            "input_tokens": 2000,
            "output_tokens": 1000,
        }

        async def mock_receive():
            yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        event_bus = EventBus()
        session = AgentSession(
            mock_client,
            "QR-1",
            event_bus=event_bus,
        )
        await session.send("do something")

        history = event_bus.get_task_history("QR-1")
        result_events = [e for e in history if e.type == "agent_result"]
        assert len(result_events) == 1
        data = result_events[0].data
        assert data["input_tokens"] == 2000
        assert data["output_tokens"] == 1000


class TestCumulativeTokenTracking:
    """Cumulative token tracking on AgentSession."""

    async def test_cumulative_tokens_update_latest_wins(
        self,
        mock_sdk,
    ) -> None:
        """cumulative tokens use latest-wins semantics."""
        from orchestrator.agent_runner import AgentSession

        ResultMessage = mock_sdk.ResultMessage

        mock_client = AsyncMock()

        result_msg1 = MagicMock(spec=ResultMessage)
        result_msg1.__class__ = ResultMessage
        result_msg1.total_cost_usd = 0.1
        result_msg1.usage = {
            "input_tokens": 500,
            "output_tokens": 200,
        }

        result_msg2 = MagicMock(spec=ResultMessage)
        result_msg2.__class__ = ResultMessage
        result_msg2.total_cost_usd = 0.2
        result_msg2.usage = {
            "input_tokens": 1500,
            "output_tokens": 800,
        }

        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield result_msg1
            else:
                yield result_msg2

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")

        await session.send("first")
        assert session.cumulative_input_tokens == 500
        assert session.cumulative_output_tokens == 200

        await session.send("second")
        assert session.cumulative_input_tokens == 1500
        assert session.cumulative_output_tokens == 800

    async def test_cumulative_tokens_initial_zero(
        self,
        mock_sdk,
    ) -> None:
        """Cumulative tokens start at zero."""
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        session = AgentSession(mock_client, "QR-1")
        assert session.cumulative_input_tokens == 0
        assert session.cumulative_output_tokens == 0

    async def test_transfer_cumulative_tokens(
        self,
        mock_sdk,
    ) -> None:
        """transfer_cumulative_tokens copies values."""
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        old = AgentSession(mock_client, "QR-1")
        old.cumulative_input_tokens = 3000
        old.cumulative_output_tokens = 1500

        new = AgentSession(mock_client, "QR-1")
        old.transfer_cumulative_tokens(new)

        assert new.cumulative_input_tokens == 3000
        assert new.cumulative_output_tokens == 1500

    async def test_no_cumulative_cost_attribute(
        self,
        mock_sdk,
    ) -> None:
        """AgentSession should NOT have cumulative_cost."""
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        session = AgentSession(mock_client, "QR-1")
        assert not hasattr(session, "cumulative_cost")


class TestSessionClosed:
    """Tests for AgentSession._closed flag."""

    async def test_send_after_close_returns_failure(self, mock_sdk) -> None:
        """send() after close() returns failure without calling SDK."""
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.query = AsyncMock()

        session = AgentSession(mock_client, "QR-1")
        await session.close()
        result = await session.send("do something")

        assert result.success is False
        assert result.output == "Session closed"
        mock_client.query.assert_not_awaited()

    async def test_closed_property_reflects_state(self, mock_sdk) -> None:
        """closed property is False initially, True after close()."""
        from orchestrator.agent_runner import AgentSession

        mock_client = AsyncMock()
        mock_client.__aexit__ = AsyncMock(return_value=None)

        session = AgentSession(mock_client, "QR-1")
        assert session.closed is False

        await session.close()
        assert session.closed is True


class TestContinuationExhaustedFlag:
    """Tests for continuation_exhausted field on AgentResult."""

    def test_defaults_to_false(self) -> None:
        from orchestrator.agent_runner import AgentResult

        result = AgentResult(success=True, output="done")
        assert result.continuation_exhausted is False

    def test_merge_sticky_true_stays_true(self) -> None:
        from orchestrator.agent_runner import AgentResult, merge_results

        base = AgentResult(
            success=True,
            output="",
            continuation_exhausted=True,
        )
        update = AgentResult(
            success=True,
            output="",
            continuation_exhausted=False,
        )
        merged = merge_results(base, update)
        assert merged.continuation_exhausted is True

    def test_merge_false_false_stays_false(self) -> None:
        from orchestrator.agent_runner import AgentResult, merge_results

        base = AgentResult(
            success=True,
            output="",
            continuation_exhausted=False,
        )
        update = AgentResult(
            success=True,
            output="",
            continuation_exhausted=False,
        )
        merged = merge_results(base, update)
        assert merged.continuation_exhausted is False


class TestExternallyResolvedFlag:
    """Tests for externally_resolved field on AgentResult."""

    def test_defaults_to_false(self) -> None:
        from orchestrator.agent_runner import AgentResult

        result = AgentResult(success=True, output="done")
        assert result.externally_resolved is False

    def test_merge_sticky_true_stays_true(self) -> None:
        from orchestrator.agent_runner import AgentResult, merge_results

        base = AgentResult(
            success=True,
            output="",
            externally_resolved=True,
        )
        update = AgentResult(
            success=True,
            output="",
            externally_resolved=False,
        )
        merged = merge_results(base, update)
        assert merged.externally_resolved is True

    def test_merge_false_false_stays_false(self) -> None:
        from orchestrator.agent_runner import AgentResult, merge_results

        base = AgentResult(
            success=True,
            output="",
            externally_resolved=False,
        )
        update = AgentResult(
            success=True,
            output="",
            externally_resolved=False,
        )
        merged = merge_results(base, update)
        assert merged.externally_resolved is False


class TestBuildOptionsWithMailbox:
    """Characterization: mailbox/comm branch in _build_options."""

    def test_mailbox_adds_comm_server_and_tools(
        self,
        mock_sdk,
        tmp_path,
    ) -> None:
        """When mailbox is provided, comm MCP server and tools are added."""
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        mailbox = MagicMock()
        runner._build_options(
            _make_issue(),
            mailbox=mailbox,
            cwd="/tmp",
        )

        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert "comm" in call_kwargs["mcp_servers"]
        for tool_name in (
            "mcp__comm__list_running_agents",
            "mcp__comm__send_message_to_agent",
            "mcp__comm__send_request_to_agent",
            "mcp__comm__reply_to_message",
            "mcp__comm__check_messages",
        ):
            assert tool_name in call_kwargs["allowed_tools"]

    def test_no_mailbox_no_comm_tools(
        self,
        mock_sdk,
        tmp_path,
    ) -> None:
        """Without mailbox, no comm server or tools."""
        from orchestrator.agent_runner import AgentRunner

        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow")

        config = _make_config(workflow_prompt_path=str(wf))
        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(config, tracker)

        runner._build_options(_make_issue(), cwd="/tmp")

        call_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
        assert "comm" not in call_kwargs["mcp_servers"]
        assert "mcp__comm__list_running_agents" not in (call_kwargs["allowed_tools"])


class TestSendToolStateConsumption:
    """Characterization: send() reads and resets ToolState."""

    async def test_send_reads_needs_info(self, mock_sdk) -> None:
        """send() returns needs_info=True and resets the flag."""
        from orchestrator.agent_runner import AgentSession
        from orchestrator.tracker_tools import ToolState

        mock_client = AsyncMock()

        async def mock_receive():
            return
            yield

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        ts = ToolState(needs_info_requested=True)
        session = AgentSession(
            mock_client,
            "QR-1",
            tool_state=ts,
        )
        result = await session.send("do something")

        assert result.needs_info is True
        assert ts.needs_info_requested is False

    async def test_send_reads_proposals(self, mock_sdk) -> None:
        """send() returns proposals and clears the list."""
        from orchestrator.agent_runner import AgentSession
        from orchestrator.tracker_tools import ToolState

        mock_client = AsyncMock()

        async def mock_receive():
            return
            yield

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        proposal = {"title": "Idea", "description": "Details"}
        ts = ToolState(proposals=[proposal])
        session = AgentSession(
            mock_client,
            "QR-1",
            tool_state=ts,
        )
        result = await session.send("do something")

        assert result.proposals == [proposal]
        assert ts.proposals == []


class TestAgentOutputEvent:
    """Characterization: send() publishes AGENT_OUTPUT per TextBlock."""

    async def test_send_publishes_agent_output_event(
        self,
        mock_sdk,
    ) -> None:
        """Each TextBlock in AssistantMessage publishes AGENT_OUTPUT."""
        from orchestrator.agent_runner import AgentSession
        from orchestrator.event_bus import EventBus

        AssistantMessage = mock_sdk.AssistantMessage
        TextBlock = mock_sdk.TextBlock

        mock_client = AsyncMock()

        block1 = MagicMock(spec=TextBlock)
        block1.__class__ = TextBlock
        block1.text = "Hello"

        block2 = MagicMock(spec=TextBlock)
        block2.__class__ = TextBlock
        block2.text = " world"

        msg = MagicMock(spec=AssistantMessage)
        msg.__class__ = AssistantMessage
        msg.error = None
        msg.content = [block1, block2]

        async def mock_receive():
            yield msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        event_bus = EventBus()
        session = AgentSession(
            mock_client,
            "QR-1",
            event_bus=event_bus,
        )
        await session.send("do something")

        history = event_bus.get_task_history("QR-1")
        output_events = [e for e in history if e.type == "agent_output"]
        assert len(output_events) == 2
        assert output_events[0].data["text"] == "Hello"
        assert output_events[1].data["text"] == " world"
