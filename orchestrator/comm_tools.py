"""In-process MCP tool wrappers for inter-agent communication."""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from orchestrator.agent_mailbox import AgentMailbox, MessageType

logger = logging.getLogger(__name__)


def build_comm_server(
    mailbox: AgentMailbox,
    issue_key: str,
    issue_summary: str,
) -> Any:
    """Build an MCP server with inter-agent communication tools.

    Each agent gets its own server instance scoped to its issue_key,
    so tools know which agent is calling.
    """

    @tool(
        "list_running_agents",
        "Discover other agents with active sessions (running, in_review, needs_info). "
        "You can also send messages to agents not in this list — delivery will resume their session.",
        {},
    )
    async def list_running_agents(args: dict[str, Any]) -> dict[str, Any]:
        agents = await mailbox.list_agents()
        # Exclude self from the list
        peers = [a for a in agents if a.task_key != issue_key]
        if not peers:
            return {"content": [{"type": "text", "text": "No other agents are currently running."}]}

        lines = [f"**{len(peers)} agent(s) running:**"]
        for agent in peers:
            extra_parts = []
            if agent.component:
                extra_parts.append(f"component: {agent.component}")
            if agent.repo:
                extra_parts.append(f"repo: {agent.repo}")
            extra = f" [{', '.join(extra_parts)}]" if extra_parts else ""
            lines.append(f"- **{agent.task_key}**: {agent.task_summary} ({agent.status}){extra}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "send_message_to_agent",
        "Send a notification message to another agent (running, in_review, or completed). "
        "Active agents receive it via interrupt immediately. "
        "Inactive agents get an on-demand session resumed to process the message. "
        "You can continue working while waiting for a reply. "
        "Use send_request_to_agent if you need a synchronous reply.",
        {"target_task_key": str, "message": str},
    )
    async def send_message_to_agent(args: dict[str, Any]) -> dict[str, Any]:
        target_key = args["target_task_key"]
        message = args["message"]
        try:
            msg = await mailbox.send_message(
                issue_key,
                issue_summary,
                target_key,
                message,
                msg_type=MessageType.NOTIFICATION,
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Message sent to {target_key} "
                            f"(id: {msg.id}, delivery: {msg.delivery_status}). "
                            "Continue working — you'll receive a reply as an interrupt."
                        ),
                    }
                ]
            }
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error sending message: {e}"}]}

    @tool(
        "send_request_to_agent",
        "Send a request to another agent and wait for their reply (blocking). "
        "Use this when you need information before continuing. "
        "Falls back with a timeout message if no reply within the timeout.",
        {"target_task_key": str, "message": str, "timeout_seconds": int},
    )
    async def send_request_to_agent(args: dict[str, Any]) -> dict[str, Any]:
        target_key = args["target_task_key"]
        message = args["message"]
        timeout = float(args.get("timeout_seconds", 60))
        try:
            reply = await mailbox.request_and_wait(
                issue_key,
                issue_summary,
                target_key,
                message,
                wait_timeout=timeout,
            )
            if reply is None:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Timeout: no reply received within {int(timeout)}s]",
                        }
                    ]
                }
            return {"content": [{"type": "text", "text": reply}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error sending request: {e}"}]}

    @tool(
        "reply_to_message",
        "Reply to a message from another agent. The reply is delivered to the sender via interrupt.",
        {"message_id": str, "reply_text": str},
    )
    async def reply_to_message(args: dict[str, Any]) -> dict[str, Any]:
        message_id = args["message_id"]
        reply_text = args["reply_text"]
        try:
            await mailbox.reply_to_message(message_id, reply_text, issue_key)
            return {"content": [{"type": "text", "text": "Reply sent successfully."}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error replying: {e}"}]}

    @tool(
        "check_messages",
        "Check for unread messages from other agents. Use this as a fallback "
        "if you want to proactively check your inbox.",
        {},
    )
    async def check_messages(args: dict[str, Any]) -> dict[str, Any]:
        unread = mailbox.get_unread_messages(issue_key)
        if not unread:
            return {"content": [{"type": "text", "text": "No unread messages."}]}

        lines = [f"**{len(unread)} unread message(s):**"]
        for msg in unread:
            lines.append(
                f"\n**From {msg.sender_task_key}** ({msg.sender_summary}) "
                f"[type: {msg.msg_type}]:\nMessage ID: {msg.id}\n{msg.text}"
            )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    return create_sdk_mcp_server(
        name="comm",
        version="1.0.0",
        tools=[
            list_running_agents,
            send_message_to_agent,
            send_request_to_agent,
            reply_to_message,
            check_messages,
        ],
    )
