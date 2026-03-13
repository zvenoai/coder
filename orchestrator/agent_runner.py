"""Claude Agent SDK wrapper for running agents."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    Literal,
    NamedTuple,
    cast,
)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from orchestrator.config import Config
from orchestrator.constants import EventType
from orchestrator.event_bus import Event, EventBus
from orchestrator.prompt_builder import build_system_prompt_append
from orchestrator.tracker_client import TrackerClient, TrackerIssue
from orchestrator.tracker_tools import ToolState, build_tracker_server

if TYPE_CHECKING:
    from orchestrator.agent_mailbox import AgentMailbox
    from orchestrator.storage import Storage

logger = logging.getLogger(__name__)

PR_URL_PATTERN = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

# Max retries when SDK raises for unknown message types (e.g. rate_limit_event)
_MAX_UNKNOWN_MSG_RETRIES = 10

_BASE_ALLOWED_TOOLS: Final[tuple[str, ...]] = (
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "Task",
    "WebSearch",
    "WebFetch",
    "mcp__tracker__tracker_get_issue",
    "mcp__tracker__tracker_add_comment",
    "mcp__tracker__tracker_get_comments",
    "mcp__tracker__tracker_get_checklist",
    "mcp__tracker__tracker_request_info",
    "mcp__tracker__tracker_signal_blocked",
    "mcp__tracker__tracker_create_subtask",
    "mcp__tracker__propose_improvement",
    "mcp__tracker__tracker_get_attachments",
    "mcp__tracker__tracker_download_attachment",
    "mcp__tracker__tracker_create_workpad",
    "mcp__tracker__tracker_update_workpad",
    "mcp__tracker__tracker_mark_complete",
)

_WORKSPACE_TOOLS: Final[tuple[str, ...]] = (
    "mcp__workspace__list_available_repos",
    "mcp__workspace__request_worktree",
)

_COMM_TOOLS: Final[tuple[str, ...]] = (
    "mcp__comm__list_running_agents",
    "mcp__comm__send_message_to_agent",
    "mcp__comm__send_request_to_agent",
    "mcp__comm__reply_to_message",
    "mcp__comm__check_messages",
)


async def receive_response_safe(
    client: ClaudeSDKClient,
) -> AsyncIterator[object]:
    """Iterate ``client.receive_response()`` skipping unknown message types.

    The SDK raises ``MessageParseError`` for unrecognised message types
    (e.g. ``rate_limit_event``).  This wrapper catches those errors and
    retries by creating a new ``receive_response()`` generator against
    the same underlying anyio memory channel.
    """
    skip_count = 0
    done = False
    while not done:
        try:
            async for message in client.receive_response():
                yield message
            done = True
        except Exception as parse_err:
            if "Unknown message type" in str(parse_err) and skip_count < _MAX_UNKNOWN_MSG_RETRIES:
                skip_count += 1
                logger.warning(
                    "Skipping unknown SDK message type (%d/%d): %s",
                    skip_count,
                    _MAX_UNKNOWN_MSG_RETRIES,
                    parse_err,
                )
                # receive_response() creates a fresh generator
                # chain against the same anyio memory channel,
                # picking up remaining buffered messages.
                continue
            raise


@dataclass
class AgentResult:
    """Result of an agent run."""

    success: bool
    output: str
    error_category: str | None = None
    cost_usd: float | None = None
    duration_seconds: float | None = None
    pr_url: str | None = None
    needs_info: bool = False
    resumed: bool = False
    is_rate_limited: bool = False
    continuation_exhausted: bool = False
    externally_resolved: bool = False
    proposals: list[dict[str, str]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total tokens (input + output)."""
        return self.input_tokens + self.output_tokens


class _ResultData(NamedTuple):
    """Data extracted from a ResultMessage."""

    cost: float | None
    input_tokens: int
    output_tokens: int


class _ToolStateSnapshot(NamedTuple):
    """Snapshot of ToolState side-channel data."""

    needs_info: bool
    proposals: list[dict[str, str]]


