"""Shared LLM call utilities for one-shot SDK queries."""

from __future__ import annotations

import asyncio
import tempfile
from typing import TYPE_CHECKING

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock

from orchestrator.agent_runner import receive_response_safe

if TYPE_CHECKING:
    from orchestrator.config import Config


async def call_llm_for_text(
    prompt: str,
    config: Config,
    system_prompt: str,
    timeout_seconds: int = 60,
    separator: str = "\n",
) -> str:
    """Call the compaction model for a one-shot text response.

    Args:
        prompt: The user prompt to send.
        config: Orchestrator configuration (for model and auth).
        system_prompt: System prompt to guide the model.
        timeout_seconds: Seconds to wait for a response.
        separator: String used to join multiple text blocks.

    Returns:
        Text response from the model.

    Raises:
        asyncio.TimeoutError: If the call exceeds timeout.
        Any exception from ClaudeSDKClient.
    """
    options = ClaudeAgentOptions(
        model=config.compaction_model,
        system_prompt=system_prompt,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        env=config.agent_env,
        cwd=tempfile.gettempdir(),
    )
    client = ClaudeSDKClient(options=options)
    output_parts: list[str] = []
    async with asyncio.timeout(timeout_seconds):
        await client.__aenter__()
        try:
            await client.query(prompt)
            async for message in receive_response_safe(client):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            output_parts.append(block.text)
        finally:
            await client.__aexit__(None, None, None)
    return separator.join(output_parts)
