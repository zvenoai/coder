"""Tests for SupervisorChatManager."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.config import Config, ReposConfig
from orchestrator.constants import CHAT_CHANNEL_KEY, HEARTBEAT_CHANNEL_KEY, TASKS_CHANNEL_KEY, EventType
from orchestrator.event_bus import EventBus


class _EmptyAsyncIter:
    """Async iterator that yields nothing — mock for SDK receive_response()."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _ChunkedAsyncIter:
    """Async iterator yielding AssistantMessages with TextBlock chunks."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        from claude_agent_sdk import AssistantMessage, TextBlock

        text = self._chunks[self._index]
        self._index += 1
        block = TextBlock(text=text)
        return AssistantMessage(content=[block], model="test")


class _MixedBlocksAsyncIter:
    """Async iterator yielding a single AssistantMessage with mixed block types."""

    def __init__(self, blocks: list) -> None:
        self._blocks = blocks
        self._done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        from claude_agent_sdk import AssistantMessage

        self._done = True
        return AssistantMessage(content=self._blocks, model="test")


class _ErrorAsyncIter:
    """Async iterator that raises an error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._error


class _BlockingAsyncIter:
    """Async iterator that blocks until an event is set, then yields chunks."""

    def __init__(self, event: asyncio.Event, chunks: list[str]) -> None:
        self._event = event
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index == 0:
            await self._event.wait()
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        from claude_agent_sdk import AssistantMessage, TextBlock

        text = self._chunks[self._index]
        self._index += 1
        block = TextBlock(text=text)
        return AssistantMessage(content=[block], model="test")


def _make_config(**overrides) -> Config:
    """Create a test Config."""
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(),
        supervisor_enabled=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_mock_sdk_client(response_iter=None) -> MagicMock:
    """Create a properly configured mock ClaudeSDKClient."""
    mock_client = AsyncMock()
    if response_iter is None:
        response_iter = _EmptyAsyncIter()
    mock_client.receive_response = MagicMock(return_value=response_iter)
    mock_client.interrupt = AsyncMock()
    return mock_client


class TestChatMessage:
    def test_dataclass_creates_correctly(self) -> None:
        from orchestrator.supervisor_chat import ChatMessage

        before = time.time() * 1000  # milliseconds
        msg = ChatMessage(role="user", content="Hello")
        after = time.time() * 1000  # milliseconds
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert before <= msg.timestamp <= after

    def test_timestamp_is_in_milliseconds(self) -> None:
        """Test that ChatMessage.timestamp uses milliseconds (compatible with Date.now())."""
        from orchestrator.supervisor_chat import ChatMessage

        before_ms = time.time() * 1000
        msg = ChatMessage(role="user", content="Test")
        after_ms = time.time() * 1000

        # Timestamp should be in milliseconds range
        assert before_ms <= msg.timestamp <= after_ms
        # Should be > 1e12 (milliseconds since epoch in 2001+)
        assert msg.timestamp > 1e12


