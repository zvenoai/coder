"""Tests for compaction module.

NOTE: orchestrator.compaction depends on claude_agent_sdk which is
mocked by the autouse mock_sdk fixture in conftest.py.  That fixture
purges orchestrator.compaction from sys.modules on every test, so
all compaction imports must be inside test methods (after the mock
is in place).
"""

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.agent_runner import AgentResult
from orchestrator.config import Config

# All current Claude models have 200K context
ALL_MODELS = [
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
]


@pytest.fixture
def base_config() -> Config:
    return Config(
        tracker_token="t",
        tracker_org_id="o",
        compaction_enabled=True,
        compaction_buffer_tokens=20000,
    )


class TestGetModelContextLimit:
    @pytest.mark.parametrize("model", [*ALL_MODELS, "claude-future-model"])
    def test_returns_200k(self, model) -> None:
        from orchestrator.compaction import _get_model_context_limit

        assert _get_model_context_limit(model) == 200000


class TestShouldCompact:
    @pytest.mark.parametrize("model", ALL_MODELS)
    def test_compacts_when_tokens_exceed_threshold(self, base_config, model) -> None:
        from orchestrator.compaction import should_compact

        # Threshold = 200000 - 20000 = 180000; total = 185000
        result = AgentResult(
            success=True,
            output="test",
            input_tokens=150000,
            output_tokens=35000,
        )
        assert should_compact(result, base_config, model=model) is True

    def test_no_compact_when_below_threshold(self, base_config) -> None:
        from orchestrator.compaction import should_compact

        result = AgentResult(
            success=True,
            output="test",
            input_tokens=100000,
            output_tokens=50000,  # total=150000 < 180000
        )
        assert (
            should_compact(
                result,
                base_config,
                model="claude-sonnet-4-5-20250929",
            )
            is False
        )

    def test_no_compact_when_disabled(self) -> None:
        from orchestrator.compaction import should_compact

        config = Config(
            tracker_token="t",
            tracker_org_id="o",
            compaction_enabled=False,
            compaction_buffer_tokens=20000,
        )
        result = AgentResult(
            success=True,
            output="test",
            input_tokens=150000,
            output_tokens=50000,
        )
        assert (
            should_compact(
                result,
                config,
                model="claude-sonnet-4-5-20250929",
            )
            is False
        )

    def test_no_compact_with_negative_threshold(self) -> None:
        """buffer > limit → negative threshold; must not trigger."""
        from orchestrator.compaction import should_compact

        config = Config(
            tracker_token="t",
            tracker_org_id="o",
            compaction_enabled=True,
            compaction_buffer_tokens=300000,
        )
        result = AgentResult(
            success=True,
            output="test",
            input_tokens=0,
            output_tokens=0,
        )
        assert (
            should_compact(
                result,
                config,
                model="claude-sonnet-4-5-20250929",
            )
            is False
        )


class TestSummarizeOutput:
    async def test_returns_summary(self) -> None:
        from orchestrator.compaction import summarize_output

        config = Config(
            tracker_token="t",
            tracker_org_id="o",
            compaction_model="claude-haiku-4-5-20251001",
        )
        summary = "## Goal\nFix the bug\n## Accomplished\nWrote tests"

        with patch(
            "orchestrator.compaction.call_llm_for_text",
            new_callable=AsyncMock,
            return_value=summary,
        ) as mock_call:
            result = await summarize_output("Long agent output here...", config)

        assert result == summary
        mock_call.assert_awaited_once()
        assert mock_call.call_args[0][1] is config
        assert "Long agent output here..." in mock_call.call_args[0][0]

    async def test_fallback_on_error(self) -> None:
        """SDK error → truncated output (last 4000 chars)."""
        from orchestrator.compaction import summarize_output

        config = Config(tracker_token="t", tracker_org_id="o")
        long_output = "x" * 5000

        with patch(
            "orchestrator.compaction.call_llm_for_text",
            new_callable=AsyncMock,
            side_effect=RuntimeError("SDK error"),
        ):
            result = await summarize_output(long_output, config)

        assert len(result) == 4000

    async def test_fallback_on_empty(self) -> None:
        """Empty SDK output → original text."""
        from orchestrator.compaction import summarize_output

        config = Config(tracker_token="t", tracker_org_id="o")

        with patch(
            "orchestrator.compaction.call_llm_for_text",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await summarize_output("original output", config)

        assert result == "original output"


class TestBuildContinuationPrompt:
    def test_includes_summary_and_issue_key(self) -> None:
        from orchestrator.compaction import build_continuation_prompt

        prompt = build_continuation_prompt(
            issue_key="QR-42",
            issue_summary="Fix auth bug",
            summary="## Goal\nFix auth\n## Accomplished\nWrote tests",
        )

        assert "QR-42" in prompt
        assert "Fix auth bug" in prompt
        assert "## Goal" in prompt
        assert "## Accomplished" in prompt

    def test_includes_russian_instruction(self) -> None:
        from orchestrator.compaction import build_continuation_prompt

        prompt = build_continuation_prompt(
            issue_key="QR-1",
            issue_summary="Test",
            summary="Summary",
        )
        assert "Russian" in prompt
