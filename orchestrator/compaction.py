"""Context window compaction for long-running agent sessions.

Monitors token usage, summarizes context via Haiku, and provides
continuation prompts for new sessions.
Based on OpenCode's compaction approach (github.com/anomalyco/opencode)
and Anthropic Cookbook: Automatic Context Compaction.
"""

from __future__ import annotations

import logging

from orchestrator.agent_runner import AgentResult
from orchestrator.config import Config
from orchestrator.llm_utils import call_llm_for_text

logger = logging.getLogger(__name__)

_FALLBACK_TRUNCATION_LENGTH = 4000

# Model context limits (all Claude 4.x models have 200K context)
_MODEL_CONTEXT_LIMITS = {
    "claude-sonnet-4-5-20250929": 200000,
    "claude-opus-4-6": 200000,
    "claude-haiku-4-5-20251001": 200000,
}
_DEFAULT_CONTEXT_LIMIT = 200000


def _get_model_context_limit(model: str) -> int:
    """Get context window size for a model.

    Args:
        model: Model identifier (e.g. "claude-sonnet-4-5-20250929")

    Returns:
        Context window size in tokens
    """
    return _MODEL_CONTEXT_LIMITS.get(model, _DEFAULT_CONTEXT_LIMIT)


def should_compact(result: AgentResult, config: Config, model: str) -> bool:
    """Determine if context should be compacted based on token usage.

    Compaction triggers when:
    - Compaction is enabled in config
    - total_tokens >= (model_context_limit - buffer_tokens)

    Args:
        result: Agent result with token usage
        config: Orchestrator configuration
        model: Model identifier

    Returns:
        True if compaction should be triggered
    """
    if not config.compaction_enabled:
        return False

    limit = _get_model_context_limit(model)
    threshold = limit - config.compaction_buffer_tokens

    # Guard against negative threshold (misconfigured buffer > limit)
    if threshold < 0:
        logger.warning(
            "Invalid compaction config: buffer_tokens (%d) > model limit (%d). Disabling compaction.",
            config.compaction_buffer_tokens,
            limit,
        )
        return False

    total = result.total_tokens

    if total >= threshold:
        logger.info(
            "Compaction threshold reached: %d tokens (threshold: %d, limit: %d, buffer: %d)",
            total,
            threshold,
            limit,
            config.compaction_buffer_tokens,
        )
        return True

    return False


_SUMMARIZE_PROMPT_TEMPLATE = """\
Summarize the following agent session output for continuing the task in a new session.
The new session will NOT have access to the previous conversation.

Use this template:
---
## Goal
[What the agent was trying to accomplish]

## Accomplished
[What work has been completed]

## In Progress
[What was being worked on when the session ended]

## Discoveries
[Notable findings — file paths, patterns, constraints, decisions made]

## Remaining
[What still needs to be done]

## Relevant Files
[Key files read, edited, or created]
---

Agent output to summarize:
{output}"""


_SUMMARIZE_SYSTEM_PROMPT = "You are a summarization assistant. Output only the summary, nothing else."


async def summarize_output(output: str, config: Config) -> str:
    """Summarize agent session output for context compaction.

    Uses Haiku (config.compaction_model) to create a structured summary.
    Falls back to truncating the output on error or empty result.

    Args:
        output: Raw agent session output
        config: Orchestrator configuration

    Returns:
        Structured summary string
    """
    prompt = _SUMMARIZE_PROMPT_TEMPLATE.format(output=output)
    try:
        summary = await call_llm_for_text(
            prompt,
            config,
            system_prompt=_SUMMARIZE_SYSTEM_PROMPT,
            timeout_seconds=60,
            separator="\n",
        )
        if summary.strip():
            return summary
        logger.warning("Compaction summarization returned empty result, using truncated output")
    except Exception:
        logger.warning("Compaction summarization failed, using truncated output", exc_info=True)

    # Fallback: return last N characters of original output
    return output[-_FALLBACK_TRUNCATION_LENGTH:]


def build_continuation_prompt(issue_key: str, issue_summary: str, summary: str) -> str:
    """Build a prompt for continuing work in a new session after compaction.

    Args:
        issue_key: Tracker issue key (e.g. "QR-42")
        issue_summary: Issue title/summary
        summary: Structured summary from previous session

    Returns:
        Continuation prompt string
    """
    return f"""\
You are continuing work on task {issue_key}: {issue_summary}

## Context from previous session
{summary}

## Instructions
Continue working on this task from where the previous session left off.
If you have next steps, proceed with them. If you are unsure how to proceed,
read the task details using tracker_get_issue and review the current state of the code.

**IMPORTANT**: All public-facing text MUST be written in Russian."""