class TestSupervisorChatManager:
    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_create_session(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        info = await mgr.create_session()
        assert info.session_id is not None
        assert isinstance(info.session_id, str)
        assert len(info.session_id) > 0
        # SDK client should have been created and entered
        mock_sdk_cls.return_value.__aenter__.assert_awaited_once()

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_created_at_is_in_milliseconds(self, mock_build_server, mock_sdk_cls) -> None:
        """Test that ChatSessionInfo.created_at uses milliseconds (compatible with ChatMessage.timestamp)."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        before_ms = time.time() * 1000
        info = await mgr.create_session()
        after_ms = time.time() * 1000

        # created_at should be in milliseconds range
        assert before_ms <= info.created_at <= after_ms
        # Should be > 1e12 (milliseconds since epoch in 2001+)
        assert info.created_at > 1e12

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_get_session_info(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        session_info = await mgr.create_session()
        info = mgr.get_session_info()
        assert info is not None
        assert info.session_id == session_info.session_id
        assert info.message_count == 0
        assert info.generating is False
        assert info.created_at > 0

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_get_session_info_no_session(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        assert mgr.get_session_info() is None

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_get_history_empty(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        assert mgr.get_history() == []

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_send_publishes_user_event(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Hello")

        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        user_events = [e for e in events if e.type == EventType.SUPERVISOR_CHAT_USER]
        assert len(user_events) == 1
        assert user_events[0].data["text"] == "Hello"

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_create_session_initializes_memory_index(self, mock_build_server, mock_sdk_cls) -> None:
        """Test that memory index is initialized when provided to create_session."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mock_memory = AsyncMock()
        mock_memory.initialize = AsyncMock()
        mock_embedder = MagicMock()

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
            memory=mock_memory,
            embedder=mock_embedder,
        )

        await mgr.create_session()

        # Memory index should have been initialized
        mock_memory.initialize.assert_awaited_once()

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_send_streams_chunks(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_ChunkedAsyncIter(["chunk1", "chunk2"]))
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Hello")

        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        chunk_events = [e for e in events if e.type == EventType.SUPERVISOR_CHAT_CHUNK]
        assert len(chunk_events) == 2
        assert chunk_events[0].data["text"] == "chunk1"
        assert chunk_events[1].data["text"] == "chunk2"

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_send_publishes_done(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_ChunkedAsyncIter(["response"]))
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Hello")

        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        done_events = [e for e in events if e.type == EventType.SUPERVISOR_CHAT_DONE]
        assert len(done_events) == 1

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_send_adds_to_history(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_ChunkedAsyncIter(["Hello back!"]))
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Hello")

        history = mgr.get_history()
        assert len(history) == 2
        assert history[0].role == "user"
        assert history[0].content == "Hello"
        assert history[1].role == "assistant"
        assert history[1].content == "Hello back!"

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_send_no_session_raises(self, mock_build_server, mock_sdk_cls) -> None:
        import pytest

        from orchestrator.supervisor_chat import SupervisorChatManager

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        with pytest.raises(ValueError, match="No active session"):
            await mgr.send("Hello")

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_send_while_generating_raises(self, mock_build_server, mock_sdk_cls) -> None:
        import pytest

        from orchestrator.supervisor_chat import SupervisorChatManager

        block_event = asyncio.Event()
        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_BlockingAsyncIter(block_event, ["chunk"]))
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()

        # Start send in background (it will block)
        task = asyncio.create_task(mgr.send("First"))
        await asyncio.sleep(0.05)  # Let the task start

        with pytest.raises(ValueError, match="already generating"):
            await mgr.send("Second")

        # Unblock and clean up
        block_event.set()
        await task

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_abort_cancels_generation(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        block_event = asyncio.Event()
        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_BlockingAsyncIter(block_event, ["chunk"]))
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        task = asyncio.create_task(mgr.send("Hello"))
        await asyncio.sleep(0.05)

        result = await mgr.abort()
        assert result is True

        # Wait for the cancelled task to complete
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should no longer be generating
        info = mgr.get_session_info()
        assert info is not None
        assert info.generating is False

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_abort_calls_sdk_interrupt(self, mock_build_server, mock_sdk_cls) -> None:
        """Test that abort() calls SDK interrupt() to stop in-flight requests."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        block_event = asyncio.Event()
        mock_client = _make_mock_sdk_client(response_iter=_BlockingAsyncIter(block_event, ["chunk"]))
        mock_sdk_cls.return_value = mock_client

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        task = asyncio.create_task(mgr.send("Hello"))
        await asyncio.sleep(0.05)

        # Verify interrupt() was NOT called yet
        assert mock_client.interrupt.call_count == 0

        # Call abort
        result = await mgr.abort()
        assert result is True

        # Verify interrupt() was called on the SDK client
        assert mock_client.interrupt.call_count == 1

        # Clean up
        block_event.set()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_abort_when_not_generating(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        result = await mgr.abort()
        assert result is False

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_reset_creates_new_session(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_ChunkedAsyncIter(["reply"]))
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        info_1 = await mgr.create_session()
        await mgr.send("Hello")
        assert len(mgr.get_history()) == 2

        # Reset by creating a new session
        mock_sdk_cls.return_value = _make_mock_sdk_client()
        info_2 = await mgr.create_session()

        assert info_2.session_id != info_1.session_id
        assert len(mgr.get_history()) == 0

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_close_cleans_up(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_client = _make_mock_sdk_client()
        mock_sdk_cls.return_value = mock_client
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.close()

        mock_client.__aexit__.assert_awaited_once()
        assert mgr.get_session_info() is None

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_close_interrupts_running_generation(self, mock_build_server, mock_sdk_cls) -> None:
        """Test that close() calls SDK interrupt() when generation is running."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        block_event = asyncio.Event()
        mock_client = _make_mock_sdk_client(response_iter=_BlockingAsyncIter(block_event, ["chunk"]))
        mock_sdk_cls.return_value = mock_client

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        task = asyncio.create_task(mgr.send("Hello"))
        await asyncio.sleep(0.05)

        # Verify interrupt() was NOT called yet
        assert mock_client.interrupt.call_count == 0

        # Close the manager while generation is running
        await mgr.close()

        # Verify interrupt() was called before cleanup
        assert mock_client.interrupt.call_count == 1

        # Clean up
        block_event.set()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_generation_error_publishes_error_event(self, mock_build_server, mock_sdk_cls) -> None:
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_ErrorAsyncIter(RuntimeError("LLM error")))
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Hello")

        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        error_events = [e for e in events if e.type == EventType.SUPERVISOR_CHAT_ERROR]
        assert len(error_events) == 1
        assert "LLM error" in error_events[0].data["error"]

        # generating flag should be reset
        info = mgr.get_session_info()
        assert info is not None
        assert info.generating is False

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_create_session_aenter_failure_cleans_up(self, mock_build_server, mock_sdk_cls) -> None:
        """If __aenter__ fails during create_session, state should be cleaned up."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_client = _make_mock_sdk_client()
        mock_client.__aenter__.side_effect = RuntimeError("connection failed")
        mock_sdk_cls.return_value = mock_client

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        import pytest

        with pytest.raises(RuntimeError, match="connection failed"):
            await mgr.create_session()

        # State must be clean — no stale session_id or client
        assert mgr.get_session_info() is None
        assert mgr._client is None

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_concurrent_create_session_does_not_leak_clients(self, mock_build_server, mock_sdk_cls) -> None:
        """Concurrent create_session calls should not leak SDK clients."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        # Track all created clients and their __aenter__ calls
        created_clients = []
        aenter_count = 0
        aenter_started = asyncio.Event()

        async def delayed_aenter():
            """Simulate slow __aenter__ to trigger race condition."""
            nonlocal aenter_count
            client_num = aenter_count
            aenter_count += 1

            if client_num == 0:
                # First client: signal that __aenter__ started, then wait a bit
                aenter_started.set()
                await asyncio.sleep(0.1)
            else:
                # Second client: wait until first __aenter__ has started
                await aenter_started.wait()

        def make_client(*args, **kwargs):
            client = _make_mock_sdk_client()
            # Replace __aenter__ with delayed version to expose race window
            client.__aenter__ = AsyncMock(side_effect=delayed_aenter)
            created_clients.append(client)
            return client

        mock_sdk_cls.side_effect = make_client

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        # Start two concurrent create_session calls
        session_infos = await asyncio.gather(
            mgr.create_session(),
            mgr.create_session(),
        )

        # BUG: Both clients were created and entered
        assert len(created_clients) == 2, "Both concurrent calls created clients"

        # Only one session should be active
        info = mgr.get_session_info()
        assert info is not None
        assert info.session_id in [s.session_id for s in session_infos]

        # BUG: The first client was NOT closed (leaked)
        # After the fix, all clients except the final one should be closed
        for i, client in enumerate(created_clients[:-1]):
            client.__aexit__.assert_awaited_once(), f"Client {i} should have been closed to prevent leak"

        # The last client should be the active one and should NOT be closed
        assert created_clients[-1].__aexit__.await_count == 0, "Active client should not be closed"

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_close_during_create_session_race(self, mock_build_server, mock_sdk_cls) -> None:
        """Test that close() is properly serialized with create_session().

        With the fix, close() waits for create_session() to complete, preventing
        inconsistent state where _client is set but _session_id is None.
        """
        from orchestrator.supervisor_chat import SupervisorChatManager

        aenter_started = asyncio.Event()
        aenter_should_complete = asyncio.Event()
        close_started = asyncio.Event()

        async def delayed_aenter():
            """Simulate slow __aenter__ to expose race window."""
            aenter_started.set()
            # Wait until close() attempts to run
            await close_started.wait()
            # Brief delay to ensure close() is blocked on the lock
            await asyncio.sleep(0.05)
            # Signal that __aenter__ is done
            aenter_should_complete.set()

        mock_client = _make_mock_sdk_client()
        mock_client.__aenter__ = AsyncMock(side_effect=delayed_aenter)
        mock_sdk_cls.return_value = mock_client

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        # Start create_session in background
        create_task = asyncio.create_task(mgr.create_session())

        # Wait until __aenter__ has started
        await aenter_started.wait()

        # Now call close() in background while create_session is in progress
        async def call_close():
            close_started.set()
            await mgr.close()

        close_task = asyncio.create_task(call_close())

        # Wait for __aenter__ to complete
        await aenter_should_complete.wait()

        # Wait for both operations to complete
        session_id = await create_task
        await close_task

        # After both operations complete, state should be consistent
        # close() should have waited for create_session() to finish,
        # then closed the newly created session
        info = mgr.get_session_info()
        assert info is None, "Session should be closed after close() completes"
        assert mgr._client is None, "Client should be None after close()"
        assert mgr._session_id is None, "Session ID should be None after close()"

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_send_races_with_close(self, mock_build_server, mock_sdk_cls) -> None:
        """Test that send() does not race with close().

        Without proper synchronization, send() can read _client and _session_id
        without holding _create_lock, while close() mutates them under the lock.
        This can lead to:
        1. send() checks _client is not None → passes
        2. close() sets _client = None and _session_id = None
        3. send() publishes events with session_id=None
        4. send() fails on self._client.query(text) with NoneType error
        """
        from orchestrator.supervisor_chat import SupervisorChatManager

        query_started = asyncio.Event()
        query_should_complete = asyncio.Event()
        close_started = asyncio.Event()

        async def delayed_query(text):
            """Simulate slow query to expose race window."""
            query_started.set()
            try:
                # Wait until close() is called
                await close_started.wait()
                # Brief delay to allow close() to proceed
                await asyncio.sleep(0.05)
            finally:
                query_should_complete.set()

        mock_client = _make_mock_sdk_client(response_iter=_ChunkedAsyncIter(["reply"]))
        mock_client.query = AsyncMock(side_effect=delayed_query)
        mock_sdk_cls.return_value = mock_client

        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()

        # Start send() in background
        send_task = asyncio.create_task(mgr.send("Hello"))

        # Wait until query has started
        await query_started.wait()

        # Now call close() while send() is in progress
        async def call_close():
            close_started.set()
            await mgr.close()

        close_task = asyncio.create_task(call_close())

        # Wait for query to complete
        await query_should_complete.wait()

        # Wait for both operations to complete
        try:
            await send_task
        except (asyncio.CancelledError, Exception):
            # Expected if send() was interrupted by close()
            pass

        await close_task

        # After both operations complete, state should be consistent
        info = mgr.get_session_info()
        assert info is None, "Session should be closed"
        assert mgr._client is None, "Client should be None"
        assert mgr._session_id is None, "Session ID should be None"

        # Verify no events were published with session_id=None (the bug)
        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        for event in events:
            if "session_id" in event.data:
                assert event.data["session_id"] is not None, (
                    f"Event {event.type} has session_id=None (race condition bug)"
                )

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_abort_captures_task_before_interrupt(self, mock_build_server, mock_sdk_cls) -> None:
        """abort() captures _generation_task before await to prevent AttributeError.

        Race scenario without fix:
        1. abort() checks _generation_task is not None
        2. abort() calls await client.interrupt()
        3. During that await, send() completes and sets _generation_task = None
        4. abort() returns and tries task.cancel() → AttributeError

        Fix: Capture task reference in local variable before await.
        """
        from orchestrator.supervisor_chat import SupervisorChatManager

        # Mock client that simulates send() completing during interrupt()
        mock_client = _make_mock_sdk_client(response_iter=_ChunkedAsyncIter(["reply"]))

        # Track generation task to simulate race
        generation_task_ref = None

        async def interrupt_that_clears_task():
            nonlocal generation_task_ref
            # Simulate the race: clear _generation_task during interrupt
            # (normally done by send() in finally block)
            await asyncio.sleep(0.01)
            # Save the task reference for verification
            generation_task_ref = mgr._generation_task
            # Clear it (simulating send() completing)
            mgr._generation_task = None

        mock_client.interrupt = AsyncMock(side_effect=interrupt_that_clears_task)
        mock_sdk_cls.return_value = mock_client

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()

        # Manually set _is_generating and _generation_task to simulate running generation
        mgr._is_generating = True
        mgr._generation_task = asyncio.create_task(asyncio.sleep(10))

        # Call abort() — should NOT raise AttributeError despite task being cleared
        result = await mgr.abort()

        # abort() should return True and not crash
        assert result is True
        # Task reference should have been captured (not None at time of cancel)
        assert generation_task_ref is not None

        # Wait for task to be actually cancelled
        try:
            await generation_task_ref
        except asyncio.CancelledError:
            pass

        # Task should now be cancelled
        assert generation_task_ref.cancelled()

        # Clean up
        mgr._is_generating = False

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_create_session_with_memory_uses_correct_parameter_name(
        self, mock_build_server, mock_sdk_cls
    ) -> None:
        """Test that create_session passes memory_index (not memory) to build_supervisor_server."""
        from orchestrator.supervisor_chat import SupervisorChatManager
        from orchestrator.supervisor_memory import EmbeddingClient, MemoryIndex

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mock_build_server.return_value = MagicMock()

        memory_index = MagicMock(spec=MemoryIndex)
        embedder = MagicMock(spec=EmbeddingClient)

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
            memory=memory_index,
            embedder=embedder,
        )

        # This should not raise TypeError
        await mgr.create_session()

        # Verify build_supervisor_server was called with memory_index parameter
        mock_build_server.assert_called_once()
        call_kwargs = mock_build_server.call_args[1]
        assert "memory_index" in call_kwargs
        assert call_kwargs["memory_index"] is memory_index
        assert "memory" not in call_kwargs

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_abort_cancels_task_even_if_interrupt_fails(self, mock_build_server, mock_sdk_cls) -> None:
        """Test that abort() cancels the task even if interrupt() raises an exception."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        block_event = asyncio.Event()
        mock_client = _make_mock_sdk_client(response_iter=_BlockingAsyncIter(block_event, ["chunk"]))

        # Make interrupt() raise an exception
        mock_client.interrupt = AsyncMock(side_effect=RuntimeError("Interrupt failed"))
        mock_sdk_cls.return_value = mock_client

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        task = asyncio.create_task(mgr.send("Hello"))
        await asyncio.sleep(0.05)

        # Before fix: abort() would raise RuntimeError and never cancel the task
        # After fix: abort() should handle the error and still cancel the task
        try:
            result = await mgr.abort()
            # After fix: should return True (successfully aborted)
            assert result is True
        except RuntimeError as e:
            # Before fix: would raise here
            raise AssertionError("abort() should not raise even if interrupt() fails") from e

        # Clean up
        block_event.set()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify the task was cancelled despite interrupt() failure
        assert task.cancelled() or task.done()

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_send_empty_text_raises(self, mock_build_server, mock_sdk_cls) -> None:
        """send() rejects empty or whitespace-only text."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )
        await mgr.create_session()

        with pytest.raises(ValueError, match="empty"):
            await mgr.send("")

        with pytest.raises(ValueError, match="empty"):
            await mgr.send("   ")

        # History should remain empty — no blank messages added
        assert len(mgr.get_history()) == 0


