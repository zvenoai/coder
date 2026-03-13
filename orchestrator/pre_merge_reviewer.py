"""Pre-merge code review sub-agent — semantic review before auto-merge."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from orchestrator.config import Config
    from orchestrator.github_client import GitHubClient, PRDetails, PRFile
    from orchestrator.tracker_client import TrackerClient

logger = logging.getLogger(__name__)

# Limits for context assembly
_MAX_TOTAL_PATCH_CHARS = 40_000
_MAX_FILES_IN_CONTEXT = 30
_MAX_CLAUDE_MD_CHARS = 8_000
_MAX_DESCRIPTION_CHARS = 4_000


def _safe_float(value: object, default: float = 0.0) -> float:
    """Convert to float, returning *default* on failure."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError, OverflowError):
        return default


@dataclass(frozen=True)
class ReviewIssue:
    """A single issue found during code review."""

    severity: str  # "critical" | "major" | "minor"
    category: str  # "quality" | "contracts" | "architecture" | "correctness"
    file_path: str
    description: str
    suggestion: str


@dataclass(frozen=True)
class ReviewVerdict:
    """Result of a pre-merge code review."""

    decision: Literal["approve", "reject"]
    summary: str
    issues: tuple[ReviewIssue, ...]
    confidence: float
    cost_usd: float
    duration_seconds: float


