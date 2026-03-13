"""Inter-agent communication mailbox for coordinating concurrent worker agents."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.event_bus import EventBus

logger = logging.getLogger(__name__)


class MessageStatus(StrEnum):
    """Status of an inter-agent message."""

    PENDING = "pending"
    READ = "read"
    REPLIED = "replied"
    EXPIRED = "expired"


class MessageType(StrEnum):
    """Semantic type of an inter-agent message."""

    REQUEST = "request"  # Sender expects a reply_to_message
    RESPONSE = "response"  # Reply to a REQUEST
    NOTIFICATION = "notification"  # Informational, no reply expected
    ARTIFACT = "artifact"  # Data transfer (JSON, code, plan)


class DeliveryStatus(StrEnum):
    """Delivery status of an inter-agent message."""

    DELIVERED = "delivered"  # Interrupt succeeded (live session received)
    QUEUED = "queued"  # In inbox, interrupt failed (fallback)
    OVERFLOW_DROPPED = "dropped"  # Evicted due to inbox overflow


@dataclass
class AgentMessage:
    """A message exchanged between worker agents."""

    id: str
    sender_task_key: str
    sender_summary: str
    target_task_key: str
    text: str
    msg_type: MessageType = MessageType.NOTIFICATION
    status: MessageStatus = MessageStatus.PENDING
    delivery_status: DeliveryStatus = DeliveryStatus.QUEUED
    reply_text: str | None = None
    created_at: float = field(default_factory=time.monotonic, repr=False)
    _reply_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass
class AgentInfo:
    """Information about a running agent for discovery."""

    task_key: str
    task_summary: str
    status: str
    component: str | None = None  # e.g. "Бекенд", "Фронтенд", "DevOps"
    repo: str | None = None  # e.g. "org/repo-name"


# Callback type aliases
ListAgentsCallback = Callable[[], Awaitable[list[AgentInfo]]]
InterruptAgentCallback = Callable[[str, str], Awaitable[None]]


class AgentMailbox:
    """Centralized mailbox for inter-agent communication.

    Manages message inboxes per agent, supports send/receive/reply with
    interrupt-based delivery. Owned by Orchestrator, passed to tool builders.
    """

    MAX_INBOX_SIZE = 50

    def _inbox_append(
        self,
        inbox: deque[AgentMessage],
        msg: AgentMessage,
    ) -> None:
        """Append a message to an inbox, evicting the oldest on overflow."""
        if len(inbox) >= self.MAX_INBOX_SIZE:
            oldest = inbox.popleft()
            if oldest.status in (
                MessageStatus.PENDING,
                MessageStatus.READ,
            ):
                oldest.status = MessageStatus.EXPIRED
            # Only overwrite delivery_status if the message was never delivered.
            # A DELIVERED message that is later evicted stays DELIVERED — the
            # interrupt already reached the target; the eviction only affects
            # inbox tracking.
            if oldest.delivery_status != DeliveryStatus.DELIVERED:
                oldest.delivery_status = DeliveryStatus.OVERFLOW_DROPPED
                self._stats["messages_overflow_dropped"] += 1
            self._messages.pop(oldest.id, None)
            logger.warning(
                "Mailbox overflow: evicted message %s from %s",
                oldest.id,
                oldest.target_task_key,
            )
        inbox.append(msg)

    def __init__(self) -> None:
        self._inboxes: dict[str, deque[AgentMessage]] = {}
        self._messages: dict[str, AgentMessage] = {}
        self._registered: set[str] = set()
        # Invariant: _agent_metadata keys == _registered keys.
        # register_agent() populates both; unregister_agent() removes from both.
        self._agent_metadata: dict[str, dict[str, str | None]] = {}
        self._list_agents_cb: ListAgentsCallback | None = None
        self._interrupt_agent_cb: InterruptAgentCallback | None = None
        self._event_bus: EventBus | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._agent_locks: dict[str, asyncio.Lock] = {}  # Per-agent locks for delivery/unregister coordination
        self._stats: dict[str, int] = {
            "messages_sent": 0,
            "messages_delivered": 0,
            "messages_queued": 0,
            "messages_overflow_dropped": 0,
            "messages_replied": 0,
            "messages_expired": 0,
        }

    def _get_agent_lock(self, task_key: str) -> asyncio.Lock:
        """Return the per-agent lock, creating it if needed.

        Per-agent locks prevent race conditions between concurrent message
        delivery (send_message, reply_to_message) and unregister_agent.
        """
        lock = self._agent_locks.get(task_key)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[task_key] = lock
        return lock

    def set_event_bus(self, event_bus: EventBus) -> None:
        """Set event bus for publishing message events."""
        self._event_bus = event_bus

    def set_callbacks(
        self,
        *,
        list_agents: ListAgentsCallback,
        interrupt_agent: InterruptAgentCallback,
    ) -> None:
        """Set orchestrator-level callbacks for agent discovery and interrupts."""
        self._list_agents_cb = list_agents
        self._interrupt_agent_cb = interrupt_agent

    def register_agent(
        self,
        task_key: str,
        component: str | None = None,
        repo: str | None = None,
    ) -> None:
        """Register an agent as active. Creates an inbox for it.

        Args:
            task_key: Unique task identifier.
            component: Optional component label (e.g. "Бекенд", "Фронтенд").
            repo: Optional repository name (e.g. "org/repo-name").
        """
        self._registered.add(task_key)
        self._agent_metadata[task_key] = {"component": component, "repo": repo}
        if task_key not in self._inboxes:
            self._inboxes[task_key] = deque()

    def is_registered(self, task_key: str) -> bool:
        """Check if an agent is currently registered."""
        return task_key in self._registered

    def get_agent_metadata(self, task_key: str) -> dict[str, str | None]:
        """Return a copy of metadata for a registered agent (component, repo).

        Returns an empty dict if the agent has no metadata recorded.
        """
        return dict(self._agent_metadata.get(task_key, {}))

    async def unregister_agent(self, task_key: str) -> None:
        """Unregister an agent. Expires pending/read messages and purges all from lookup.

        Acquires the per-agent lock to ensure concurrent message delivery
        completes before the agent is unregistered.
        """
        async with self._get_agent_lock(task_key):
            self._registered.discard(task_key)
            self._agent_metadata.pop(task_key, None)
            inbox = self._inboxes.pop(task_key, None)
            if inbox:
                for msg in inbox:
                    if msg.status in (MessageStatus.PENDING, MessageStatus.READ):
                        msg.status = MessageStatus.EXPIRED
                        self._stats["messages_expired"] += 1
                    # Purge all messages from lookup to prevent unbounded growth
                    self._messages.pop(msg.id, None)
        # Clean up lock AFTER releasing to prevent race with waiters
        self._agent_locks.pop(task_key, None)

    async def list_agents(self) -> list[AgentInfo]:
        """List all running agents. Delegates to orchestrator callback."""
        if self._list_agents_cb is None:
            return []
        return await self._list_agents_cb()

    async def send_message(
        self,
        sender_key: str,
        sender_summary: str,
        target_key: str,
        text: str,
        msg_type: MessageType = MessageType.NOTIFICATION,
    ) -> AgentMessage:
        """Send a message from one agent to another.

        Creates the message, adds to target inbox, and interrupts the target.
        If the target is not registered (no live session), the message is
        still created and delivery is attempted via the interrupt callback
        (which can fall back to on-demand session creation).

        Returns the created message (caller can await reply_event if desired).

        Raises:
            ValueError: If sender is not registered or sender sends to self.
        """
        if sender_key == target_key:
            raise ValueError(f"Agent {sender_key} cannot send a message to itself")
        if sender_key not in self._registered:
            raise ValueError(f"Sender agent {sender_key} is not registered")

        msg = AgentMessage(
            id=str(uuid.uuid4()),
            sender_task_key=sender_key,
            sender_summary=sender_summary,
            target_task_key=target_key,
            text=text,
            msg_type=msg_type,
        )
        self._messages[msg.id] = msg
        self._stats["messages_sent"] += 1

        # Acquire per-agent lock to prevent race with unregister_agent
        async with self._get_agent_lock(target_key):
            # If target has an inbox, queue message there; otherwise
            # create a temporary inbox so the message is tracked.
            inbox = self._inboxes.get(target_key)
            if inbox is None:
                self.cleanup_orphan_inboxes()
                inbox = deque()
                self._inboxes[target_key] = inbox
            self._inbox_append(inbox, msg)

            # Publish event
            if self._event_bus is not None:
                from orchestrator.constants import EventType
                from orchestrator.event_bus import Event

                self._publish_event(
                    Event(
                        type=EventType.AGENT_MESSAGE_SENT,
                        task_key=sender_key,
                        data={"target": target_key, "message_id": msg.id},
                    )
                )

            # Interrupt target agent to deliver the message
            if self._interrupt_agent_cb is not None:
                try:
                    notification = (
                        f"[Inter-Agent Message from {sender_key} ({sender_summary})]\n"
                        f"Message ID: {msg.id}\n"
                        f"Type: {msg_type}\n\n"
                        f"{text}\n\n"
                        f"Use `reply_to_message` to respond, or `check_messages` to see all unread."
                    )
                    await self._interrupt_agent_cb(target_key, notification)
                    msg.delivery_status = DeliveryStatus.DELIVERED
                    self._stats["messages_delivered"] += 1
                    logger.debug(
                        "Delivered message %s from %s to %s via interrupt",
                        msg.id,
                        sender_key,
                        target_key,
                    )
                except Exception:
                    msg.delivery_status = DeliveryStatus.QUEUED
                    self._stats["messages_queued"] += 1
                    logger.warning(
                        "Failed to interrupt agent %s for message delivery; message queued",
                        target_key,
                    )
            else:
                msg.delivery_status = DeliveryStatus.QUEUED
                self._stats["messages_queued"] += 1

        return msg

    async def request_and_wait(
        self,
        sender_key: str,
        sender_summary: str,
        target_key: str,
        text: str,
        wait_timeout: float = 60.0,
    ) -> str | None:
        """Send a REQUEST message and block until reply or timeout.

        Args:
            sender_key: Task key of the sending agent.
            sender_summary: Short description of the sending agent.
            target_key: Task key of the target agent.
            text: Message text.
            wait_timeout: Seconds to wait for a reply before returning None.

        Returns:
            Reply text if received within timeout, None on timeout.
        """
        msg = await self.send_message(
            sender_key,
            sender_summary,
            target_key,
            text,
            msg_type=MessageType.REQUEST,
        )
        try:
            async with asyncio.timeout(wait_timeout):
                await msg._reply_event.wait()
            return msg.reply_text
        except TimeoutError:
            # Mark as expired so the target gets a clean error if it tries
            # to reply after the sender has already given up.
            msg.status = MessageStatus.EXPIRED
            self._stats["messages_expired"] += 1
            return None

    def get_unread_messages(self, task_key: str) -> list[AgentMessage]:
        """Get unread (pending) messages for an agent, marking them as read."""
        inbox = self._inboxes.get(task_key)
        if not inbox:
            return []

        unread = [m for m in inbox if m.status == MessageStatus.PENDING]
        for m in unread:
            m.status = MessageStatus.READ
        return unread

    async def reply_to_message(
        self,
        message_id: str,
        reply_text: str,
        replier_key: str,
    ) -> None:
        """Reply to a message. Interrupts the original sender.

        Raises:
            ValueError: If message not found, expired, or replier is not the target.
        """
        msg = self._messages.get(message_id)
        if msg is None:
            raise ValueError(f"Message {message_id} not found")
        if msg.status not in (MessageStatus.PENDING, MessageStatus.READ):
            raise ValueError(f"Message {message_id} cannot be replied to (status: {msg.status})")
        if msg.target_task_key != replier_key:
            raise ValueError(f"Only the target agent ({msg.target_task_key}) can reply, not {replier_key}")

        msg.reply_text = reply_text
        msg.status = MessageStatus.REPLIED
        msg._reply_event.set()
        self._stats["messages_replied"] += 1

        # Acquire per-agent lock on sender to prevent race with unregister_agent
        sender_key = msg.sender_task_key
        async with self._get_agent_lock(sender_key):
            # Add synthetic PENDING message to sender's inbox as fallback.
            # If interrupt delivery works, sender sees the reply twice (harmless).
            # If interrupt is missed, check_messages will find it.
            # NOTE: synthetic RESPONSE messages are intentionally NOT counted in
            # messages_sent — they are a delivery fallback, not a first-class send.
            # Only messages_replied (incremented above) tracks this operation.
            sender_inbox = self._inboxes.get(sender_key)
            if sender_inbox is not None:
                reply_msg = AgentMessage(
                    id=str(uuid.uuid4()),
                    sender_task_key=msg.target_task_key,
                    sender_summary=f"Reply to {msg.id[:8]}",
                    target_task_key=sender_key,
                    text=(f"[Reply from {msg.target_task_key}]\nOriginal: {msg.text}\n\nReply: {reply_text}"),
                    msg_type=MessageType.RESPONSE,
                )
                self._messages[reply_msg.id] = reply_msg
                self._inbox_append(sender_inbox, reply_msg)

            # Publish event
            if self._event_bus is not None:
                from orchestrator.constants import EventType
                from orchestrator.event_bus import Event

                self._publish_event(
                    Event(
                        type=EventType.AGENT_MESSAGE_REPLIED,
                        task_key=replier_key,
                        data={"message_id": message_id, "sender": sender_key},
                    )
                )

            # Interrupt the original sender to deliver the reply
            if self._interrupt_agent_cb is not None:
                try:
                    notification = f"[Reply from {msg.target_task_key} to your message]\nOriginal: {msg.text}\n\nReply: {reply_text}"
                    await self._interrupt_agent_cb(sender_key, notification)
                except Exception:
                    logger.warning(
                        "Failed to interrupt agent %s for reply delivery",
                        sender_key,
                    )

    def _publish_event(self, event: object) -> None:
        """Publish an event to the event bus, logging errors instead of swallowing them."""
        if self._event_bus is None:
            raise RuntimeError("_publish_event called but event_bus is not set")
        task = asyncio.create_task(self._event_bus.publish(event))  # type: ignore[arg-type]
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(_handle_event_task_error)

    def cleanup_orphan_inboxes(self) -> int:
        """Remove inboxes for unregistered agents with no actionable messages.

        An orphan inbox is one whose target_key was never registered (or
        was unregistered) and all messages are in terminal state
        (replied / expired).  Returns the number of inboxes cleaned up.
        """
        terminal = (MessageStatus.REPLIED, MessageStatus.EXPIRED)
        orphan_keys: list[str] = []
        for key, inbox in self._inboxes.items():
            if key in self._registered:
                continue
            if all(m.status in terminal for m in inbox):
                orphan_keys.append(key)

        for key in orphan_keys:
            inbox = self._inboxes.pop(key)
            for msg in inbox:
                self._messages.pop(msg.id, None)

        if orphan_keys:
            logger.debug(
                "Cleaned up %d orphan inboxes: %s",
                len(orphan_keys),
                orphan_keys,
            )
        return len(orphan_keys)

    def get_all_messages(self, task_key: str | None = None) -> list[AgentMessage]:
        """Get all messages, optionally filtered by sender or target task_key.

        Used by supervisor for read-only monitoring.
        """
        all_msgs = list(self._messages.values())
        if task_key is not None:
            all_msgs = [m for m in all_msgs if task_key in (m.sender_task_key, m.target_task_key)]
        return all_msgs

    def get_stats(self) -> dict[str, int]:
        """Return a snapshot of inter-agent communication statistics."""
        return dict(self._stats)

    def cleanup_terminal_messages(self, max_age_seconds: float = 3600.0) -> int:
        """Remove old terminal messages (REPLIED/EXPIRED) from the global lookup.

        Only touches the ``_messages`` dict — does NOT remove items from any
        agent's inbox deque.  Inboxes are managed by register/unregister.

        Args:
            max_age_seconds: Messages older than this (by ``created_at``) are
                eligible for removal.  Defaults to 1 hour.

        Returns:
            Number of messages removed.
        """
        now = time.monotonic()
        terminal = (MessageStatus.REPLIED, MessageStatus.EXPIRED)
        to_remove = [
            msg_id
            for msg_id, msg in self._messages.items()
            if msg.status in terminal and (now - msg.created_at) >= max_age_seconds
        ]
        for msg_id in to_remove:
            del self._messages[msg_id]
        return len(to_remove)


def _handle_event_task_error(task: asyncio.Task[None]) -> None:
    """Log errors from fire-and-forget event publish tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Failed to publish mailbox event: %s", exc)
