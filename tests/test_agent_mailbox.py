"""Tests for inter-agent communication mailbox."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.agent_mailbox import (
    AgentInfo,
    AgentMailbox,
    AgentMessage,
    DeliveryStatus,
    MessageStatus,
    MessageType,
)


class InterruptTracker:
    """Tracks interrupt calls for test assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, task_key: str, message: str) -> None:
        self.calls.append((task_key, message))


@pytest.fixture
def interrupt_tracker() -> InterruptTracker:
    return InterruptTracker()


@pytest.fixture
def mailbox(
    interrupt_tracker: InterruptTracker,
) -> AgentMailbox:
    """Create a mailbox with mock callbacks (no agents registered)."""
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

    mb.set_callbacks(
        list_agents=list_agents,
        interrupt_agent=interrupt_tracker,
    )
    return mb


@pytest.fixture
def two_agents(mailbox: AgentMailbox) -> AgentMailbox:
    """Mailbox with QR-1 and QR-2 registered."""
    mailbox.register_agent("QR-1")
    mailbox.register_agent("QR-2")
    return mailbox


class TestMessageLifecycle:
    @pytest.mark.asyncio
    async def test_send_and_receive(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend API", "QR-2", "What endpoint?")

        assert msg.sender_task_key == "QR-1"
        assert msg.target_task_key == "QR-2"
        assert msg.text == "What endpoint?"
        assert msg.status == MessageStatus.PENDING

        unread = two_agents.get_unread_messages("QR-2")
        assert len(unread) == 1
        assert unread[0].id == msg.id
        assert unread[0].status == MessageStatus.READ

        # Second call returns empty (already read)
        assert two_agents.get_unread_messages("QR-2") == []

    @pytest.mark.asyncio
    async def test_reply_to_message(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "What endpoint?")
        two_agents.get_unread_messages("QR-2")

        await two_agents.reply_to_message(msg.id, "POST /api/v1/auth", "QR-2")

        assert msg.status == MessageStatus.REPLIED
        assert msg.reply_text == "POST /api/v1/auth"

    @pytest.mark.asyncio
    async def test_reply_sets_event(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Question?")
        two_agents.get_unread_messages("QR-2")

        assert not msg._reply_event.is_set()

        await two_agents.reply_to_message(msg.id, "Answer", "QR-2")

        assert msg._reply_event.is_set()

    @pytest.mark.asyncio
    async def test_send_interrupts_target(
        self,
        two_agents: AgentMailbox,
        interrupt_tracker: InterruptTracker,
    ) -> None:
        await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")

        assert len(interrupt_tracker.calls) == 1
        assert interrupt_tracker.calls[0][0] == "QR-2"

    @pytest.mark.asyncio
    async def test_reply_interrupts_sender(
        self,
        two_agents: AgentMailbox,
        interrupt_tracker: InterruptTracker,
    ) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Question?")
        two_agents.get_unread_messages("QR-2")

        interrupt_tracker.calls.clear()

        await two_agents.reply_to_message(msg.id, "Answer", "QR-2")

        assert len(interrupt_tracker.calls) == 1
        assert interrupt_tracker.calls[0][0] == "QR-1"


class TestInterruptFailure:
    @pytest.mark.asyncio
    async def test_message_stays_pending_when_interrupt_fails(
        self,
    ) -> None:
        mb = AgentMailbox()

        async def list_agents() -> list[AgentInfo]:
            return []

        async def failing_interrupt(task_key: str, message: str) -> None:
            raise RuntimeError("connection lost")

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=failing_interrupt,
        )
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        msg = await mb.send_message("QR-1", "Backend", "QR-2", "Hello?")

        assert msg.status == MessageStatus.PENDING

        unread = mb.get_unread_messages("QR-2")
        assert len(unread) == 1
        assert unread[0].id == msg.id


class TestUnregister:
    @pytest.mark.parametrize(
        "read_before_unregister",
        [False, True],
        ids=["pending", "read"],
    )
    @pytest.mark.asyncio
    async def test_unregister_expires_messages(
        self,
        two_agents: AgentMailbox,
        read_before_unregister: bool,
    ) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello?")
        if read_before_unregister:
            two_agents.get_unread_messages("QR-2")
        await two_agents.unregister_agent("QR-2")

        assert msg.status == MessageStatus.EXPIRED


class TestInboxOverflow:
    @pytest.mark.asyncio
    async def test_overflow_drops_oldest(self, two_agents: AgentMailbox) -> None:
        messages: list[AgentMessage] = []
        for i in range(AgentMailbox.MAX_INBOX_SIZE + 1):
            msg = await two_agents.send_message("QR-1", "Backend", "QR-2", f"Message {i}")
            messages.append(msg)

        assert messages[0].status == MessageStatus.EXPIRED
        assert messages[-1].status == MessageStatus.PENDING

        unread = two_agents.get_unread_messages("QR-2")
        assert len(unread) == AgentMailbox.MAX_INBOX_SIZE


class TestReplyErrors:
    @pytest.mark.asyncio
    async def test_reply_to_expired_message(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello?")
        await two_agents.unregister_agent("QR-2")

        with pytest.raises(ValueError, match="not found"):
            await two_agents.reply_to_message(msg.id, "Reply", "QR-2")

    @pytest.mark.asyncio
    async def test_reply_by_wrong_agent(self, two_agents: AgentMailbox) -> None:
        two_agents.register_agent("QR-3")

        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello?")
        two_agents.get_unread_messages("QR-2")

        with pytest.raises(ValueError, match="Only the target"):
            await two_agents.reply_to_message(msg.id, "Reply", "QR-3")

    @pytest.mark.asyncio
    async def test_reply_to_already_replied_message(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello?")
        two_agents.get_unread_messages("QR-2")
        await two_agents.reply_to_message(msg.id, "First reply", "QR-2")

        with pytest.raises(ValueError, match="cannot be replied to"):
            await two_agents.reply_to_message(msg.id, "Second reply", "QR-2")

    @pytest.mark.asyncio
    async def test_reply_to_nonexistent_message(self, mailbox: AgentMailbox) -> None:
        with pytest.raises(ValueError, match="not found"):
            await mailbox.reply_to_message("nonexistent-id", "Reply", "QR-1")


class TestGetAllMessages:
    @pytest.mark.asyncio
    async def test_all_messages_no_filter(self, two_agents: AgentMailbox) -> None:
        await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        await two_agents.send_message("QR-2", "Frontend", "QR-1", "Hi back")

        all_msgs = two_agents.get_all_messages()
        assert len(all_msgs) == 2

    @pytest.mark.asyncio
    async def test_all_messages_with_filter(self, two_agents: AgentMailbox) -> None:
        two_agents.register_agent("QR-3")

        await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        await two_agents.send_message("QR-3", "DevOps", "QR-2", "Hi there")
        await two_agents.send_message("QR-2", "Frontend", "QR-1", "Reply")

        filtered = two_agents.get_all_messages(task_key="QR-1")
        assert len(filtered) == 2

    @pytest.mark.asyncio
    async def test_all_messages_empty(self, mailbox: AgentMailbox) -> None:
        assert mailbox.get_all_messages() == []


class TestListAgents:
    @pytest.mark.asyncio
    async def test_list_agents(self, mailbox: AgentMailbox) -> None:
        agents = await mailbox.list_agents()
        assert len(agents) == 2
        assert agents[0].task_key == "QR-1"

    @pytest.mark.asyncio
    async def test_list_agents_without_callbacks(
        self,
    ) -> None:
        mb = AgentMailbox()
        agents = await mb.list_agents()
        assert agents == []


class TestSendErrors:
    @pytest.mark.asyncio
    async def test_send_to_unregistered_creates_inbox(self, mailbox: AgentMailbox) -> None:
        mailbox.register_agent("QR-1")

        msg = await mailbox.send_message("QR-1", "Backend", "QR-99", "Hello")
        assert msg.target_task_key == "QR-99"
        assert "QR-99" in mailbox._inboxes
        assert len(mailbox._inboxes["QR-99"]) == 1

    @pytest.mark.asyncio
    async def test_send_to_self(self, mailbox: AgentMailbox) -> None:
        mailbox.register_agent("QR-1")

        with pytest.raises(ValueError, match=r"cannot send.*to itself"):
            await mailbox.send_message("QR-1", "Backend", "QR-1", "Hello me")

    @pytest.mark.asyncio
    async def test_send_from_unregistered_sender(self, mailbox: AgentMailbox) -> None:
        mailbox.register_agent("QR-2")

        with pytest.raises(ValueError, match="not registered"):
            await mailbox.send_message("QR-1", "Backend", "QR-2", "Ghost message")


class TestMessageCleanup:
    @pytest.mark.parametrize(
        "reply_before_unregister",
        [False, True],
        ids=["expired_pending", "replied_then_expired"],
    )
    @pytest.mark.asyncio
    async def test_unregister_purges_from_messages(
        self,
        two_agents: AgentMailbox,
        reply_before_unregister: bool,
    ) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello?")
        msg_id = msg.id
        if reply_before_unregister:
            two_agents.get_unread_messages("QR-2")
            await two_agents.reply_to_message(msg.id, "Answer", "QR-2")
        await two_agents.unregister_agent("QR-2")

        assert msg_id not in two_agents._messages

    @pytest.mark.asyncio
    async def test_overflow_purges_expired_from_messages(self, two_agents: AgentMailbox) -> None:
        first_msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Message 0")
        first_id = first_msg.id
        for i in range(1, AgentMailbox.MAX_INBOX_SIZE):
            await two_agents.send_message("QR-1", "Backend", "QR-2", f"Message {i}")

        assert first_id in two_agents._messages

        # Overflow trigger
        await two_agents.send_message("QR-1", "Backend", "QR-2", "Overflow trigger")

        assert first_id not in two_agents._messages


class TestEventPublishing:
    @pytest.mark.asyncio
    async def test_send_publishes_event(self, two_agents: AgentMailbox) -> None:
        import asyncio

        from orchestrator.constants import EventType
        from orchestrator.event_bus import EventBus

        bus = EventBus()
        queue = bus.subscribe_global()
        two_agents.set_event_bus(bus)

        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello!")
        await asyncio.sleep(0)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

        sent_events = [e for e in events if e.type == EventType.AGENT_MESSAGE_SENT]
        assert len(sent_events) == 1
        assert sent_events[0].task_key == "QR-1"
        assert sent_events[0].data["target"] == "QR-2"
        assert sent_events[0].data["message_id"] == msg.id

    @pytest.mark.asyncio
    async def test_reply_publishes_event(self, two_agents: AgentMailbox) -> None:
        import asyncio

        from orchestrator.constants import EventType
        from orchestrator.event_bus import EventBus

        bus = EventBus()
        queue = bus.subscribe_global()
        two_agents.set_event_bus(bus)

        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Question?")
        two_agents.get_unread_messages("QR-2")
        await two_agents.reply_to_message(msg.id, "Answer!", "QR-2")
        await asyncio.sleep(0)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

        replied_events = [e for e in events if e.type == EventType.AGENT_MESSAGE_REPLIED]
        assert len(replied_events) == 1
        assert replied_events[0].task_key == "QR-2"
        assert replied_events[0].data["message_id"] == msg.id

    @pytest.mark.asyncio
    async def test_no_event_bus_no_error(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        two_agents.get_unread_messages("QR-2")
        await two_agents.reply_to_message(msg.id, "Reply", "QR-2")


class TestSendToUnregisteredDuringRace:
    @pytest.mark.asyncio
    async def test_send_to_concurrently_unregistered_agent(self, two_agents: AgentMailbox) -> None:
        await two_agents.unregister_agent("QR-2")

        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        assert msg.target_task_key == "QR-2"
        assert "QR-2" in two_agents._inboxes


class TestOrphanInboxCleanup:
    @pytest.mark.asyncio
    async def test_removes_fully_expired_orphan(self, mailbox: AgentMailbox) -> None:
        mailbox.register_agent("QR-1")
        msg = await mailbox.send_message("QR-1", "Backend", "QR-99", "Hello")
        msg.status = MessageStatus.EXPIRED

        assert "QR-99" in mailbox._inboxes
        removed = mailbox.cleanup_orphan_inboxes()
        assert removed == 1
        assert "QR-99" not in mailbox._inboxes
        assert msg.id not in mailbox._messages

    @pytest.mark.asyncio
    async def test_keeps_inbox_with_pending_messages(self, mailbox: AgentMailbox) -> None:
        mailbox.register_agent("QR-1")
        await mailbox.send_message("QR-1", "Backend", "QR-99", "Hello")

        removed = mailbox.cleanup_orphan_inboxes()
        assert removed == 0
        assert "QR-99" in mailbox._inboxes

    @pytest.mark.asyncio
    async def test_keeps_registered_inbox(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        msg.status = MessageStatus.EXPIRED

        removed = two_agents.cleanup_orphan_inboxes()
        assert removed == 0
        assert "QR-2" in two_agents._inboxes

    @pytest.mark.asyncio
    async def test_send_to_new_target_triggers_cleanup(self, mailbox: AgentMailbox) -> None:
        mailbox.register_agent("QR-1")

        msg1 = await mailbox.send_message("QR-1", "Backend", "QR-90", "old")
        msg1.status = MessageStatus.EXPIRED

        await mailbox.send_message("QR-1", "Backend", "QR-91", "new")

        assert "QR-90" not in mailbox._inboxes
        assert "QR-91" in mailbox._inboxes


class TestReplyInboxFallback:
    @pytest.mark.asyncio
    async def test_reply_adds_message_to_sender_inbox(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "What endpoint?")
        two_agents.get_unread_messages("QR-2")
        await two_agents.reply_to_message(msg.id, "POST /api/v1/auth", "QR-2")

        unread = two_agents.get_unread_messages("QR-1")
        assert len(unread) == 1
        assert "POST /api/v1/auth" in unread[0].text
        assert unread[0].status == MessageStatus.READ

    @pytest.mark.asyncio
    async def test_reply_no_crash_when_sender_unregistered(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Question?")
        two_agents.get_unread_messages("QR-2")

        await two_agents.unregister_agent("QR-1")

        await two_agents.reply_to_message(msg.id, "Answer", "QR-2")
        assert msg.status == MessageStatus.REPLIED
        assert msg.reply_text == "Answer"

    @pytest.mark.asyncio
    async def test_reply_inbox_respects_overflow(self, two_agents: AgentMailbox) -> None:
        # Fill QR-1's inbox to MAX
        for i in range(AgentMailbox.MAX_INBOX_SIZE):
            await two_agents.send_message("QR-2", "Frontend", "QR-1", f"Spam {i}")

        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Important question")
        two_agents.get_unread_messages("QR-2")
        await two_agents.reply_to_message(msg.id, "Important answer", "QR-2")

        unread = two_agents.get_unread_messages("QR-1")
        reply_msgs = [m for m in unread if "Important answer" in m.text]
        assert len(reply_msgs) == 1


class TestFireAndForgetTaskAnchoring:
    @pytest.mark.asyncio
    async def test_publish_tasks_are_tracked(self, two_agents: AgentMailbox) -> None:
        from orchestrator.event_bus import EventBus

        bus = EventBus()
        two_agents.set_event_bus(bus)

        await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello!")

        assert hasattr(two_agents, "_background_tasks")
        assert isinstance(two_agents._background_tasks, set)

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(two_agents._background_tasks) == 0


class TestMessageType:
    @pytest.mark.asyncio
    async def test_message_defaults_to_notification(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        assert msg.msg_type == MessageType.NOTIFICATION

    @pytest.mark.asyncio
    async def test_send_message_with_request_type(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "What endpoint?", msg_type=MessageType.REQUEST)
        assert msg.msg_type == MessageType.REQUEST

    @pytest.mark.asyncio
    async def test_send_message_with_artifact_type(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", '{"key": "val"}', msg_type=MessageType.ARTIFACT)
        assert msg.msg_type == MessageType.ARTIFACT

    @pytest.mark.asyncio
    async def test_reply_gets_response_type(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Question?")
        two_agents.get_unread_messages("QR-2")

        await two_agents.reply_to_message(msg.id, "Answer", "QR-2")

        # The synthetic reply message in QR-1's inbox should have RESPONSE type
        unread = two_agents.get_unread_messages("QR-1")
        assert len(unread) == 1
        assert unread[0].msg_type == MessageType.RESPONSE


class TestDeliveryStatus:
    @pytest.mark.asyncio
    async def test_delivery_status_delivered_when_interrupt_succeeds(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        assert msg.delivery_status == DeliveryStatus.DELIVERED

    @pytest.mark.asyncio
    async def test_delivery_status_queued_when_interrupt_fails(self) -> None:
        mb = AgentMailbox()

        async def list_agents() -> list[AgentInfo]:
            return []

        async def failing_interrupt(task_key: str, message: str) -> None:
            raise RuntimeError("connection lost")

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=failing_interrupt,
        )
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        msg = await mb.send_message("QR-1", "Backend", "QR-2", "Hello?")
        assert msg.delivery_status == DeliveryStatus.QUEUED

    @pytest.mark.asyncio
    async def test_delivery_status_queued_without_interrupt_callback(self) -> None:
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        msg = await mb.send_message("QR-1", "Backend", "QR-2", "Hello?")
        assert msg.delivery_status == DeliveryStatus.QUEUED

    @pytest.mark.asyncio
    async def test_delivery_status_overflow_dropped(self) -> None:
        # Use a failing interrupt so messages are QUEUED; evicted ones become DROPPED
        mb = AgentMailbox()

        async def failing_interrupt(task_key: str, message: str) -> None:
            raise RuntimeError("interrupt failed")

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(list_agents=list_agents, interrupt_agent=failing_interrupt)
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        messages: list[AgentMessage] = []
        for i in range(AgentMailbox.MAX_INBOX_SIZE + 1):
            msg = await mb.send_message("QR-1", "Backend", "QR-2", f"Message {i}")
            messages.append(msg)

        # First message was QUEUED (interrupt failed) then evicted → OVERFLOW_DROPPED
        assert messages[0].delivery_status == DeliveryStatus.OVERFLOW_DROPPED
        # Last message is still QUEUED (interrupt failed, not yet evicted)
        assert messages[-1].delivery_status == DeliveryStatus.QUEUED

    @pytest.mark.asyncio
    async def test_delivery_status_overflow_preserves_delivered(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        # Messages delivered via interrupt keep DELIVERED even when evicted from inbox
        messages: list[AgentMessage] = []
        for i in range(AgentMailbox.MAX_INBOX_SIZE + 1):
            msg = await two_agents.send_message("QR-1", "Backend", "QR-2", f"Message {i}")
            messages.append(msg)

        # First message was DELIVERED before eviction — status must not be downgraded
        assert messages[0].delivery_status == DeliveryStatus.DELIVERED
        assert messages[-1].delivery_status == DeliveryStatus.DELIVERED


class TestRequestAndWait:
    @pytest.mark.asyncio
    async def test_request_and_wait_returns_reply(self, two_agents: AgentMailbox) -> None:
        async def reply_after_delay() -> None:
            await asyncio.sleep(0.05)
            msgs = two_agents.get_unread_messages("QR-2")
            assert len(msgs) == 1
            await two_agents.reply_to_message(msgs[0].id, "POST /api/v1/auth", "QR-2")

        reply_task = asyncio.create_task(reply_after_delay())
        result = await two_agents.request_and_wait("QR-1", "Backend", "QR-2", "What endpoint?", wait_timeout=2.0)
        await reply_task

        assert result == "POST /api/v1/auth"

    @pytest.mark.asyncio
    async def test_request_and_wait_timeout_returns_none(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        result = await two_agents.request_and_wait("QR-1", "Backend", "QR-2", "What endpoint?", wait_timeout=0.05)
        assert result is None

    @pytest.mark.asyncio
    async def test_request_and_wait_sets_request_type(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        # Let the request time out; message expires but stays in _messages
        await two_agents.request_and_wait("QR-1", "Backend", "QR-2", "Question?", wait_timeout=0.01)
        # get_all_messages is public and doesn't filter by status
        all_msgs = two_agents.get_all_messages("QR-2")
        assert len(all_msgs) == 1
        assert all_msgs[0].msg_type == MessageType.REQUEST
        assert all_msgs[0].status == MessageStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_request_and_wait_timeout_increments_expired_stat(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        await two_agents.request_and_wait("QR-1", "Backend", "QR-2", "Q?", wait_timeout=0.01)
        assert two_agents.get_stats()["messages_expired"] == 1


class TestAgentInfoMetadata:
    def test_agent_info_includes_component_field(self) -> None:
        info = AgentInfo(
            task_key="QR-1",
            task_summary="Backend",
            status="running",
            component="Бекенд",
        )
        assert info.component == "Бекенд"

    def test_agent_info_includes_repo_field(self) -> None:
        info = AgentInfo(
            task_key="QR-1",
            task_summary="Backend",
            status="running",
            repo="org/api-repo",
        )
        assert info.repo == "org/api-repo"

    def test_agent_info_defaults_component_none(self) -> None:
        info = AgentInfo(task_key="QR-1", task_summary="Backend", status="running")
        assert info.component is None
        assert info.repo is None

    def test_register_agent_stores_component(self) -> None:
        mb = AgentMailbox()
        mb.register_agent("QR-1", component="Бекенд", repo="org/backend")
        meta = mb.get_agent_metadata("QR-1")
        assert meta["component"] == "Бекенд"
        assert meta["repo"] == "org/backend"

    @pytest.mark.asyncio
    async def test_unregister_clears_metadata(self) -> None:
        mb = AgentMailbox()
        mb.register_agent("QR-1", component="Бекенд")
        await mb.unregister_agent("QR-1")
        assert mb.get_agent_metadata("QR-1") == {}


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_increment_on_send(self, two_agents: AgentMailbox) -> None:
        await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        assert two_agents.get_stats()["messages_sent"] == 1

    @pytest.mark.asyncio
    async def test_stats_increment_on_delivered(self, two_agents: AgentMailbox) -> None:
        await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        assert two_agents.get_stats()["messages_delivered"] == 1

    @pytest.mark.asyncio
    async def test_stats_increment_on_queued_when_interrupt_fails(self) -> None:
        mb = AgentMailbox()

        async def list_agents() -> list[AgentInfo]:
            return []

        async def failing_interrupt(task_key: str, message: str) -> None:
            raise RuntimeError("fail")

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=failing_interrupt,
        )
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        await mb.send_message("QR-1", "Backend", "QR-2", "Hello")
        stats = mb.get_stats()
        assert stats["messages_sent"] == 1
        assert stats["messages_queued"] == 1
        assert stats["messages_delivered"] == 0

    @pytest.mark.asyncio
    async def test_stats_increment_on_overflow(self) -> None:
        # QUEUED messages that get evicted should increment messages_overflow_dropped
        mb = AgentMailbox()

        async def failing_interrupt(task_key: str, message: str) -> None:
            raise RuntimeError("fail")

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(list_agents=list_agents, interrupt_agent=failing_interrupt)
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        for i in range(AgentMailbox.MAX_INBOX_SIZE + 1):
            await mb.send_message("QR-1", "Backend", "QR-2", f"Msg {i}")
        assert mb.get_stats()["messages_overflow_dropped"] == 1

    @pytest.mark.asyncio
    async def test_stats_overflow_not_counted_for_delivered(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        # DELIVERED messages evicted from the inbox were already received —
        # they must NOT increment messages_overflow_dropped
        for i in range(AgentMailbox.MAX_INBOX_SIZE + 1):
            await two_agents.send_message("QR-1", "Backend", "QR-2", f"Msg {i}")
        assert two_agents.get_stats()["messages_overflow_dropped"] == 0

    @pytest.mark.asyncio
    async def test_stats_increment_on_replied(self, two_agents: AgentMailbox) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Question?")
        two_agents.get_unread_messages("QR-2")
        await two_agents.reply_to_message(msg.id, "Answer", "QR-2")
        assert two_agents.get_stats()["messages_replied"] == 1

    def test_get_stats_returns_copy(self, two_agents: AgentMailbox) -> None:
        stats = two_agents.get_stats()
        stats["messages_sent"] = 999
        assert two_agents.get_stats()["messages_sent"] == 0

    @pytest.mark.asyncio
    async def test_stats_expired_on_unregister(self, two_agents: AgentMailbox) -> None:
        await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello")
        await two_agents.unregister_agent("QR-2")
        assert two_agents.get_stats()["messages_expired"] == 1


class TestCleanupTerminalMessages:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_terminal_messages(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello?")
        two_agents.get_unread_messages("QR-2")
        await two_agents.reply_to_message(msg.id, "Answer", "QR-2")

        assert msg.id in two_agents._messages
        removed = two_agents.cleanup_terminal_messages(max_age_seconds=-1)
        # Removes 1: the original REPLIED message.
        # Synthetic RESPONSE message in QR-1's inbox stays PENDING.
        assert removed == 1
        assert msg.id not in two_agents._messages

    @pytest.mark.asyncio
    async def test_cleanup_keeps_recent_terminal_messages(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello?")
        two_agents.get_unread_messages("QR-2")
        await two_agents.reply_to_message(msg.id, "Answer", "QR-2")

        assert msg.id in two_agents._messages
        removed = two_agents.cleanup_terminal_messages(max_age_seconds=3600)
        assert removed == 0
        assert msg.id in two_agents._messages

    @pytest.mark.asyncio
    async def test_cleanup_does_not_remove_pending_messages(
        self,
        two_agents: AgentMailbox,
    ) -> None:
        msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Hello?")

        removed = two_agents.cleanup_terminal_messages(max_age_seconds=-1)
        assert removed == 0
        assert msg.id in two_agents._messages

    @pytest.mark.asyncio
    async def test_cleanup_returns_count(self, two_agents: AgentMailbox) -> None:
        for _ in range(3):
            msg = await two_agents.send_message("QR-1", "Backend", "QR-2", "Q?")
            two_agents.get_unread_messages("QR-2")
            await two_agents.reply_to_message(msg.id, "A", "QR-2")

        # Only the 3 original messages have REPLIED status; the 3 synthetic
        # response messages in QR-1's inbox still have PENDING status.
        removed = two_agents.cleanup_terminal_messages(max_age_seconds=-1)
        assert removed == 3


class TestConcurrentDeliveryAndUnregister:
    """Tests for race conditions between concurrent message delivery and unregister."""

    @pytest.mark.asyncio
    async def test_unregister_waits_for_in_progress_send(self) -> None:
        """Test that unregister waits for in-progress send to complete fully."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        # Track delivery progress
        interrupt_started = asyncio.Event()
        interrupt_completed = asyncio.Event()

        async def slow_interrupt(task_key: str, message: str) -> None:
            interrupt_started.set()
            await asyncio.sleep(0.05)
            interrupt_completed.set()

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=slow_interrupt,
        )

        # Start send_message - it will yield at the interrupt
        async def send_msg() -> AgentMessage:
            return await mb.send_message("QR-1", "Backend", "QR-2", "Test message")

        send_task = asyncio.create_task(send_msg())

        # Wait for interrupt to start
        await interrupt_started.wait()

        # Now start unregister concurrently - it should wait for send to complete
        async def unregister() -> None:
            await mb.unregister_agent("QR-2")

        unregister_task = asyncio.create_task(unregister())

        # Give unregister a tiny bit of time to try to run
        await asyncio.sleep(0.01)

        # Wait for both operations to complete
        msg, _ = await asyncio.gather(send_task, unregister_task)

        # CRITICAL: Message must be DELIVERED first, THEN EXPIRED by unregister
        # This proves unregister waited for delivery to complete
        assert msg.delivery_status == DeliveryStatus.DELIVERED
        assert msg.status == MessageStatus.EXPIRED
        assert interrupt_completed.is_set()

        # Message should be purged from _messages by unregister
        assert msg.id not in mb._messages

    @pytest.mark.asyncio
    async def test_unregister_waits_for_in_progress_reply(self) -> None:
        """Test that unregister waits for in-progress reply to complete fully."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        interrupt_started = asyncio.Event()
        interrupt_completed = asyncio.Event()

        async def slow_interrupt(task_key: str, message: str) -> None:
            interrupt_started.set()
            await asyncio.sleep(0.05)
            interrupt_completed.set()

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=slow_interrupt,
        )

        # Send initial message
        msg = await mb.send_message("QR-1", "Backend", "QR-2", "Question?")
        mb.get_unread_messages("QR-2")

        interrupt_started.clear()
        interrupt_completed.clear()

        # Start reply - it will yield at the interrupt to sender
        async def reply_msg() -> None:
            await mb.reply_to_message(msg.id, "Answer", "QR-2")

        reply_task = asyncio.create_task(reply_msg())

        # Wait for interrupt to start (this is the reply interrupt to QR-1)
        await interrupt_started.wait()

        # Now unregister sender while reply interrupt is in progress
        async def unregister() -> None:
            await mb.unregister_agent("QR-1")

        unregister_task = asyncio.create_task(unregister())

        # Give unregister time to try to run
        await asyncio.sleep(0.01)

        # Wait for both operations to complete
        await asyncio.gather(reply_task, unregister_task)

        # Reply must have completed fully before unregister
        assert msg.status == MessageStatus.REPLIED
        assert msg.reply_text == "Answer"
        assert msg._reply_event.is_set()
        assert interrupt_completed.is_set()

    @pytest.mark.asyncio
    async def test_concurrent_sends_and_unregister_deterministic(self) -> None:
        """Test multiple concurrent sends with unregister - no orphaned messages."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        async def list_agents() -> list[AgentInfo]:
            return []

        async def fast_interrupt(task_key: str, message: str) -> None:
            await asyncio.sleep(0.005)

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=fast_interrupt,
        )

        # Fire 5 sends and 1 unregister concurrently
        async def send_msg(i: int) -> AgentMessage:
            return await mb.send_message("QR-1", "Backend", "QR-2", f"Message {i}")

        async def unregister() -> None:
            await asyncio.sleep(0.01)  # Let some messages start
            await mb.unregister_agent("QR-2")

        tasks = [send_msg(i) for i in range(5)]
        tasks.append(unregister())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # No exceptions
        for result in results:
            assert not isinstance(result, Exception)

        # All messages should be in valid state
        messages = [r for r in results[:-1] if isinstance(r, AgentMessage)]
        for msg in messages:
            # Must be either DELIVERED then EXPIRED, or just EXPIRED
            if msg.id in mb._messages:
                # Still in _messages means it wasn't purged - shouldn't happen
                # after unregister, but if it is there, it should be EXPIRED
                assert msg.status == MessageStatus.EXPIRED
            else:
                # Purged by unregister - normal
                # Status should be EXPIRED (set by unregister before purge)
                assert msg.status == MessageStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_lock_is_per_agent(self) -> None:
        """Test that locks are per-agent - QR-2 lock doesn't block QR-3 operations."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")
        mb.register_agent("QR-3")

        interrupt_started_qr2 = asyncio.Event()
        interrupt_can_continue_qr2 = asyncio.Event()

        async def slow_interrupt_qr2(task_key: str, message: str) -> None:
            if task_key == "QR-2":
                interrupt_started_qr2.set()
                await interrupt_can_continue_qr2.wait()

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=slow_interrupt_qr2,
        )

        # Start send to QR-2 (will block at interrupt)
        send_qr2_task = asyncio.create_task(mb.send_message("QR-1", "Backend", "QR-2", "To QR-2"))

        # Wait for QR-2 interrupt to start
        await interrupt_started_qr2.wait()

        # Send to QR-3 should NOT be blocked by QR-2's lock
        msg_qr3 = await mb.send_message("QR-1", "Backend", "QR-3", "To QR-3")
        assert msg_qr3.target_task_key == "QR-3"

        # Release QR-2 interrupt
        interrupt_can_continue_qr2.set()
        msg_qr2 = await send_qr2_task
        assert msg_qr2.target_task_key == "QR-2"

    @pytest.mark.asyncio
    async def test_unregister_cleans_up_lock(self) -> None:
        """Test that unregister removes the agent's lock from the locks dict."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(list_agents=list_agents, interrupt_agent=lambda k, m: asyncio.sleep(0))

        # Send a message to trigger lock creation
        await mb.send_message("QR-1", "Backend", "QR-2", "Hello")

        # Lock should exist after send
        assert "QR-2" in mb._agent_locks

        # Unregister should clean up the lock
        await mb.unregister_agent("QR-2")

        # Lock should be removed to prevent memory leaks
        assert "QR-2" not in mb._agent_locks

    @pytest.mark.asyncio
    async def test_unregister_during_send_does_not_lose_message(self) -> None:
        """Test that unregister during send maintains message consistency."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        # Use an event to control when interrupt yields
        interrupt_started = asyncio.Event()
        interrupt_can_continue = asyncio.Event()

        async def slow_interrupt(task_key: str, message: str) -> None:
            interrupt_started.set()
            await interrupt_can_continue.wait()

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=slow_interrupt,
        )

        # Start send_message - it will block at the interrupt
        async def send_msg() -> AgentMessage:
            return await mb.send_message("QR-1", "Backend", "QR-2", "Test message")

        send_task = asyncio.create_task(send_msg())

        # Wait for interrupt to start
        await interrupt_started.wait()

        # Now call unregister while send is in the middle of delivery
        unregister_task = asyncio.create_task(mb.unregister_agent("QR-2"))

        # Let interrupt continue
        interrupt_can_continue.set()

        # Wait for both operations to complete
        msg, _ = await asyncio.gather(send_task, unregister_task)

        # Message should be in a consistent state: either delivered+expired or just expired
        # It should NOT be orphaned in _messages without an inbox
        assert msg.status == MessageStatus.EXPIRED or msg.delivery_status in (
            DeliveryStatus.DELIVERED,
            DeliveryStatus.QUEUED,
        )

        # Either message was removed by unregister, or it's still there but expired
        if msg.id in mb._messages:
            assert mb._messages[msg.id].status == MessageStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_unregister_during_reply_does_not_lose_reply(self) -> None:
        """Test that unregister during reply maintains reply consistency."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        interrupt_started = asyncio.Event()

        async def slow_interrupt(task_key: str, message: str) -> None:
            interrupt_started.set()
            await asyncio.sleep(0.05)  # Small delay for concurrent unregister

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=slow_interrupt,
        )

        # Send initial message
        msg = await mb.send_message("QR-1", "Backend", "QR-2", "Question?")
        mb.get_unread_messages("QR-2")

        interrupt_started.clear()

        # Start reply - it will yield during the interrupt
        async def reply_msg() -> None:
            await mb.reply_to_message(msg.id, "Answer", "QR-2")

        reply_task = asyncio.create_task(reply_msg())

        # Wait for interrupt to start
        await interrupt_started.wait()

        # Now unregister sender while reply is in progress
        unregister_task = asyncio.create_task(mb.unregister_agent("QR-1"))

        # Give unregister a chance to run
        await asyncio.sleep(0.01)

        # Wait for both operations to complete
        await asyncio.gather(reply_task, unregister_task)

        # Reply event should be set and message should be in REPLIED state
        # (set before lock acquisition, so always succeeds even with concurrent unregister)
        assert msg._reply_event.is_set()
        assert msg.status == MessageStatus.REPLIED
        assert msg.reply_text == "Answer"

    @pytest.mark.asyncio
    async def test_concurrent_sends_and_unregister(self) -> None:
        """Test multiple concurrent sends with unregister."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        async def list_agents() -> list[AgentInfo]:
            return []

        async def fast_interrupt(task_key: str, message: str) -> None:
            await asyncio.sleep(0.01)  # Small delay to increase chance of race

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=fast_interrupt,
        )

        # Fire 5 sends and 1 unregister concurrently
        async def send_msg(i: int) -> AgentMessage:
            return await mb.send_message("QR-1", "Backend", "QR-2", f"Message {i}")

        async def unregister() -> None:
            await asyncio.sleep(0.005)  # Small delay before unregister
            await mb.unregister_agent("QR-2")

        tasks = [send_msg(i) for i in range(5)]
        tasks.append(unregister())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Should complete without exceptions
        for result in results[:-1]:  # All send results
            assert not isinstance(result, Exception)
            if isinstance(result, AgentMessage):
                # Message should be in a valid state
                assert result.status in (MessageStatus.PENDING, MessageStatus.EXPIRED, MessageStatus.READ)

    @pytest.mark.asyncio
    async def test_unregister_blocks_until_delivery_completes(self) -> None:
        """Test that unregister waits for in-progress delivery to complete."""
        mb = AgentMailbox()
        mb.register_agent("QR-1")
        mb.register_agent("QR-2")

        delivery_started = asyncio.Event()
        delivery_completed = asyncio.Event()

        async def slow_interrupt(task_key: str, message: str) -> None:
            delivery_started.set()
            await asyncio.sleep(0.1)  # Simulate slow delivery
            delivery_completed.set()

        async def list_agents() -> list[AgentInfo]:
            return []

        mb.set_callbacks(
            list_agents=list_agents,
            interrupt_agent=slow_interrupt,
        )

        # Start send_message
        async def send_msg() -> AgentMessage:
            return await mb.send_message("QR-1", "Backend", "QR-2", "Test message")

        send_task = asyncio.create_task(send_msg())

        # Wait for delivery to start
        await delivery_started.wait()

        # Now start unregister - it should block until delivery completes
        async def unregister() -> None:
            await mb.unregister_agent("QR-2")

        unregister_task = asyncio.create_task(unregister())

        # Give unregister a chance to try to run
        await asyncio.sleep(0.01)

        # Wait for everything to complete
        msg, _ = await asyncio.gather(send_task, unregister_task)

        # CRITICAL: Message must be DELIVERED first, THEN EXPIRED by unregister
        # This proves unregister waited for delivery to complete
        assert msg.delivery_status == DeliveryStatus.DELIVERED
        assert msg.status == MessageStatus.EXPIRED
        assert delivery_completed.is_set()