class TestAutoSend:
    """Tests for auto_send() — autonomous system messages."""

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_auto_send_creates_session_if_none(self, mock_build_server, mock_sdk_cls) -> None:
        """auto_send should create a session automatically when none exists."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        assert mgr.get_session_info() is None

        await mgr.auto_send("Plan this epic")

        # Session should have been created
        assert mgr.get_session_info() is not None
        # Message should be in history
        assert len(mgr.get_history()) >= 1
        assert mgr.get_history()[0].content == "Plan this epic"

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_auto_send_reuses_existing_session(self, mock_build_server, mock_sdk_cls) -> None:
        """auto_send should reuse an existing session, not create a new one."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        info = await mgr.create_session()
        original_session_id = info.session_id

        await mgr.auto_send("Plan epic children")

        session_info = mgr.get_session_info()
        assert session_info is not None
        assert session_info.session_id == original_session_id

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_auto_send_waits_for_generation(self, mock_build_server, mock_sdk_cls) -> None:
        """auto_send should wait for in-progress generation to complete before sending."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        block_event = asyncio.Event()
        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_BlockingAsyncIter(block_event, ["response"]))

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )
        await mgr.create_session()

        # Start a blocking send
        blocking_task = asyncio.create_task(mgr.send("human message"))
        await asyncio.sleep(0.05)
        info = mgr.get_session_info()
        assert info is not None
        assert info.generating is True

        # Start auto_send — it should wait
        auto_task = asyncio.create_task(mgr.auto_send("system message"))
        await asyncio.sleep(0.05)
        assert not auto_task.done(), "auto_send should wait for generation to complete"

        # Unblock the first generation
        block_event.set()
        await blocking_task

        # Now auto_send needs a fresh response iterator
        mock_sdk_cls.return_value.receive_response = MagicMock(return_value=_EmptyAsyncIter())
        await auto_task

        # Both messages should be in history
        history = mgr.get_history()
        user_msgs = [m for m in history if m.role == "user"]
        assert len(user_msgs) == 2
        assert user_msgs[0].content == "human message"
        assert user_msgs[1].content == "system message"

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_auto_send_publishes_events(self, mock_build_server, mock_sdk_cls) -> None:
        """auto_send should publish chat events like regular send."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.auto_send("auto plan")

        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        event_types = [e.type for e in events]
        assert EventType.SUPERVISOR_CHAT_USER in event_types
        assert EventType.SUPERVISOR_CHAT_DONE in event_types