def merge_results(base: AgentResult, update: AgentResult) -> AgentResult:
    """Merge drain result into base: accumulate costs, prefer latest data.

    Field semantics:
    - Sticky flags (success, needs_info, is_rate_limited): once True, stays True.
    - Latest-wins (output, pr_url): prefer update, fallback base.
    - Accumulators (cost_usd, duration_seconds, proposals): sum/concat.
    - Token counts: latest-wins if update succeeded (avoids double-counting).

    Token handling rationale: SDK's next ``input_tokens`` already includes
    prior outputs as part of the conversation history.  Summing would
    double-count them in ``total_tokens``.  Failed drain calls return
    ``AgentResult`` with zero defaults — overwriting base's real counts
    with zeros would prevent compaction from triggering, so we only use
    update tokens when update succeeded.
    """
    return AgentResult(
        success=base.success or update.success,
        output=update.output or base.output,
        error_category=update.error_category or base.error_category,
        cost_usd=(base.cost_usd or 0) + (update.cost_usd or 0),
        duration_seconds=((base.duration_seconds or 0) + (update.duration_seconds or 0)),
        pr_url=update.pr_url or base.pr_url,
        needs_info=update.needs_info or base.needs_info,
        is_rate_limited=base.is_rate_limited or update.is_rate_limited,
        continuation_exhausted=(base.continuation_exhausted or update.continuation_exhausted),
        externally_resolved=(base.externally_resolved or update.externally_resolved),
        resumed=base.resumed,
        proposals=base.proposals + update.proposals,
        input_tokens=(update.input_tokens if update.success else base.input_tokens),
        output_tokens=(update.output_tokens if update.success else base.output_tokens),
    )


