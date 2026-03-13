"""Post-merge verification sub-agent.

Waits for CI and K8s readiness after merge, then spawns a
one-shot sub-agent to verify the deployment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from orchestrator.constants import EventType

if TYPE_CHECKING:
    from orchestrator.config import Config
    from orchestrator.event_bus import EventBus
    from orchestrator.github_client import GitHubClient
    from orchestrator.k8s_client import K8sClient
    from orchestrator.storage import Storage
    from orchestrator.tracker_client import TrackerClient

logger = logging.getLogger(__name__)

_CI_POLL_INTERVAL = 15  # seconds between CI polls
_K8S_POLL_INTERVAL = 10  # seconds between K8s polls
_TERMINAL_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"},
)
_ACCEPTABLE_CONCLUSIONS = frozenset(
    {"SUCCESS", "NEUTRAL", "SKIPPED", "STALE"},
)
_MAX_DESCRIPTION_CHARS = 4_000


@dataclass(frozen=True)
class VerificationIssue:
    """A single issue found during verification."""

    category: str  # "api", "ui", "data", "performance"
    description: str
    evidence: str  # actual response/log


@dataclass(frozen=True)
class VerificationResult:
    """Result of a post-merge verification."""

    decision: Literal["pass", "fail", "skip"]
    summary: str
    checks_passed: bool
    k8s_ready: bool
    cost_usd: float
    duration_seconds: float
    issues: tuple[VerificationIssue, ...] = ()


def _extract_json(text: str) -> str | None:
    """Extract a JSON object from text.

    Tries in order:
    1. Markdown code fence (```json ... ```)
    2. First ``{`` to last ``}`` substring
    """
    fence = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```",
        text,
        re.DOTALL,
    )
    if fence:
        return fence.group(1).strip()

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return text[first_brace : last_brace + 1]

    return None


def parse_verification_result(
    raw_output: str,
    cost_usd: float,
    duration: float,
) -> VerificationResult:
    """Parse agent output into a VerificationResult.

    Returns skip on parse errors (fail-open).
    """
    stripped = raw_output.strip()
    candidate = _extract_json(stripped) or stripped

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse verification JSON — skip")
        return VerificationResult(
            decision="skip",
            summary="Verification parse error — skip",
            checks_passed=True,
            k8s_ready=True,
            cost_usd=cost_usd,
            duration_seconds=duration,
        )

    decision = data.get("decision", "")
    if decision not in ("pass", "fail"):
        logger.warning(
            "Invalid verification decision %r — skip",
            decision,
        )
        return VerificationResult(
            decision="skip",
            summary=f"Invalid decision {decision!r} — skip",
            checks_passed=True,
            k8s_ready=True,
            cost_usd=cost_usd,
            duration_seconds=duration,
        )

    raw_issues = data.get("issues", [])
    issues: list[VerificationIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        issues.append(
            VerificationIssue(
                category=str(raw.get("category", "")),
                description=str(raw.get("description", "")),
                evidence=str(raw.get("evidence", "")),
            )
        )

    return VerificationResult(
        decision=decision,
        summary=str(data.get("summary", "")),
        checks_passed=True,
        k8s_ready=True,
        cost_usd=cost_usd,
        duration_seconds=duration,
        issues=tuple(issues),
    )


def assemble_verification_context(
    issue_key: str,
    issue_summary: str,
    issue_description: str,
    merge_sha: str,
    env_config: dict | None,
) -> str:
    """Assemble context for the verification sub-agent."""
    sections: list[str] = []

    desc = issue_description[:_MAX_DESCRIPTION_CHARS]
    if len(issue_description) > _MAX_DESCRIPTION_CHARS:
        desc += "\n... [truncated]"
    sections.append(f"## Task\n**Issue:** {issue_key} — {issue_summary}\n\n{desc}")

    sections.append(f"## Merge\nCommit SHA: {merge_sha}")

    if env_config:
        sections.append(f"## Environment Configuration\n```json\n{json.dumps(env_config, indent=2)}\n```")
    else:
        sections.append("## Environment Configuration\nNo environment config available.")

    return "\n\n".join(sections)


_VERIFICATION_SYSTEM_PROMPT = """\
You are a post-merge verification agent. Your job is to \
verify that a recently merged change works correctly in \
the deployed environment.

## What You Receive
- Task description (what was changed)
- Merge commit SHA
- Environment config (URLs, endpoints)