class TestMemoryIntegration:
    """Tests for memory lifecycle integration in supervisor chat."""

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_create_session_calls_sync(self, mock_build_server, mock_sdk_cls) -> None:
        """create_session should call sync(embedder) after initialize() to index .md files."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mock_memory = AsyncMock()
        mock_memory.initialize = AsyncMock()
        mock_memory.sync = AsyncMock()
        mock_memory.memory_dir = "/tmp/test_memory"
        mock_embedder = MagicMock()

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
            memory=mock_memory,
            embedder=mock_embedder,
        )

        await mgr.create_session()

        # Both initialize and sync should be called
        mock_memory.initialize.assert_awaited_once()
        mock_memory.sync.assert_awaited_once_with(mock_embedder)

    @patch("orchestrator.supervisor_chat.read_memory_file")
    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    @patch("orchestrator.supervisor_chat.ClaudeAgentOptions")
    async def test_create_session_injects_memory_md(
        self, mock_options_cls, mock_build_server, mock_sdk_cls, mock_read_memory
    ) -> None:
        """create_session should inject MEMORY.md content into system prompt."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mock_memory = AsyncMock()
        mock_memory.initialize = AsyncMock()
        mock_memory.sync = AsyncMock()
        mock_memory.memory_dir = "/tmp/test_memory"
        mock_embedder = MagicMock()
        mock_read_memory.return_value = "# Long-term Knowledge\n\nWe use FastAPI."

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
            memory=mock_memory,
            embedder=mock_embedder,
        )

        await mgr.create_session()

        # ClaudeAgentOptions should have been called with system_prompt containing MEMORY.md
        mock_options_cls.assert_called_once()
        call_kwargs = mock_options_cls.call_args[1]
        prompt_append = call_kwargs["system_prompt"]["append"]
        assert "Long-term Knowledge" in prompt_append
        assert "We use FastAPI" in prompt_append
        assert "<long-term-memory>" in prompt_append

    @patch("orchestrator.supervisor_chat.read_memory_file")
    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_create_session_no_memory_md_still_works(
        self, mock_build_server, mock_sdk_cls, mock_read_memory
    ) -> None:
        """create_session should work when MEMORY.md doesn't exist."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mock_memory = AsyncMock()
        mock_memory.initialize = AsyncMock()
        mock_memory.sync = AsyncMock()
        mock_memory.memory_dir = "/tmp/test_memory"
        mock_embedder = MagicMock()
        mock_read_memory.return_value = None  # No MEMORY.md

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
            memory=mock_memory,
            embedder=mock_embedder,
        )

        info = await mgr.create_session()
        assert info.session_id is not None

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_sync_failure_does_not_break_session(self, mock_build_server, mock_sdk_cls) -> None:
        """If sync() fails, session creation should still proceed."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client()
        mock_memory = AsyncMock()
        mock_memory.initialize = AsyncMock()
        mock_memory.sync = AsyncMock(side_effect=RuntimeError("embedding API down"))
        mock_memory.memory_dir = "/tmp/test_memory"
        mock_embedder = MagicMock()

        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
            memory=mock_memory,
            embedder=mock_embedder,
        )

        info = await mgr.create_session()
        assert info.session_id is not None