class AgentSession:
    """Long-lived SDK session — supports multiple query() calls."""

    def __init__(
        self,
        client: ClaudeSDKClient,
        issue_key: str,
        event_bus: EventBus | None = None,
        tool_state: ToolState | None = None,
    ) -> None:
        self._client = client
        self._issue_key = issue_key
        self._event_bus = event_bus
        self._tool_state = tool_state
        self._pending_messages: asyncio.Queue[str] = asyncio.Queue()
        self._session_id: str | None = None
        self._closed: bool = False
        self.cumulative_input_tokens: int = 0
        self.cumulative_output_tokens: int = 0

    @property
    def session_id(self) -> str | None:
        """Session ID from the last ResultMessage (for session resumption)."""
        return self._session_id

    @property
    def closed(self) -> bool:
        """Whether the session has been closed."""
        return self._closed

    async def interrupt(self) -> None:
        """Interrupt the running agent without queueing a message."""
        await self._client.interrupt()

    async def interrupt_with_message(self, message: str) -> None:
        """Interrupt the running agent and queue a message for it."""
        self._pending_messages.put_nowait(message)
        await self._client.interrupt()

    def has_pending_messages(self) -> bool:
        """Check if there are pending messages to deliver."""
        return not self._pending_messages.empty()

    def get_pending_message(self) -> str | None:
        """Get the next pending message, or None if empty."""
        try:
            return self._pending_messages.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _process_assistant_message(
        self,
        message: AssistantMessage,
        output_parts: list[str],
    ) -> bool:
        """Process an AssistantMessage: collect text, detect rate limit.

        Returns True if rate_limit error detected.
        """
        is_rate_limited = message.error == "rate_limit"
        for block in message.content:
            if isinstance(block, TextBlock):
                output_parts.append(block.text)
                if self._event_bus:
                    await self._event_bus.publish(
                        Event(
                            type=EventType.AGENT_OUTPUT,
                            task_key=self._issue_key,
                            data={"text": block.text},
                        )
                    )
        return is_rate_limited

    async def _apply_result_message(
        self,
        message: ResultMessage,
        start: float,
    ) -> _ResultData:
        """Extract cost/tokens from ResultMessage, publish event.

        Side effects: updates ``_session_id`` and cumulative token
        counts on the session instance.
        """
        cost = getattr(message, "total_cost_usd", None)
        self._session_id = getattr(message, "session_id", None)
        usage = getattr(message, "usage", None)
        input_tokens = usage.get("input_tokens", 0) if usage else 0
        output_tokens = usage.get("output_tokens", 0) if usage else 0
        # Tokens: latest-wins (SDK includes prior context)
        self.cumulative_input_tokens = input_tokens
        self.cumulative_output_tokens = output_tokens
        if self._event_bus:
            elapsed_ms = (time.monotonic() - start) * 1000
            await self._event_bus.publish(
                Event(
                    type=EventType.AGENT_RESULT,
                    task_key=self._issue_key,
                    data={
                        "cost": cost,
                        "duration_ms": elapsed_ms,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                )
            )
        return _ResultData(
            cost=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _read_tool_state(self) -> _ToolStateSnapshot:
        """Read and reset tool_state side-channel flags.

        Returns a snapshot; resets ``needs_info_requested`` and
        ``proposals`` on the underlying ToolState so they are not
        consumed twice.
        """
        if not self._tool_state:
            return _ToolStateSnapshot(
                needs_info=False,
                proposals=[],
            )
        needs_info = self._tool_state.needs_info_requested
        if needs_info:
            self._tool_state.needs_info_requested = False
        proposals = list(self._tool_state.proposals)
        if proposals:
            self._tool_state.proposals.clear()
        return _ToolStateSnapshot(
            needs_info=needs_info,
            proposals=proposals,
        )

    def _build_success_result(
        self,
        output_parts: list[str],
        result_data: _ResultData,
        is_rate_limited: bool,
        duration: float,
    ) -> AgentResult:
        """Build successful AgentResult from collected data."""
        output = "\n".join(output_parts) if output_parts else "No output"
        pr_match = PR_URL_PATTERN.search(output)
        logger.info(
            "Agent session for %s completed in %.0fs (cost: %s, tokens: %d in/%d out)",
            self._issue_key,
            duration,
            result_data.cost,
            result_data.input_tokens,
            result_data.output_tokens,
        )
        ts = self._read_tool_state()
        return AgentResult(
            success=True,
            output=output,
            cost_usd=result_data.cost,
            duration_seconds=duration,
            pr_url=(pr_match.group(0) if pr_match else None),
            needs_info=ts.needs_info,
            is_rate_limited=is_rate_limited,
            proposals=ts.proposals,
            input_tokens=result_data.input_tokens,
            output_tokens=result_data.output_tokens,
        )

    async def send(self, prompt: str) -> AgentResult:
        """Send a prompt and collect response (re-usable)."""
        if self._closed:
            return AgentResult(
                success=False,
                output="Session closed",
            )
        start = time.monotonic()
        output_parts: list[str] = []
        result_data = _ResultData(None, 0, 0)
        is_rate_limited = False
        try:
            await self._client.query(prompt)
            async for msg in receive_response_safe(
                self._client,
            ):
                if isinstance(msg, AssistantMessage):
                    if await self._process_assistant_message(
                        msg,
                        output_parts,
                    ):
                        is_rate_limited = True
                elif isinstance(msg, ResultMessage):
                    result_data = await self._apply_result_message(
                        msg,
                        start,
                    )
        except Exception as e:
            logger.error(
                "Agent session failed for %s: %s",
                self._issue_key,
                e,
            )
            return AgentResult(
                success=False,
                output=str(e),
                duration_seconds=time.monotonic() - start,
            )
        return self._build_success_result(
            output_parts,
            result_data,
            is_rate_limited,
            duration=time.monotonic() - start,
        )

    async def drain_pending_messages(
        self,
        base_result: AgentResult,
    ) -> AgentResult:
        """Drain all pending interrupt messages, merging results.

        Best-effort: exceptions are caught and logged so they don't
        prevent the caller from handling the base result.

        Returns base_result unchanged if no messages are pending.
        """
        if not self.has_pending_messages():
            return base_result
        result = base_result
        try:
            while self.has_pending_messages():
                msg = self.get_pending_message()
                if msg is not None:
                    drain_result = await self.send(msg)
                    result = merge_results(result, drain_result)
        except Exception:
            logger.warning(
                "Error draining interrupt messages for %s",
                self._issue_key,
                exc_info=True,
            )
        return result

    def transfer_pending_messages(self, target: AgentSession) -> int:
        """Transfer all pending messages to another session.

        Used during compaction to preserve messages when replacing
        the session. Returns the number of messages transferred.
        """
        count = 0
        while not self._pending_messages.empty():
            try:
                msg = self._pending_messages.get_nowait()
                target._pending_messages.put_nowait(msg)
                count += 1
            except asyncio.QueueEmpty:
                break
        return count

    def transfer_cumulative_tokens(
        self,
        target: AgentSession,
    ) -> None:
        """Copy cumulative token counts to a new session."""
        target.cumulative_input_tokens = self.cumulative_input_tokens
        target.cumulative_output_tokens = self.cumulative_output_tokens

    async def close(self) -> None:
        """Close the underlying SDK client."""
        self._closed = True
        try:
            await self._client.__aexit__(None, None, None)
        except Exception:
            logger.warning("Error closing agent session for %s", self._issue_key)


class AgentRunner:
    """Launches Claude Agent SDK to execute tasks."""

    def __init__(
        self,
        config: Config,
        tracker: TrackerClient,
        storage: Storage | None = None,
    ) -> None:
        self._config = config
        self._tracker = tracker
        self._storage = storage

    def _build_mcp_servers(
        self,
        issue: TrackerIssue,
        tool_state: ToolState | None,
        workspace_server: object | None,
        mailbox: AgentMailbox | None,
    ) -> dict[str, Any]:
        """Build MCP server dict for agent options."""
        tracker_server = build_tracker_server(
            self._tracker,
            issue.key,
            tool_state=tool_state,
            config=self._config,
            issue_components=issue.components,
            storage=self._storage,
        )
        servers: dict[str, Any] = {"tracker": tracker_server}
        if workspace_server:
            servers["workspace"] = workspace_server
        if mailbox is not None:
            # Lazy import: avoids SDK resolution issues
            # under autouse mock_sdk fixture in tests.
            from orchestrator.comm_tools import build_comm_server

            servers["comm"] = build_comm_server(
                mailbox,
                issue.key,
                issue.summary,
            )
        return servers

    def _build_allowed_tools(
        self,
        workspace_server: object | None,
        mailbox: AgentMailbox | None,
    ) -> list[str]:
        """Build allowed tools list based on available servers."""
        tools = list(_BASE_ALLOWED_TOOLS)
        if workspace_server:
            tools.extend(_WORKSPACE_TOOLS)
        if mailbox is not None:
            tools.extend(_COMM_TOOLS)
        return tools

    def _build_options(
        self,
        issue: TrackerIssue,
        tool_state: ToolState | None = None,
        model: str | None = None,
        workspace_server: object | None = None,
        cwd: str | None = None,
        resume_session_id: str | None = None,
        mailbox: AgentMailbox | None = None,
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions for a task."""
        cfg = self._config
        workflow_content = build_system_prompt_append(
            cfg.workflow_prompt_path,
        )
        mcp_servers = self._build_mcp_servers(
            issue,
            tool_state,
            workspace_server,
            mailbox,
        )
        allowed_tools = self._build_allowed_tools(
            workspace_server,
            mailbox,
        )

        resume_kwargs: dict[str, Any] = {}
        if resume_session_id:
            resume_kwargs["resume"] = resume_session_id
            resume_kwargs["fork_session"] = True

        max_budget = float(cfg.agent_max_budget_usd) if cfg.agent_max_budget_usd is not None else None
        return ClaudeAgentOptions(
            model=model or cfg.agent_model,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": workflow_content,
            },
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            permission_mode=cast(
                Literal[
                    "default",
                    "acceptEdits",
                    "plan",
                    "bypassPermissions",
                ]
                | None,
                cfg.agent_permission_mode,
            ),
            cwd=cwd or "/tmp",
            max_budget_usd=max_budget,
            hooks={},
            env=cfg.agent_env,
            setting_sources=["project"],
            **resume_kwargs,
        )

    async def create_session(
        self,
        issue: TrackerIssue,
        event_bus: EventBus | None = None,
        tool_state: ToolState | None = None,
        model: str | None = None,
        workspace_server: object | None = None,
        cwd: str | None = None,
        resume_session_id: str | None = None,
        mailbox: AgentMailbox | None = None,
    ) -> AgentSession:
        """Create a long-lived agent session."""
        options = self._build_options(
            issue,
            tool_state=tool_state,
            model=model,
            workspace_server=workspace_server,
            cwd=cwd,
            resume_session_id=resume_session_id,
            mailbox=mailbox,
        )
        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        return AgentSession(client, issue.key, event_bus=event_bus, tool_state=tool_state)
