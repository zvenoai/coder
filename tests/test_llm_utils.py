"""Tests for llm_utils.call_llm_for_text().

NOTE: orchestrator.llm_utils depends on claude_agent_sdk which is
mocked by the autouse mock_sdk fixture in conftest.py.  That fixture
purges orchestrator.llm_utils from sys.modules on every test, so
all llm_utils imports must be inside test methods (after the mock
is in place).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.config import Config


@pytest.mark.parametrize(
    ("separator", "expected"),
    [
        ("\n", "Part 1\nPart 2"),
        ("", "Part 1Part 2"),
    ],
)
async def test_joins_text_blocks_with_separator(separator: str, expected: str) -> None:
    from orchestrator.llm_utils import call_llm_for_text

    config = Config(tracker_token="t", tracker_org_id="o")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    mock_client.query = AsyncMock()

    from claude_agent_sdk import AssistantMessage, TextBlock

    tb1 = TextBlock(text="Part 1")
    tb2 = TextBlock(text="Part 2")
    msg = AssistantMessage(content=[tb1, tb2], model="test")

    async def fake_response():
        yield msg

    mock_client.receive_response = fake_response

    with patch("orchestrator.llm_utils.ClaudeSDKClient", return_value=mock_client):
        result = await call_llm_for_text(
            "prompt",
            config,
            system_prompt="sys",
            timeout_seconds=60,
            separator=separator,
        )

    assert result == expected


async def test_creates_client_with_compaction_model() -> None:
    from orchestrator.llm_utils import call_llm_for_text

    config = Config(
        tracker_token="t",
        tracker_org_id="o",
        compaction_model="claude-haiku-4-5-20251001",
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    mock_client.query = AsyncMock()

    async def empty_response():
        return
        yield  # make it an async generator

    mock_client.receive_response = empty_response

    with (
        patch("orchestrator.llm_utils.ClaudeSDKClient", return_value=mock_client),
        patch("orchestrator.llm_utils.ClaudeAgentOptions") as mock_opts_cls,
    ):
        await call_llm_for_text(
            "test prompt",
            config,
            system_prompt="You are helpful.",
        )

    mock_opts_cls.assert_called_once()
    call_kwargs = mock_opts_cls.call_args[1]
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
    assert call_kwargs["permission_mode"] == "bypassPermissions"
    assert call_kwargs["allowed_tools"] == []
    assert call_kwargs["system_prompt"] == "You are helpful."


async def test_closes_client_on_error() -> None:
    from orchestrator.llm_utils import call_llm_for_text

    config = Config(tracker_token="t", tracker_org_id="o")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    mock_client.query = AsyncMock(side_effect=RuntimeError("connection lost"))

    with (
        patch("orchestrator.llm_utils.ClaudeSDKClient", return_value=mock_client),
        pytest.raises(RuntimeError, match="connection lost"),
    ):
        await call_llm_for_text("prompt", config, system_prompt="sys")

    mock_client.__aexit__.assert_awaited_once()