class TestThinkingAndToolUseBlocks:
    """Tests for ThinkingBlock and ToolUseBlock streaming."""

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_thinking_block_publishes_event(self, mock_build_server, mock_sdk_cls) -> None:
        """ThinkingBlock should publish SUPERVISOR_CHAT_THINKING event."""
        from claude_agent_sdk import ThinkingBlock

        from orchestrator.supervisor_chat import SupervisorChatManager

        thinking = ThinkingBlock(
            thinking="Let me analyze the codebase...",
            signature="",
        )

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_MixedBlocksAsyncIter([thinking]))
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Hello")

        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        thinking_events = [e for e in events if e.type == EventType.SUPERVISOR_CHAT_THINKING]
        assert len(thinking_events) == 1
        assert thinking_events[0].data["thinking"] == "Let me analyze the codebase..."

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_tool_use_block_publishes_event(self, mock_build_server, mock_sdk_cls) -> None:
        """ToolUseBlock should publish SUPERVISOR_CHAT_TOOL_USE event."""
        from claude_agent_sdk import ToolUseBlock

        from orchestrator.supervisor_chat import SupervisorChatManager

        tool_use = ToolUseBlock(
            id="tool-1",
            name="tracker_search",
            input={"query": "QR-123"},
        )

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_MixedBlocksAsyncIter([tool_use]))
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Find issue")

        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        tool_events = [e for e in events if e.type == EventType.SUPERVISOR_CHAT_TOOL_USE]
        assert len(tool_events) == 1
        assert tool_events[0].data["tool"] == "tracker_search"
        assert tool_events[0].data["input"] == {"query": "QR-123"}

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_mixed_blocks_publish_all_event_types(self, mock_build_server, mock_sdk_cls) -> None:
        """Mixed sequence of ThinkingBlock, ToolUseBlock, TextBlock publishes all events in order."""
        from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock

        from orchestrator.supervisor_chat import SupervisorChatManager

        thinking = ThinkingBlock(thinking="Analyzing...", signature="")
        tool_use = ToolUseBlock(
            id="tool-1",
            name="bash",
            input={"command": "ls"},
        )
        text = TextBlock(text="Here are the results.")

        mock_sdk_cls.return_value = _make_mock_sdk_client(
            response_iter=_MixedBlocksAsyncIter([thinking, tool_use, text])
        )
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Do something")

        events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        # Filter to only the content events (not USER/DONE)
        content_types = [
            e.type
            for e in events
            if e.type
            in (
                EventType.SUPERVISOR_CHAT_THINKING,
                EventType.SUPERVISOR_CHAT_TOOL_USE,
                EventType.SUPERVISOR_CHAT_CHUNK,
            )
        ]
        assert content_types == [
            EventType.SUPERVISOR_CHAT_THINKING,
            EventType.SUPERVISOR_CHAT_TOOL_USE,
            EventType.SUPERVISOR_CHAT_CHUNK,
        ]

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_thinking_and_tool_use_not_in_history(self, mock_build_server, mock_sdk_cls) -> None:
        """ThinkingBlock and ToolUseBlock should NOT appear in chat history — only TextBlock text."""
        from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock

        from orchestrator.supervisor_chat import SupervisorChatManager

        thinking = ThinkingBlock(thinking="Deep thought...", signature="")
        tool_use = ToolUseBlock(
            id="tool-1",
            name="memory_search",
            input={"query": "test"},
        )
        text = TextBlock(text="Final answer.")

        mock_sdk_cls.return_value = _make_mock_sdk_client(
            response_iter=_MixedBlocksAsyncIter([thinking, tool_use, text])
        )
        mgr = SupervisorChatManager(
            CHAT_CHANNEL_KEY,
            config=_make_config(),
            event_bus=EventBus(),
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("Question")

        history = mgr.get_history()
        assert len(history) == 2  # user + assistant
        assert history[1].role == "assistant"
        assert history[1].content == "Final answer."
        # No thinking or tool_use content in history
        assert "Deep thought" not in history[1].content
        assert "memory_search" not in history[1].content


class TestChannelKeyRouting:
    """Tests verifying that events use the channel_key supplied at construction."""

    @pytest.mark.parametrize("channel_key", [TASKS_CHANNEL_KEY, HEARTBEAT_CHANNEL_KEY])
    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_publishes_events_with_correct_task_key(
        self, mock_build_server, mock_sdk_cls, channel_key: str
    ) -> None:
        """Events published by a manager use its constructor-supplied channel_key."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_ChunkedAsyncIter(["hello"]))
        event_bus = EventBus()
        mgr = SupervisorChatManager(
            channel_key,
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )

        await mgr.create_session()
        await mgr.send("ping")

        history = event_bus.get_task_history(channel_key)
        assert any(e.task_key == channel_key for e in history)
        # Must NOT publish to the chat channel
        chat_history = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        assert len(chat_history) == 0

    @patch("orchestrator.supervisor_chat.ClaudeSDKClient")
    @patch("orchestrator.supervisor_chat.build_supervisor_server")
    async def test_different_channels_use_different_task_keys(self, mock_build_server, mock_sdk_cls) -> None:
        """Two managers with different channel keys publish to separate EventBus buckets."""
        from orchestrator.supervisor_chat import SupervisorChatManager

        mock_sdk_cls.return_value = _make_mock_sdk_client(response_iter=_ChunkedAsyncIter(["ok"]))
        event_bus = EventBus()
        _common_kwargs: dict = dict(
            config=_make_config(),
            event_bus=event_bus,
            tracker=MagicMock(),
            get_pending_proposals=list,
            get_recent_events=lambda count: [],
            tracker_queue="QR",
            tracker_project_id=13,
            tracker_boards=[14],
            tracker_tag="ai-task",
        )
        chat_mgr = SupervisorChatManager(CHAT_CHANNEL_KEY, **_common_kwargs)
        tasks_mgr = SupervisorChatManager(TASKS_CHANNEL_KEY, **_common_kwargs)

        await chat_mgr.create_session()
        await chat_mgr.send("from chat")

        await tasks_mgr.create_session()
        await tasks_mgr.send("from tasks")

        chat_events = event_bus.get_task_history(CHAT_CHANNEL_KEY)
        tasks_events = event_bus.get_task_history(TASKS_CHANNEL_KEY)

        assert len(chat_events) > 0
        assert len(tasks_events) > 0
        # Events must be isolated per channel
        assert all(e.task_key == CHAT_CHANNEL_KEY for e in chat_events)
        assert all(e.task_key == TASKS_CHANNEL_KEY for e in tasks_events)
