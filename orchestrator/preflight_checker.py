"""Pre-dispatch evidence collector — gathers evidence for supervisor review."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from orchestrator.config import Config
    from orchestrator.github_client import GitHubClient
    from orchestrator.storage import Storage
    from orchestrator.tracker_client import TrackerClient

logger = logging.getLogger(__name__)

COMMENT_MAX_AGE_DAYS = 7
GIT_LOG_MAX_AGE_DAYS = 30

# Patterns indicating task is already implemented
_ALREADY_IMPLEMENTED_PATTERNS = (
    "задача уже реализована",
    "already implemented",
    "дубликат",
)


@dataclass(frozen=True)
class PreflightResult:
    """Result of preflight evidence collection.

    Attributes:
        reason: Human-readable summary of collected evidence.
        source: Origin of the evidence.
        needs_review: Evidence found, needs supervisor review.
        evidence: Collected evidence items for supervisor.
    """

    reason: str = ""
    source: str = ""
    needs_review: bool = False
    evidence: tuple[str, ...] = field(default_factory=tuple)


class PreflightChecker:
    """Collects evidence about prior implementation for supervisor review.

    Performs four checks (all sources, no short-circuit):
    1. Check task_runs SQLite table for successful completions
    2. Check Tracker comments for "already implemented" patterns
    3. Check git history for commits mentioning the task key
    4. Check GitHub for merged PRs mentioning the task key

    No automated skip decisions — evidence is presented to the
    supervisor who decides whether to dispatch or skip.

    All checks are graceful — errors are logged and skipped.
    """

    def __init__(
        self,
        tracker: TrackerClient,
        github: GitHubClient,
        config: Config,
        storage: Storage | None = None,
    ) -> None:
        self._tracker = tracker
        self._github = github
        self._config = config
        self._storage = storage
        self._review_approved: set[str] = set()

    def approve_for_dispatch(self, key: str) -> None:
        """Mark task as approved by supervisor.

        Bypasses evidence collection on next check().
        """
        self._review_approved.add(key)

    async def check(self, issue) -> PreflightResult:
        """Run evidence collection on an issue.

        Returns immediately if supervisor already approved.
        Otherwise collects evidence from all sources (no
        short-circuit) and returns needs_review=True when
        any evidence is found.
        """
        # Supervisor already approved — bypass all checks
        if issue.key in self._review_approved:
            self._review_approved.discard(issue.key)
            return PreflightResult()

        # Collect evidence from all sources
        evidence: list[str] = []

        task_run_ev = await self._check_task_runs(issue.key)
        if task_run_ev:
            evidence.append(task_run_ev)

        comment_ev = await self._check_comments(issue.key)
        if comment_ev:
            evidence.append(comment_ev)

        git_ev = await self._check_git_history(issue.key)
        if git_ev:
            evidence.append(git_ev)

        pr_ev = await self._check_merged_prs(issue.key)
        if pr_ev:
            evidence.append(pr_ev)

        if not evidence:
            return PreflightResult()

        return PreflightResult(
            needs_review=True,
            reason="; ".join(evidence),
            source="evidence_collector",
            evidence=tuple(evidence),
        )

    async def _check_task_runs(
        self,
        issue_key: str,
    ) -> str | None:
        """Check task_runs table for successful runs.

        Returns evidence string or None.
        A successful run with an unmerged PR returns None
        (agent needs to resume PR monitoring).
        """
        if self._storage is None:
            return None
        try:
            found = await self._storage.has_successful_task_run(
                issue_key,
            )
            if not found:
                return None
            # Successful run exists — but is there an unmerged PR?
            try:
                unmerged = await self._storage.has_unmerged_pr(
                    issue_key,
                )
            except Exception:
                logger.warning(
                    "Error checking unmerged PR for %s, allowing dispatch",
                    issue_key,
                    exc_info=True,
                )
                return None
            if unmerged:
                return None
            return "Successful task_run recorded (no unmerged PR)"
        except Exception:
            logger.warning(
                "Error checking task_runs for %s, allowing dispatch",
                issue_key,
                exc_info=True,
            )
            return None

    async def _check_comments(
        self,
        issue_key: str,
    ) -> str | None:
        """Check Tracker comments for implementation indicators.

        Returns evidence string or None.
        """
        try:
            comments = await asyncio.to_thread(
                self._tracker.get_comments,
                issue_key,
            )

            now = time.time()
            max_age_seconds = COMMENT_MAX_AGE_DAYS * 86400

            for comment in comments:
                text = comment.get("text", "").lower()
                created_at_str = comment.get("createdAt", "")

                if not any(p in text for p in _ALREADY_IMPLEMENTED_PATTERNS):
                    continue

                # Parse timestamp (ISO 8601 format)
                try:
                    normalized = created_at_str.replace(
                        "Z",
                        "+00:00",
                    )
                    if normalized.endswith("+0000") or normalized.endswith("-0000"):
                        normalized = normalized[:-2] + ":" + normalized[-2:]
                    created_at = datetime.fromisoformat(
                        normalized,
                    )
                    age_seconds = now - created_at.timestamp()

                    if age_seconds <= max_age_seconds:
                        date_str = created_at.strftime(
                            "%Y-%m-%d",
                        )
                        snippet = text[:80]
                        return f"Comment from {date_str}: '{snippet}'"
                except (ValueError, AttributeError):
                    logger.warning(
                        "Failed to parse comment timestamp: %r",
                        created_at_str,
                    )
                    continue

            return None

        except requests.RequestException:
            logger.warning(
                "Error checking comments for %s, allowing dispatch",
                issue_key,
                exc_info=True,
            )
            return None

    async def _check_git_history(
        self,
        issue_key: str,
    ) -> str | None:
        """Check git history for commits mentioning this task.

        Returns evidence string or None.
        If there is an unmerged PR for this task, git commits are
        expected (created by the agent) and are not evidence that
        the task is done.
        No LLM confirmation — just reports the commits found.
        """
        # Check for unmerged PR first — same logic as _check_task_runs
        if self._storage is not None:
            try:
                unmerged = await self._storage.has_unmerged_pr(
                    issue_key,
                )
                if unmerged:
                    return None
            except Exception:
                logger.warning(
                    "Error checking unmerged PR for %s in git history check, reporting evidence",
                    issue_key,
                    exc_info=True,
                )

        for repo_info in self._config.repos_config.all_repos:
            try:
                grep_pattern = rf"\b{re.escape(issue_key)}\b"
                result = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "git",
                        "log",
                        "--all",
                        "--extended-regexp",
                        f"--grep={grep_pattern}",
                        "--oneline",
                        f"--since={GIT_LOG_MAX_AGE_DAYS} days ago",
                    ],
                    cwd=repo_info.path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if result.returncode != 0:
                    continue

                git_output = result.stdout.strip()
                if not git_output:
                    continue

                # Truncate to first few lines
                lines = git_output.split("\n")
                summary = "; ".join(line[:80] for line in lines[:3])
                if len(lines) > 3:
                    summary += f" (+{len(lines) - 3} more)"
                return f"Git commits found: {summary}"

            except Exception:
                logger.warning(
                    "Error checking git history in repo %s for %s, continuing with other repos",
                    repo_info.path,
                    issue_key,
                    exc_info=True,
                )
                continue

        return None

    async def _check_merged_prs(
        self,
        issue_key: str,
    ) -> str | None:
        """Check GitHub for merged PRs mentioning this task.

        Returns evidence string or None.
        """
        orgs: set[str] = set()
        if self._config.repos_config.all_repos:
            for repo in self._config.repos_config.all_repos:
                match = re.search(
                    r"github\.com[:/]([^/]+)/",
                    repo.url,
                )
                if match:
                    orgs.add(match.group(1))

        for org in sorted(orgs):
            try:
                prs = await asyncio.to_thread(
                    self._github.search_prs,
                    issue_key,
                    org=org,
                    merged_only=True,
                )

                title_pattern = re.compile(
                    rf"\b{re.escape(issue_key)}\b",
                )
                for pr in prs:
                    if not pr.get("merged", False):
                        continue
                    title = pr.get("title", "")
                    if not title_pattern.search(title):
                        number = pr.get("number", "unknown")
                        logger.debug(
                            "Ignoring PR #%s (%s) — title does not contain %s",
                            number,
                            title[:60],
                            issue_key,
                        )
                        continue
                    number = pr.get("number", "unknown")
                    return f"Merged PR #{number}: {title[:60]}"

            except requests.RequestException:
                logger.warning(
                    "Error checking merged PRs in org %s for %s, continuing with other orgs",
                    org,
                    issue_key,
                    exc_info=True,
                )
                continue

        return None
