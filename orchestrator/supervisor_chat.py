"""Supervisor chat manager — interactive streaming chat with the supervisor agent."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from orchestrator.agent_runner import receive_response_safe
from orchestrator.config import Config
from orchestrator.constants import EventType
from orchestrator.event_bus import Event, EventBus
from orchestrator.supervisor_memory import read_memory_file
from orchestrator.supervisor_tools import build_supervisor_allowed_tools, build_supervisor_server
from orchestrator.tracker_client import TrackerClient

if TYPE_CHECKING:
    from orchestrator.agent_mailbox import AgentMailbox
    from orchestrator.dependency_manager import DependencyManager
    from orchestrator.epic_coordinator import EpicCoordinator
    from orchestrator.github_client import GitHubClient
    from orchestrator.heartbeat import HeartbeatMonitor
    from orchestrator.k8s_client import K8sClient
    from orchestrator.preflight_checker import PreflightChecker
    from orchestrator.storage import Storage
    from orchestrator.supervisor_memory import EmbeddingClient, MemoryIndex

logger = logging.getLogger(__name__)

SUPERVISOR_CHAT_MCP_SERVER_NAME = "supervisor"


def _timestamp_ms() -> float:
    """Return current timestamp in milliseconds (compatible with JavaScript Date.now())."""
    return time.time() * 1000


@dataclass
class ChatMessage:
    """A single message in the chat history."""

    role: Literal["user", "assistant"]
    content: str
    timestamp: float = field(default_factory=_timestamp_ms)


@dataclass
class ChatSessionInfo:
    """Metadata about a chat session."""

    session_id: str
    created_at: float
    message_count: int
    generating: bool


class SupervisorChatManager:
    """Manages interactive chat sessions with the supervisor agent.

    Provides create/send/abort/close lifecycle for streaming chat
    conversations via the event bus.
    """

    def __init__(
        self,
        channel_key: str,
        *,
        config: Config,
        event_bus: EventBus,
        tracker: TrackerClient,
        get_pending_proposals: Callable[[], list[dict[str, str]]],
        get_recent_events: Callable[[int], list[dict[str, object]]],
        tracker_queue: str,
        tracker_project_id: int,
        tracker_boards: list[int],
        tracker_tag: str,
        storage: Storage | None = None,
        github: GitHubClient | None = None,
        memory: MemoryIndex | None = None,
        embedder: EmbeddingClient | None = None,
        # Agent management callbacks
        list_running_tasks_callback: Callable[[], list[dict[str, object]]] | None = None,
        send_message_callback: Callable[[str, str], Awaitable[None]] | None = None,
        abort_task_callback: Callable[[str], Awaitable[None]] | None = None,
        cancel_task_callback: Callable[[str, str], Awaitable[None]] | None = None,
        epic_coordinator: EpicCoordinator | None = None,
        mailbox: AgentMailbox | None = None,
        k8s_client: K8sClient | None = None,
        dependency_manager: DependencyManager | None = None,
        # Preflight review
        preflight_checker: PreflightChecker | None = None,
        mark_dispatched_callback: Callable[[str], None] | None = None,
        remove_dispatched_callback: Callable[[str], None] | None = None,
        clear_recovery_callback: Callable[[str], None] | None = None,
        # Diagnostic callbacks
        get_state_callback: Callable[[], dict[str, Any]] | None = None,
        get_task_events_callback: (Callable[[str], list[dict[str, Any]]] | None) = None,
        # Heartbeat monitor
        heartbeat_monitor: HeartbeatMonitor | None = None,
    ) -> None:
        self._channel_key = channel_key
        self._config = config
        self._event_bus = event_bus
        self._tracker = tracker
        self._get_pending_proposals = get_pending_proposals
        self._get_recent_events = get_recent_events
        self._tracker_queue = tracker_queue
        self._tracker_project_id = tracker_project_id
        self._tracker_boards = tracker_boards
        self._tracker_tag = tracker_tag
        self._storage = storage
        self._github = github
        self._memory = memory
        self._embedder = embedder
        self._list_running_tasks = list_running_tasks_callback
        self._send_message = send_message_callback
        self._abort_task = abort_task_callback
        self._cancel_task = cancel_task_callback
        self._epic_coordinator = epic_coordinator
        self._mailbox = mailbox
        self._k8s_client = k8s_client
        self._dependency_manager = dependency_manager
        self._preflight_checker = preflight_checker
        self._mark_dispatched = mark_dispatched_callback
        self._remove_dispatched = remove_dispatched_callback
        self._clear_recovery = clear_recovery_callback
        self._get_state_callback = get_state_callback
        self._get_task_events_callback = get_task_events_callback
        self._heartbeat_monitor = heartbeat_monitor

        # Session state
        self._session_id: str | None = None
        self._created_at: float | None = None
        self._client: ClaudeSDKClient | None = None
        self._history: list[ChatMessage] = []
        self._is_generating: bool = False
        self._generation_task: asyncio.Task | None = None
        self._create_lock: asyncio.Lock = asyncio.Lock()
        self._created_task_keys: list[str] = []
        self._on_task_created: Callable[[str], None] | None = None
        self._idle_event: asyncio.Event = asyncio.Event()
        self._idle_event.set()
        self._auto_send_lock: asyncio.Lock = asyncio.Lock()

    def set_heartbeat_monitor(
        self,
        monitor: HeartbeatMonitor,
    ) -> None:
        """Set the heartbeat monitor (for circular-init wiring)."""
        self._heartbeat_monitor = monitor

    async def create_session(self) -> ChatSessionInfo:
        """Create a new chat session, closing any existing one.

        Returns session info for the newly created session.
        """
        async with self._create_lock:
            # Close existing session if any
            if self._client is not None:
                await self._close_unlocked()

            session_id = str(uuid.uuid4())
            created_at = _timestamp_ms()
            self._history = []
            self._is_generating = False
            self._generation_task = None
            self._created_task_keys = []

            # Track created tasks to publish events
            def _on_task_created(key: str) -> None:
                self._created_task_keys.append(key)

            self._on_task_created = _on_task_created

            # Build MCP server and allowed tools
            has_agent_mgmt = self._list_running_tasks is not None
            supervisor_server = build_supervisor_server(
                client=self._tracker,
                get_pending_proposals=self._get_pending_proposals,
                get_recent_events=self._get_recent_events,
                on_task_created=_on_task_created,
                tracker_queue=self._tracker_queue,
                tracker_project_id=self._tracker_project_id,
                tracker_boards=self._tracker_boards,
                tracker_tag=self._tracker_tag,
                storage=self._storage,
                github=self._github,
                memory_index=self._memory,
                embedder=self._embedder,
                list_running_tasks_callback=self._list_running_tasks,
                send_message_callback=self._send_message,
                abort_task_callback=self._abort_task,
                cancel_task_callback=self._cancel_task,
                epic_coordinator=self._epic_coordinator,
                mailbox=self._mailbox,
                k8s_client=self._k8s_client,
                dependency_manager=self._dependency_manager,
                preflight_checker=self._preflight_checker,
                mark_dispatched_callback=self._mark_dispatched,
                remove_dispatched_callback=self._remove_dispatched,
                clear_recovery_callback=self._clear_recovery,
                event_bus=self._event_bus,
                heartbeat_monitor=self._heartbeat_monitor,
                get_state_callback=self._get_state_callback,
                get_task_events_callback=self._get_task_events_callback,
            )

            # Initialize and sync memory index (OpenClaw pattern):
            # 1. initialize() — create SQLite tables
            # 2. sync(embedder) — index .md files for hybrid search
            # 3. Read MEMORY.md — inject curated long-term knowledge into system prompt
            memory_context = ""
            if self._memory is not None:
                try:
                    await self._memory.initialize()
                    if self._embedder is not None:
                        await self._memory.sync(self._embedder)
                except Exception:
                    logger.warning("Failed to initialize/sync memory index", exc_info=True)

                # Inject MEMORY.md into system prompt (always, like OpenClaw bootstrap)
                memory_content = read_memory_file(self._memory.memory_dir, "MEMORY.md")
                if memory_content:
                    memory_context = "\n\n<long-term-memory>\n" + memory_content + "\n</long-term-memory>"

            allowed_tools = build_supervisor_allowed_tools(
                SUPERVISOR_CHAT_MCP_SERVER_NAME,
                has_storage=self._storage is not None,
                has_github=self._github is not None,
                has_memory=self._memory is not None and self._embedder is not None,
                has_agent_mgmt=has_agent_mgmt,
                has_epics=self._epic_coordinator is not None,
                has_mailbox=self._mailbox is not None,
                has_k8s=self._k8s_client is not None,
                has_dependencies=self._dependency_manager is not None,
                has_preflight=self._preflight_checker is not None and self._dependency_manager is not None,
                has_diagnostics=(self._get_state_callback is not None and self._get_task_events_callback is not None),
                has_heartbeat=self._heartbeat_monitor is not None,
            )

            system_prompt_append = (
                "You are a supervisor — the orchestrator's direct interface to the human operator. "
                "You have FULL authority: read/write files, run bash commands, manage Tracker issues, "
                "query GitHub, access stats and memory. Do whatever is needed to answer questions "
                "and execute the operator's instructions. Be concise but thorough." + memory_context
            )

            options = ClaudeAgentOptions(
                model=self._config.supervisor_model,
                system_prompt={
                    "type": "preset",
                    "preset": "claude_code",
                    "append": system_prompt_append,
                },
                mcp_servers={SUPERVISOR_CHAT_MCP_SERVER_NAME: supervisor_server},
                allowed_tools=allowed_tools,
                cwd=self._config.workspace_dir,
                permission_mode="bypassPermissions",
                env=self._config.agent_env,
            )

            client = ClaudeSDKClient(options=options)
            try:
                await client.__aenter__()
            except Exception:
                # Clean up stale state if __aenter__ fails
                raise

            # Assign all state atomically at the end, inside the lock
            self._client = client
            self._session_id = session_id
            self._created_at = created_at

            return ChatSessionInfo(
                session_id=session_id,
                created_at=created_at,
                message_count=0,
                generating=False,
            )

    def get_session_info(self) -> ChatSessionInfo | None:
        """Return metadata about the current session, or None if no session."""
        if self._session_id is None or self._created_at is None:
            return None
        return ChatSessionInfo(
            session_id=self._session_id,
            created_at=self._created_at,
            message_count=len(self._history),
            generating=self._is_generating,
        )

    def get_history(self) -> list[ChatMessage]:
        """Return a copy of the message history."""
        return list(self._history)

    async def send(self, text: str) -> None:
        """Send a user message and stream the response via event bus.

        Raises ValueError if no session, if already generating, or if text is empty.
        """
        if not text.strip():
            raise ValueError("Message text must not be empty")

        # Capture client and session_id atomically to prevent race with close()
        async with self._create_lock:
            if self._client is None or self._session_id is None:
                raise ValueError("No active session — call create_session() first")
            if self._is_generating:
                raise ValueError("already generating — wait or call abort()")

            # Capture references while holding the lock
            client = self._client
            session_id = self._session_id

            self._is_generating = True
            self._idle_event.clear()
            self._generation_task = asyncio.current_task()

            # Add user message to history
            self._history.append(ChatMessage(role="user", content=text))

        # Release lock before publishing events and making SDK calls
        # Use captured references instead of self._client / self._session_id
        # Publish user event
        await self._event_bus.publish(
            Event(
                type=EventType.SUPERVISOR_CHAT_USER,
                task_key=self._channel_key,
                data={"text": text, "session_id": session_id},
            )
        )

        collected_texts: list[str] = []
        try:
            await client.query(text)
            async for message in receive_response_safe(client):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ThinkingBlock):
                            await self._event_bus.publish(
                                Event(
                                    type=EventType.SUPERVISOR_CHAT_THINKING,
                                    task_key=self._channel_key,
                                    data={"thinking": block.thinking, "session_id": session_id},
                                )
                            )
                        elif isinstance(block, ToolUseBlock):
                            await self._event_bus.publish(
                                Event(
                                    type=EventType.SUPERVISOR_CHAT_TOOL_USE,
                                    task_key=self._channel_key,
                                    data={"tool": block.name, "input": block.input, "session_id": session_id},
                                )
                            )
                        elif isinstance(block, TextBlock):
                            collected_texts.append(block.text)
                            await self._event_bus.publish(
                                Event(
                                    type=EventType.SUPERVISOR_CHAT_CHUNK,
                                    task_key=self._channel_key,
                                    data={"text": block.text, "session_id": session_id},
                                )
                            )

            # Add assistant message to history
            assistant_text = "".join(collected_texts) if collected_texts else ""
            if assistant_text:
                self._history.append(ChatMessage(role="assistant", content=assistant_text))

            # Publish done event
            await self._event_bus.publish(
                Event(
                    type=EventType.SUPERVISOR_CHAT_DONE,
                    task_key=self._channel_key,
                    data={"session_id": session_id},
                )
            )

        except asyncio.CancelledError:
            # Aborted by user — re-raise without publishing error
            raise
        except Exception as e:
            logger.exception("Supervisor chat generation error")
            await self._event_bus.publish(
                Event(
                    type=EventType.SUPERVISOR_CHAT_ERROR,
                    task_key=self._channel_key,
                    data={"error": str(e), "session_id": session_id},
                )
            )
        finally:
            self._is_generating = False
            self._generation_task = None
            self._idle_event.set()

            # Publish SUPERVISOR_TASK_CREATED events for any tasks created during this run
            if self._created_task_keys:
                for task_key in self._created_task_keys:
                    await self._event_bus.publish(
                        Event(
                            type=EventType.SUPERVISOR_TASK_CREATED,
                            task_key=self._channel_key,
                            data={"task_key": task_key},
                        )
                    )
                self._created_task_keys = []

    async def auto_send(self, text: str) -> None:
        """Send an automated system message, creating session if needed.

        Waits for any in-progress generation to complete before sending.
        Serializes concurrent auto_send calls via a dedicated lock.
        Used for autonomous system events (e.g., epic planning).
        """
        async with self._auto_send_lock:
            if self._client is None:
                await self.create_session()
            await self._idle_event.wait()
            await self.send(text)

    async def abort(self) -> bool:
        """Cancel the current generation if running.

        Returns True if generation was cancelled, False if not generating.
        """
        # Atomically check and capture references under lock to prevent
        # TOCTOU race with send()'s finally block clearing _generation_task.
        async with self._create_lock:
            if not self._is_generating or self._generation_task is None:
                return False
            task = self._generation_task
            client = self._client

        # Interrupt the SDK client to stop the in-flight request
        # Use try/except to ensure task.cancel() is always called even if interrupt() fails
        if client is not None:
            try:
                await client.interrupt()
            except Exception:
                pass

        task.cancel()
        return True

    async def _close_unlocked(self) -> None:
        """Internal close implementation without locking (for use within locked sections)."""
        # Capture task reference before await to prevent race with send() completion
        task = self._generation_task

        if task is not None and not task.done():
            # Interrupt the SDK client before cancelling the task
            if self._client is not None:
                try:
                    await self._client.interrupt()
                except Exception:
                    logger.debug("Error interrupting SDK client during close")

            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                logger.warning("Error closing supervisor chat session")

        self._client = None
        self._session_id = None
        self._created_at = None
        self._history = []
        self._is_generating = False
        self._generation_task = None

    async def close(self) -> None:
        """Close the current session and clean up resources."""
        async with self._create_lock:
            await self._close_unlocked()