def _extract_json(text: str) -> str | None:
    """Extract a JSON object from text with multiple strategies.

    Tries in order:
    1. Markdown code fence (```json ... ```)
    2. First ``{`` to last ``}`` substring
    """
    # Strategy 1: markdown code fence
    fence = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```",
        text,
        re.DOTALL,
    )
    if fence:
        return fence.group(1).strip()

    # Strategy 2: first { to last }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return text[first_brace : last_brace + 1]

    return None


def parse_verdict(
    raw_output: str,
    cost_usd: float,
    duration: float,
    *,
    fail_open: bool = False,
) -> ReviewVerdict:
    """Parse agent output into a ReviewVerdict.

    Args:
        raw_output: Raw text output from the review agent.
        cost_usd: Cost of the review agent run.
        duration: Duration of the review in seconds.
        fail_open: If True, return approve on parse errors
            (legacy behavior). If False, return reject.
    """
    fallback: Literal["approve", "reject"] = "approve" if fail_open else "reject"
    stripped = raw_output.strip()
    candidate = _extract_json(stripped) or stripped

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "Failed to parse review verdict JSON — %s",
            "fail-open approve" if fail_open else "fail-close reject",
        )
        label = "fail-open approve" if fail_open else "fail-close reject"
        return ReviewVerdict(
            decision=fallback,
            summary=f"Review parse error — {label}",
            issues=(),
            confidence=0.0,
            cost_usd=cost_usd,
            duration_seconds=duration,
        )

    decision = data.get("decision", "")
    if decision not in ("approve", "reject"):
        label = "fail-open approve" if fail_open else "fail-close reject"
        logger.warning(
            "Invalid review decision %r — %s",
            decision,
            label,
        )
        return ReviewVerdict(
            decision=fallback,
            summary=(f"Invalid decision {decision!r} — {label}"),
            issues=(),
            confidence=0.0,
            cost_usd=cost_usd,
            duration_seconds=duration,
        )

    raw_issues = data.get("issues", [])
    issues: list[ReviewIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        issues.append(
            ReviewIssue(
                severity=str(raw.get("severity", "major")),
                category=str(raw.get("category", "quality")),
                file_path=str(raw.get("file_path", "")),
                description=str(raw.get("description", "")),
                suggestion=str(raw.get("suggestion", "")),
            )
        )

    raw_confidence = _safe_float(data.get("confidence", 0.0))
    confidence = min(1.0, max(0.0, raw_confidence))

    return ReviewVerdict(
        decision=decision,
        summary=str(data.get("summary", "")),
        issues=tuple(issues),
        confidence=confidence,
        cost_usd=cost_usd,
        duration_seconds=duration,
    )


def assemble_context(
    details: PRDetails,
    files: list[PRFile],
    issue_key: str,
    issue_summary: str,
    issue_description: str,
    claude_md: str,
) -> str:
    """Assemble the review prompt from PR details, files, and task info."""
    sections: list[str] = []

    # Task requirements
    desc = issue_description[:_MAX_DESCRIPTION_CHARS]
    if len(issue_description) > _MAX_DESCRIPTION_CHARS:
        desc += "\n... [truncated]"
    sections.append(f"## Task Requirements\n**Issue:** {issue_key} — {issue_summary}\n\n{desc}")

    # Project conventions
    if claude_md:
        truncated = claude_md[:_MAX_CLAUDE_MD_CHARS]
        if len(claude_md) > _MAX_CLAUDE_MD_CHARS:
            truncated += "\n... [truncated]"
        sections.append(f"## Project Conventions\n{truncated}")

    # PR overview
    sections.append(
        f"## PR: {details.title}\n"
        f"{details.body}\n\n"
        f"Author: {details.author} | "
        f"Base: {details.base_branch} ← {details.head_branch}\n"
        f"Files changed: {details.changed_files} "
        f"(+{details.additions}, -{details.deletions})"
    )

    # Changed files with patches
    file_lines: list[str] = []
    total_chars = 0
    for idx, pr_file in enumerate(files):
        if idx >= _MAX_FILES_IN_CONTEXT:
            file_lines.append(f"\n... and {len(files) - idx} more file(s) truncated")
            break
        patch_text = pr_file.patch or "(binary or empty)"
        header = f"### {pr_file.filename} ({pr_file.status}, +{pr_file.additions}/-{pr_file.deletions})"
        entry = f"{header}\n```diff\n{patch_text}\n```"
        if total_chars + len(entry) > _MAX_TOTAL_PATCH_CHARS:
            remaining = len(files) - idx
            file_lines.append(
                f"\n... {remaining} more file(s) truncated (total patch size exceeded {_MAX_TOTAL_PATCH_CHARS} chars)"
            )
            break
        file_lines.append(entry)
        total_chars += len(entry)

    sections.append("## Changed Files\n" + "\n\n".join(file_lines))

    return "\n\n".join(sections)


_REVIEW_SYSTEM_PROMPT = """\
You are a code reviewer. Your job is to review a pull request \
and decide whether it should be approved or rejected.

## Review Criteria
1. **Code quality** — does the code follow project conventions?
2. **Contracts** — correct dependencies, no mocks in production code, \
proper imports
3. **Architecture** — follows project patterns, no circular dependencies
4. **Correctness** — does the implementation actually solve the task \
described in the issue?
5. **Tests** — are new features covered by tests?
6. **Security** — check for OWASP vulnerabilities:
   - SQL injection (string concatenation in queries instead of \
parameterized queries)
   - XSS (unsanitized user input rendered in templates or responses)
   - Command injection (subprocess/os.system calls with user input)
   - Hardcoded secrets, credentials, or API keys in source code
   - Insecure deserialization (pickle, yaml.load without SafeLoader)
   - Missing input validation at system boundaries (API endpoints, \
form handlers, CLI args)
   - Path traversal (user-controlled paths without sanitization)

## Rules
- "reject" ONLY for critical or major issues that would cause bugs, \
break contracts, violate architecture, or introduce security \
vulnerabilities
- "approve" for minor issues — mention them but don't block
- Be specific: cite file paths and line numbers
- Don't nitpick style if the project has a formatter

## Output Format
Respond with a single JSON object (no extra text outside the JSON):
```
{
  "decision": "approve" or "reject",
  "summary": "1-3 sentence overview",
  "issues": [
    {
      "severity": "critical" | "major" | "minor",
      "category": "quality" | "contracts" | "architecture" \
| "correctness" | "security",
      "file_path": "path/to/file.py",
      "description": "what is wrong",
      "suggestion": "how to fix it"
    }
  ],
  "confidence": 0.0 to 1.0
}
```
"""


class PreMergeReviewer:
    """Runs a one-shot sub-agent to review PR code before auto-merge."""

    def __init__(
        self,
        github: GitHubClient,
        tracker: TrackerClient,
        config: Config,
    ) -> None:
        self._github = github
        self._tracker = tracker
        self._config = config

    async def review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        issue_key: str,
        issue_summary: str,
        repo_paths: list[Path] | None = None,
    ) -> ReviewVerdict:
        """Run a one-shot review agent on the PR.

        Behavior on error depends on config
        ``pre_merge_review_fail_open``:
        - True: returns approve on any error (legacy).
        - False (default): returns reject on any error.

        Args:
            owner: GitHub repo owner.
            repo: GitHub repo name.
            pr_number: PR number.
            issue_key: Tracker issue key.
            issue_summary: Tracker issue summary.
            repo_paths: Optional list of local repo paths to find
                CLAUDE.md.
        """
        fail_open = self._config.pre_merge_review_fail_open
        start = time.monotonic()
        try:
            return await self._do_review(
                owner,
                repo,
                pr_number,
                issue_key,
                issue_summary,
                repo_paths,
                start,
            )
        except Exception:
            duration = time.monotonic() - start
            fallback: Literal["approve", "reject"] = "approve" if fail_open else "reject"
            label = "fail-open approve" if fail_open else "fail-close reject"
            logger.warning(
                "Pre-merge review failed for %s — %s",
                issue_key,
                label,
                exc_info=True,
            )
            return ReviewVerdict(
                decision=fallback,
                summary=f"Review error — {label}",
                issues=(),
                confidence=0.0,
                cost_usd=0.0,
                duration_seconds=duration,
            )

    async def _do_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        issue_key: str,
        issue_summary: str,
        repo_paths: list[Path] | None,
        start: float,
    ) -> ReviewVerdict:
        """Internal review logic — may raise on errors."""
        import asyncio

        # 1. Gather PR context (parallel — independent API calls)
        details, files = await asyncio.gather(
            asyncio.to_thread(
                self._github.get_pr_details,
                owner,
                repo,
                pr_number,
            ),
            asyncio.to_thread(
                self._github.get_pr_files,
                owner,
                repo,
                pr_number,
            ),
        )

        # 2. Gather task description (best-effort)
        issue_description = ""
        try:
            issue = await asyncio.to_thread(
                self._tracker.get_issue,
                issue_key,
            )
            issue_description = issue.description or ""
        except Exception:
            logger.warning(
                "Failed to fetch issue %s for review context",
                issue_key,
            )

        # 3. Load CLAUDE.md from repo paths (best-effort, blocking I/O
        #    but single small file — acceptable)
        claude_md = await asyncio.to_thread(
            load_claude_md,
            repo_paths,
        )

        # 4. Assemble prompt
        context = assemble_context(
            details=details,
            files=files,
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            claude_md=claude_md,
        )

        # 5. Run sub-agent
        raw_output, cost_usd = await self._run_review_agent(context)
        duration = time.monotonic() - start

        # 6. Parse verdict
        return parse_verdict(
            raw_output,
            cost_usd,
            duration,
            fail_open=self._config.pre_merge_review_fail_open,
        )

    async def _run_review_agent(
        self,
        prompt: str,
    ) -> tuple[str, float]:
        """Run the review sub-agent and return (output, cost_usd).

        Uses Claude Agent SDK with no MCP tools — pure reasoning.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
        )

        from orchestrator.agent_runner import receive_response_safe

        cfg = self._config
        options = ClaudeAgentOptions(
            model=cfg.pre_merge_review_model,
            system_prompt=_REVIEW_SYSTEM_PROMPT,
            permission_mode="bypassPermissions",
            max_budget_usd=cfg.pre_merge_review_max_budget_usd,
            allowed_tools=[],
            mcp_servers={},
            cwd=tempfile.gettempdir(),
            env=cfg.agent_env,
        )

        client = ClaudeSDKClient(options=options)
        output_parts: list[str] = []
        cost = 0.0

        async with client:
            await client.query(prompt)
            async for message in receive_response_safe(client):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            output_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0

        return "\n".join(output_parts), cost


def load_claude_md(repo_paths: list[Path] | None) -> str:
    """Load CLAUDE.md from the first repo path that has one."""
    if not repo_paths:
        return ""
    for repo_path in repo_paths:
        claude_md_path = repo_path / "CLAUDE.md"
        if claude_md_path.exists():
            try:
                return claude_md_path.read_text()
            except OSError:
                continue
    return ""