## What You Do
1. Determine what needs verification based on the task
2. Make real HTTP requests to verify endpoints work
3. Check response codes and basic response shape
4. Report any issues found

## Output Format
Respond with a single JSON object (no extra text):
```
{
  "decision": "pass" or "fail",
  "summary": "1-3 sentence overview",
  "issues": [
    {
      "category": "api" | "ui" | "data" | "performance",
      "description": "what is wrong",
      "evidence": "actual response or error"
    }
  ]
}
```
"""


class PostMergeVerifier:
    """Runs post-merge verification after PR merge."""

    def __init__(
        self,
        github: GitHubClient,
        tracker: TrackerClient,
        k8s_client: K8sClient | None,
        storage: Storage | None,
        config: Config,
        event_bus: EventBus,
    ) -> None:
        self._github = github
        self._tracker = tracker
        self._k8s = k8s_client
        self._storage = storage
        self._config = config
        self._event_bus = event_bus

    async def verify(
        self,
        issue_key: str,
        owner: str,
        repo: str,
        pr_number: int,
        merge_sha: str,
        issue_summary: str,
        issue_description: str,
    ) -> VerificationResult:
        """Run post-merge verification.

        Returns a VerificationResult with the outcome.
        """
        cfg = self._config
        if not cfg.post_merge_verification_enabled:
            return VerificationResult(
                decision="skip",
                summary="Post-merge verification disabled",
                checks_passed=False,
                k8s_ready=False,
                cost_usd=0.0,
                duration_seconds=0.0,
            )

        start = time.monotonic()
        try:
            return await self._do_verify(
                issue_key=issue_key,
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                merge_sha=merge_sha,
                issue_summary=issue_summary,
                issue_description=issue_description,
                start=start,
            )
        except Exception:
            duration = time.monotonic() - start
            logger.warning(
                "Post-merge verification error for %s — skip",
                issue_key,
                exc_info=True,
            )
            return VerificationResult(
                decision="skip",
                summary="Verification error — skip",
                checks_passed=False,
                k8s_ready=False,
                cost_usd=0.0,
                duration_seconds=duration,
            )

    async def _do_verify(
        self,
        issue_key: str,
        owner: str,
        repo: str,
        pr_number: int,
        merge_sha: str,
        issue_summary: str,
        issue_description: str,
        start: float,
    ) -> VerificationResult:
        """Internal verification logic."""
        cfg = self._config

        # 1. Wait for CI
        checks_passed = True
        if cfg.post_merge_wait_for_ci:
            checks_passed = await self._wait_for_ci(
                owner,
                repo,
                merge_sha,
                cfg.post_merge_ci_timeout_seconds,
            )

        # 2. Wait for K8s
        k8s_ready = True
        if cfg.post_merge_wait_for_k8s and cfg.post_merge_k8s_deployment:
            k8s_ready = await self._wait_for_k8s(
                cfg.k8s_namespace,
                cfg.post_merge_k8s_deployment,
                cfg.post_merge_k8s_timeout_seconds,
            )

        # 3. Load environment config
        env_config = None
        if self._storage is not None:
            env_name = cfg.post_merge_verification_environment
            env_data = await self._storage.get_environment(env_name)
            env_config = env_data.get("config") if env_data else None

        # 4. Assemble context
        context = assemble_verification_context(
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            merge_sha=merge_sha,
            env_config=env_config,
        )

        # 5. Run sub-agent with dedicated timeout
        agent_timeout = cfg.post_merge_verification_timeout_seconds
        raw_output, cost_usd = await asyncio.wait_for(
            self._run_verification_agent(context),
            timeout=agent_timeout,
        )
        duration = time.monotonic() - start

        # 6. Parse result
        result = parse_verification_result(raw_output, cost_usd, duration)

        # Override checks_passed/k8s_ready from wait phase
        result = VerificationResult(
            decision=result.decision,
            summary=result.summary,
            checks_passed=checks_passed,
            k8s_ready=k8s_ready,
            cost_usd=result.cost_usd,
            duration_seconds=result.duration_seconds,
            issues=result.issues,
        )

        # 7. Publish events / create hotfix
        from orchestrator.event_bus import Event

        if result.decision == "fail":
            await self._create_hotfix_task(issue_key, issue_summary, result)
            await self._event_bus.publish(
                Event(
                    type=EventType.VERIFICATION_FAILED,
                    task_key=issue_key,
                    data={
                        "summary": result.summary,
                        "pr_number": pr_number,
                        "merge_sha": merge_sha,
                    },
                )
            )
        elif result.decision == "pass":
            await self._event_bus.publish(
                Event(
                    type=EventType.TASK_VERIFIED,
                    task_key=issue_key,
                    data={
                        "summary": result.summary,
                        "pr_number": pr_number,
                        "merge_sha": merge_sha,
                    },
                )
            )

        return result

    async def _wait_for_ci(
        self,
        owner: str,
        repo: str,
        sha: str,
        timeout_secs: int,
    ) -> bool:
        """Poll CI checks until all pass or timeout."""
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            checks = await asyncio.to_thread(
                self._github.get_commit_check_runs,
                owner,
                repo,
                sha,
            )
            # Only consider completed checks for pass/fail
            completed = [c for c in checks if c.status == "COMPLETED"]
            if completed and len(completed) == len(checks):
                # All checks completed — check results
                if all(c.conclusion in _ACCEPTABLE_CONCLUSIONS for c in completed):
                    logger.info("CI checks passed for %s", sha)
                    return True
                # At least one terminal failure
                failed = [c.name for c in completed if c.conclusion in _TERMINAL_CONCLUSIONS]
                if failed:
                    logger.warning(
                        "CI terminal failure for %s: %s",
                        sha,
                        ", ".join(failed),
                    )
                    return False
            await asyncio.sleep(_CI_POLL_INTERVAL)

        logger.warning("CI timeout after %ds for %s", timeout_secs, sha)
        return False

    async def _wait_for_k8s(
        self,
        namespace: str,
        deployment: str,
        timeout_secs: int,
    ) -> bool:
        """Poll K8s pods until ready or timeout."""
        if self._k8s is None:
            logger.warning("K8s client not available — skip wait")
            return True
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            pods = await asyncio.to_thread(
                self._k8s.list_pods,
                namespace,
            )
            # Filter pods by deployment name prefix (name-hash-hash)
            prefix = deployment + "-"
            matching = [p for p in pods if p.name.startswith(prefix)]
            if matching and all(p.phase == "Running" and all(c.ready for c in p.containers) for p in matching):
                logger.info(
                    "K8s pods ready for %s/%s",
                    namespace,
                    deployment,
                )
                return True
            await asyncio.sleep(_K8S_POLL_INTERVAL)

        logger.warning(
            "K8s timeout after %ds for %s/%s",
            timeout_secs,
            namespace,
            deployment,
        )
        return False

    async def _run_verification_agent(
        self,
        prompt: str,
    ) -> tuple[str, float]:
        """Run the verification sub-agent.

        Returns (output, cost_usd).
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
        )

        from orchestrator.agent_runner import (
            receive_response_safe,
        )

        cfg = self._config
        options = ClaudeAgentOptions(
            model=cfg.post_merge_verification_model,
            system_prompt=_VERIFICATION_SYSTEM_PROMPT,
            permission_mode="bypassPermissions",
            max_budget_usd=(cfg.post_merge_verification_max_budget_usd),
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
            async for msg in receive_response_safe(client):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            output_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    cost = getattr(msg, "total_cost_usd", 0.0) or 0.0

        return "\n".join(output_parts), cost

    async def _create_hotfix_task(
        self,
        issue_key: str,
        issue_summary: str,
        result: VerificationResult,
    ) -> None:
        """Create a hotfix Bug task in Tracker."""
        cfg = self._config
        issues_text = ""
        for issue in result.issues:
            issues_text += f"\n- [{issue.category}] {issue.description}\n  Evidence: {issue.evidence}"

        description = (
            f"Post-merge verification failed for "
            f"{issue_key}.\n\n"
            f"**Summary:** {result.summary}\n\n"
            f"**Issues found:**{issues_text}"
        )

        try:
            await asyncio.to_thread(
                self._tracker.create_issue,
                cfg.tracker_queue,
                f"[Hotfix] {issue_key}: {issue_summary}",
                description,
                issue_type=1,  # Bug
                tags=["ai-task", "hotfix"],
                parent=issue_key,
            )
            logger.info("Created hotfix task for %s", issue_key)
        except Exception:
            logger.warning(
                "Failed to create hotfix task for %s",
                issue_key,
                exc_info=True,
            )
