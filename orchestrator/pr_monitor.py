"""Monitor for PRs in review — tracks review comments and CI pipeline status."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import time
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import requests

if TYPE_CHECKING:
    from orchestrator.github_client import PRStatus

from orchestrator._persistence import BackgroundPersistenceMixin
from orchestrator.agent_runner import AgentSession
from orchestrator.constants import EventType, PRState, ResolutionType
from orchestrator.event_bus import Event
from orchestrator.prompt_builder import (
    build_merge_conflict_prompt,
    build_pipeline_failure_prompt,
    build_pre_merge_rejection_prompt,
    build_review_prompt,
)
from orchestrator.workspace_tools import WorkspaceState

if TYPE_CHECKING:
    from orchestrator.agent_mailbox import AgentMailbox
    from orchestrator.config import Config
    from orchestrator.event_bus import EventBus
    from orchestrator.github_client import GitHubClient
    from orchestrator.post_merge_verifier import PostMergeVerifier
    from orchestrator.pre_merge_reviewer import PreMergeReviewer, ReviewVerdict
    from orchestrator.proposal_manager import ProposalManager
    from orchestrator.storage import Storage
    from orchestrator.tracker_client import TrackerClient

logger = logging.getLogger(__name__)

_PR_PARSE_PATTERN = re.compile(r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)")


@dataclass
class TrackedPR:
    """A PR being monitored for review comments and CI pipeline status."""

    issue_key: str
    pr_url: str
    owner: str
    repo: str
    pr_number: int
    session: AgentSession
    workspace_state: WorkspaceState
    last_check_at: float
    issue_summary: str = ""
    seen_thread_ids: set[str] = field(default_factory=set)
    seen_failed_checks: set[str] = field(default_factory=set)
    seen_merge_conflict: bool = False
    merge_conflict_retries: int = 0
    merge_conflict_head_sha: str = ""
    auto_merge_attempted: bool = False
    human_gate_passed: bool = False
    # Review state is ephemeral — not persisted to storage.
    # On restart, review will be re-requested (harmless, costs extra).
    pre_merge_review_requested: bool = False
    pre_merge_review_verdict: Literal["approve", "reject"] | None = None
    last_seen_head_sha: str = ""
    # Tracks whether bot posted REQUEST_CHANGES on GitHub.
    # NOT reset by reset_review_flags — it reflects GitHub state,
    # not internal review state.  Cleared when APPROVE is posted
    # to dismiss the stale review.
    request_changes_posted: bool = False
    removed: bool = False

    def reset_review_flags(self) -> None:
        """Reset review and merge flags for a fresh review cycle."""
        self.pre_merge_review_requested = False
        self.pre_merge_review_verdict = None
        self.auto_merge_attempted = False
        self.human_gate_passed = False


def find_pr_url_in_comments(
    tracker: TrackerClient,
    issue_key: str,
) -> str | None:
    """Scan task comments for a GitHub PR URL.

    Standalone version for use outside PRMonitor (e.g., orphan
    reconciliation at startup before monitors are initialized).
    """
    try:
        comments = tracker.get_comments(issue_key)
    except requests.RequestException:
        return None
    for comment in reversed(comments):  # newest first
        text = comment.get("text", "")
        match = _PR_PARSE_PATTERN.search(text)
        if match:
            return match.group(0)
    return None


def parse_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    """Extract (owner, repo, pr_number) from a GitHub PR URL."""
    match = _PR_PARSE_PATTERN.search(pr_url)
    if not match:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    pr_number = int(match.group("number"))
    return (owner, repo, pr_number)


class _PersistedDedup(typing.NamedTuple):
    """Persisted dedup state restored from storage."""

    seen_thread_ids: set[str]
    seen_failed_checks: set[str]
    session_id: str | None
    seen_merge_conflict: bool
    merge_conflict_retries: int = 0
    merge_conflict_head_sha: str = ""


# Type alias for callbacks
CleanupWorktreesCallback = Callable[[str, list[Path]], None]


class PRMonitor(BackgroundPersistenceMixin):
    """Monitors PRs in review for unresolved review conversations and CI failures."""

    def __init__(
        self,
        tracker: TrackerClient,
        github: GitHubClient,
        event_bus: EventBus,
        proposal_manager: ProposalManager,
        config: Config,
        semaphore: asyncio.Semaphore,
        session_locks: dict[str, asyncio.Lock],
        shutdown_event: asyncio.Event,
        cleanup_worktrees_callback: CleanupWorktreesCallback,
        dispatched_set: set[str] | None = None,
        storage: Storage | None = None,
        mailbox: AgentMailbox | None = None,
        reviewer: PreMergeReviewer | None = None,
        verifier: PostMergeVerifier | None = None,
    ) -> None:
        self._tracker = tracker
        self._github = github
        self._event_bus = event_bus
        self._proposal_manager = proposal_manager
        self._config = config
        self._semaphore = semaphore
        self._session_locks = session_locks
        self._shutdown = shutdown_event
        self._cleanup_worktrees = cleanup_worktrees_callback
        self._dispatched = dispatched_set if dispatched_set is not None else set()
        self._storage = storage
        self._mailbox = mailbox
        self._reviewer = reviewer
        self._verifier: PostMergeVerifier | None = verifier
        self._tracked_prs: dict[str, TrackedPR] = {}
        self._persisted_dedup: dict[str, _PersistedDedup] = {}
        # Keyed by issue_key so we can check for in-flight reviews
        # and cancel stale ones on new commits.
        self._review_tasks: dict[str, asyncio.Task[None]] = {}
        self._init_persistence()

    def get_tracked(self) -> dict[str, TrackedPR]:
        """Get active tracked PRs (excludes terminal)."""
        return {k: pr for k, pr in self._tracked_prs.items() if not pr.removed}

    def set_verifier(
        self,
        verifier: PostMergeVerifier,
    ) -> None:
        """Set the post-merge verifier (late injection)."""
        self._verifier = verifier

    async def load(self) -> None:
        """Load seen_thread_ids and seen_failed_checks from storage.

        This restores dedup state only. TrackedPR objects with sessions
        are NOT restored — they will be recreated on PR resume.
        The loaded data is applied when track() is called for a restored PR.
        """
        if not self._storage:
            return
        try:
            records = await self._storage.load_pr_tracking()
            self._persisted_dedup.clear()
            for rec in records:
                self._persisted_dedup[rec.task_key] = _PersistedDedup(
                    seen_thread_ids=set(rec.seen_thread_ids),
                    seen_failed_checks=set(rec.seen_failed_checks),
                    session_id=rec.session_id,
                    seen_merge_conflict=rec.seen_merge_conflict,
                    merge_conflict_retries=rec.merge_conflict_retries,
                    merge_conflict_head_sha=rec.merge_conflict_head_sha,
                )
            if records:
                logger.info("Loaded PR tracking dedup for %d PRs from storage", len(records))
        except Exception:
            logger.warning("Failed to load PR tracking from storage", exc_info=True)

    def _persist_tracking(self, issue_key: str) -> None:
        """Schedule async persistence of PR tracking dedup data."""
        if not self._storage:
            return

        from orchestrator.stats_models import PRTrackingData

        pr = self._tracked_prs.get(issue_key)
        if not pr:
            return

        data = PRTrackingData(
            task_key=issue_key,
            pr_url=pr.pr_url,
            issue_summary=pr.issue_summary,
            seen_thread_ids=sorted(pr.seen_thread_ids),
            seen_failed_checks=sorted(pr.seen_failed_checks),
            session_id=pr.session.session_id,
            seen_merge_conflict=pr.seen_merge_conflict,
            merge_conflict_retries=pr.merge_conflict_retries,
            merge_conflict_head_sha=pr.merge_conflict_head_sha,
        )

        async def _write() -> None:
            async with self._key_locks[issue_key]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.upsert_pr_tracking(data)
                except Exception:
                    logger.warning("Failed to persist PR tracking for %s", issue_key, exc_info=True)

        self._schedule_task(_write())

    def _persist_delete_tracking(self, issue_key: str) -> None:
        """Schedule async deletion of PR tracking data."""
        if not self._storage:
            return

        async def _delete() -> None:
            async with self._key_locks[issue_key]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.delete_pr_tracking(issue_key)
                except Exception:
                    logger.warning("Failed to delete PR tracking for %s", issue_key, exc_info=True)

        self._schedule_task(_delete())

    def track(
        self,
        issue_key: str,
        pr_url: str,
        session: AgentSession,
        workspace_state: WorkspaceState,
        issue_summary: str | None = None,
    ) -> None:
        """Start tracking a PR for review monitoring."""
        parsed = parse_pr_url(pr_url)
        if not parsed:
            logger.warning("Could not parse PR URL: %s", pr_url)
            return
        owner, repo, pr_number = parsed
        tracked = TrackedPR(
            issue_key=issue_key,
            issue_summary=issue_summary or issue_key,
            pr_url=pr_url,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            session=session,
            workspace_state=workspace_state,
            last_check_at=time.monotonic(),
        )
        # Restore persisted dedup data if available
        persisted = self._persisted_dedup.get(issue_key)
        if persisted:
            tracked.seen_thread_ids = persisted.seen_thread_ids
            tracked.seen_failed_checks = persisted.seen_failed_checks
            tracked.seen_merge_conflict = persisted.seen_merge_conflict
            tracked.merge_conflict_retries = persisted.merge_conflict_retries
            tracked.merge_conflict_head_sha = persisted.merge_conflict_head_sha
            del self._persisted_dedup[issue_key]
        self._tracked_prs[issue_key] = tracked
        self._persist_tracking(issue_key)

    def get_persisted_session_id(self, issue_key: str) -> str | None:
        """Get persisted session_id for an issue (for session resumption after restart)."""
        persisted = self._persisted_dedup.get(issue_key)
        if persisted:
            return persisted.session_id
        return None

    def find_existing_pr(self, issue_key: str) -> str | None:
        """Scan task comments for a GitHub PR URL."""
        return find_pr_url_in_comments(self._tracker, issue_key)

    async def remove(self, issue_key: str) -> None:
        """Remove a tracked PR, close session, clean up."""
        lock = self._session_locks.get(issue_key)
        if lock is not None:
            await lock.acquire()
        try:
            pr = self._tracked_prs.pop(issue_key, None)
            if pr is None:
                return
            pr.removed = True
            self._cancel_review_task(issue_key)
            await pr.session.close()
        finally:
            if lock is not None:
                lock.release()
            self._session_locks.pop(issue_key, None)
        self._cleanup_worktrees(
            issue_key,
            pr.workspace_state.repo_paths,
        )
        self._persist_delete_tracking(issue_key)
        if self._mailbox is not None:
            await self._mailbox.unregister_agent(issue_key)

    async def cleanup(self, issue_key: str) -> None:
        """Remove a tracked PR and clean up its worktrees."""
        self._cancel_review_task(issue_key)
        pr = self._tracked_prs.pop(issue_key, None)
        if pr:
            pr.removed = True
            self._cleanup_worktrees(issue_key, pr.workspace_state.repo_paths)
            self._persist_delete_tracking(issue_key)
        if self._mailbox is not None:
            await self._mailbox.unregister_agent(issue_key)
        self._dispatched.discard(issue_key)
        self._session_locks.pop(issue_key, None)

    def _cancel_review_task(self, issue_key: str) -> None:
        """Cancel an in-flight review task for *issue_key* (if any)."""
        task = self._review_tasks.pop(issue_key, None)
        if task is not None and not task.done():
            task.cancel()

    async def close_all(self) -> None:
        """Close all tracked sessions (for graceful shutdown)."""
        # Cancel in-flight review tasks and await completion.
        # Snapshot values to avoid dict-modified-during-iteration:
        # done-callbacks pop from _review_tasks concurrently.
        review_tasks = list(self._review_tasks.values())
        for task in review_tasks:
            task.cancel()
        if review_tasks:
            await asyncio.gather(
                *review_tasks,
                return_exceptions=True,
            )
        self._review_tasks.clear()

        # Capture keys before clearing for session lock cleanup
        tracked_keys = list(self._tracked_prs.keys())
        for key, pr in self._tracked_prs.items():
            await pr.session.close()
            if self._mailbox is not None:
                await self._mailbox.unregister_agent(key)
        self._tracked_prs.clear()
        # Clean up session locks for all tracked PRs
        for key in tracked_keys:
            self._session_locks.pop(key, None)

    async def watch(self) -> None:
        """Background watcher loop — monitors PRs for review comments and CI failures."""
        while True:
            await self._check_all()

            if self._shutdown.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._config.review_check_delay_seconds,
                )
            except TimeoutError:
                pass
            if self._shutdown.is_set():
                break

    async def _check_all(self) -> None:
        """Single pass over tracked PRs — check status and reviews."""
        for issue_key, pr in list(self._tracked_prs.items()):
            # Rate limit: don't check too frequently
            elapsed = time.monotonic() - pr.last_check_at
            if elapsed < self._config.review_check_delay_seconds:
                continue

            pr.last_check_at = time.monotonic()

            try:
                status = await asyncio.to_thread(self._github.get_pr_status, pr.owner, pr.repo, pr.pr_number)
            except requests.RequestException:
                logger.warning("Failed to check PR status for %s", issue_key)
                continue

            # Detect new commits — reset review/merge flags so the PR
            # gets a fresh review cycle after the worker pushes a fix.
            # Cancel any in-flight review task to avoid a stale verdict
            # overwriting the reset flags after we clear them.
            if (
                status.head_sha
                and pr.last_seen_head_sha
                and status.head_sha != pr.last_seen_head_sha
                and (pr.pre_merge_review_requested or pr.auto_merge_attempted)
            ):
                logger.info(
                    "New commit on %s (SHA %s → %s) — resetting review/merge flags",
                    issue_key,
                    pr.last_seen_head_sha[:8],
                    status.head_sha[:8],
                )
                self._cancel_review_task(issue_key)
                pr.reset_review_flags()
            # Empty string on first poll — intentional sentinel so the
            # reset logic above is skipped (no previous SHA to compare).
            if status.head_sha:
                pr.last_seen_head_sha = status.head_sha

            # Handle closed/merged PR
            if status.state != PRState.OPEN:
                await self._handle_pr_closed_or_merged(issue_key, pr, status.state)
                continue

            # Process merge conflicts
            await self._process_merge_conflicts(issue_key, pr, status)

            # Process unresolved review threads
            await self._process_review_threads(issue_key, pr)

            # Process failed CI checks
            await self._process_failed_checks(issue_key, pr)

            # Process auto-merge
            await self._process_auto_merge(issue_key, pr, status)

    async def _process_merge_conflicts(self, issue_key: str, pr: TrackedPR, status: PRStatus) -> None:
        """Process merge conflict state for a tracked PR.

        Sends a merge conflict prompt when CONFLICTING is detected.
        Retries up to merge_conflict_max_retries when the agent
        pushes a new commit (head_sha changes) but conflict persists.
        Resets all state when PR becomes MERGEABLE again.
        UNKNOWN state is ignored.
        """
        if pr.removed:
            return
        mergeable = status.mergeable

        if mergeable == "MERGEABLE":
            if pr.seen_merge_conflict:
                pr.seen_merge_conflict = False
                pr.merge_conflict_retries = 0
                pr.merge_conflict_head_sha = ""
                self._persist_tracking(issue_key)
            return

        if mergeable != "CONFLICTING":
            # UNKNOWN or empty — ignore
            return

        if pr.seen_merge_conflict:
            max_retries = self._config.merge_conflict_max_retries
            if pr.merge_conflict_retries >= max_retries:
                return  # exhausted
            if not status.head_sha or status.head_sha == pr.merge_conflict_head_sha:
                return  # same SHA — agent hasn't pushed yet
            pr.merge_conflict_retries += 1
            # Fall through to re-send prompt

        pr.seen_merge_conflict = True
        pr.merge_conflict_head_sha = status.head_sha or ""
        self._persist_tracking(issue_key)

        logger.info("Merge conflict detected for %s (%s)", issue_key, pr.pr_url)

        await self._event_bus.publish(
            Event(
                type=EventType.MERGE_CONFLICT,
                task_key=issue_key,
                data={"pr_url": pr.pr_url},
            )
        )

        prompt = build_merge_conflict_prompt(issue_key, pr.pr_url)
        try:
            lock = self._session_locks.setdefault(issue_key, asyncio.Lock())
            async with lock:
                async with self._semaphore:
                    result = await pr.session.send(prompt)
                    result = await pr.session.drain_pending_messages(result)
        except Exception:
            logger.warning(
                "Failed to send merge conflict prompt to agent for %s",
                issue_key,
                exc_info=True,
            )
            return

        if result.proposals:
            await self._proposal_manager.process_proposals(issue_key, result.proposals)

        pr.last_check_at = time.monotonic()
        self._persist_tracking(issue_key)

        if result.success:
            logger.info("Agent addressed merge conflicts for %s", issue_key)
        else:
            logger.warning(
                "Agent failed to address merge conflicts for %s: %s",
                issue_key,
                result.output[:200],
            )

    async def _close_orphaned_subtasks(self, parent_key: str, pr_number: int) -> None:
        """Close subtasks that are superseded by parent task completion.

        When a PR is merged, the parent task is complete. Any linked subtasks
        that are still open should be auto-closed to prevent wasted dispatch cycles.

        Args:
            parent_key: The parent task key (e.g., "QR-167")
            pr_number: The PR number for context in closure comments
        """
        from orchestrator.epic_coordinator import EpicCoordinator

        try:
            links = await asyncio.to_thread(self._tracker.get_links, parent_key)
        except requests.RequestException:
            logger.warning(
                "Failed to fetch links for %s — cannot auto-close subtasks",
                parent_key,
                exc_info=True,
            )
            return

        # Extract child task keys from links (no fallback — strict filtering)
        child_keys = EpicCoordinator.extract_child_keys_strict(links, parent_key)

        if not child_keys:
            logger.debug("No subtask links found for %s", parent_key)
            return

        logger.info(
            "Found %d potential subtask(s) for %s: %s",
            len(child_keys),
            parent_key,
            child_keys,
        )

        # Close each non-resolved subtask
        for child_key in child_keys:
            try:
                issue = await asyncio.to_thread(self._tracker.get_issue, child_key)

                # Skip if cancelled (check before resolved, canonical pattern)
                if EpicCoordinator.is_cancelled_status(issue.status):
                    logger.debug("Subtask %s is cancelled (%s) — skipping", child_key, issue.status)
                    continue

                # Skip if already resolved
                if EpicCoordinator.is_resolved_status(issue.status):
                    logger.debug("Subtask %s already resolved (%s) — skipping", child_key, issue.status)
                    continue

                # Add comment explaining auto-closure
                comment = (
                    f"Задача закрыта автоматически: родительская задача {parent_key} "
                    f"завершена (PR #{pr_number} смержен). Весь scope реализован "
                    f"в рамках родительской задачи."
                )
                await asyncio.to_thread(self._tracker.add_comment, child_key, comment)

                # Close the subtask
                await asyncio.to_thread(
                    self._tracker.transition_to_closed,
                    child_key,
                    resolution=ResolutionType.FIXED,
                    comment=None,  # Comment already added above
                )

                # Remove from dispatched set to free the slot
                self._dispatched.discard(child_key)

                logger.info(
                    "Auto-closed orphaned subtask %s (parent %s merged)",
                    child_key,
                    parent_key,
                )

            except requests.RequestException:
                # Log but continue — failure to close one subtask shouldn't block others
                logger.warning(
                    "Failed to auto-close subtask %s (parent %s) — continuing",
                    child_key,
                    parent_key,
                    exc_info=True,
                )

    async def _handle_pr_closed_or_merged(self, issue_key: str, pr: TrackedPR, state: str) -> None:
        """Handle a PR that has been closed or merged."""
        pr.removed = True
        self._cancel_review_task(issue_key)
        logger.info("PR %s is %s — cleaning up", pr.pr_url, state)
        try:
            if state == PRState.MERGED:
                try:
                    await asyncio.to_thread(
                        lambda: self._tracker.transition_to_closed(
                            issue_key,
                            resolution=ResolutionType.FIXED,
                            comment=f"PR merged: {pr.pr_url}",
                        )
                    )
                except requests.RequestException:
                    logger.warning(
                        "Failed to close %s after merge",
                        issue_key,
                    )

                # Auto-close orphaned subtasks after parent is closed
                await self._close_orphaned_subtasks(
                    issue_key,
                    pr.pr_number,
                )

                await self._event_bus.publish(
                    Event(
                        type=EventType.PR_MERGED,
                        task_key=issue_key,
                        data={"pr_url": pr.pr_url},
                    )
                )

                # Fire-and-forget post-merge verification
                if self._verifier and self._config.post_merge_verification_enabled:
                    # Use actual merge commit SHA, not PR head SHA
                    merge_sha = await asyncio.to_thread(
                        self._github.get_merge_commit_sha,
                        pr.owner,
                        pr.repo,
                        pr.pr_number,
                    )
                    if not merge_sha:
                        merge_sha = pr.last_seen_head_sha
                    self._schedule_task(
                        self._run_post_merge_verification(
                            pr,
                            merge_sha,
                        )
                    )
            await pr.session.close()
        finally:
            await self.cleanup(issue_key)

    async def _run_post_merge_verification(
        self,
        pr: TrackedPR,
        merge_sha: str,
    ) -> None:
        """Run post-merge verification (fire-and-forget).

        Exceptions are logged but never propagate.
        """
        if self._verifier is None:
            return
        try:
            # Fetch issue description from Tracker
            issue_desc = ""
            try:
                issue = await asyncio.to_thread(
                    self._tracker.get_issue,
                    pr.issue_key,
                )
                issue_desc = issue.description or ""
            except Exception:
                logger.warning(
                    "Failed to fetch description for %s",
                    pr.issue_key,
                )

            result = await self._verifier.verify(
                issue_key=pr.issue_key,
                owner=pr.owner,
                repo=pr.repo,
                pr_number=pr.pr_number,
                merge_sha=merge_sha,
                issue_summary=pr.issue_summary,
                issue_description=issue_desc,
            )
            logger.info(
                "Post-merge verification for %s: %s — %s",
                pr.issue_key,
                result.decision,
                result.summary,
            )
        except Exception:
            logger.warning(
                "Post-merge verification error for %s",
                pr.issue_key,
                exc_info=True,
            )

    async def _process_review_threads(self, issue_key: str, pr: TrackedPR) -> None:
        """Process unresolved review threads for a tracked PR."""
        if pr.removed:
            return
        try:
            threads = await asyncio.to_thread(self._github.get_unresolved_threads, pr.owner, pr.repo, pr.pr_number)
        except requests.RequestException:
            logger.warning("Failed to get reviews for %s", issue_key)
            return

        # Filter: only threads we haven't processed yet
        new_threads = [t for t in threads if t.id not in pr.seen_thread_ids]
        if not new_threads:
            return

        # Send reviews to agent in the SAME session
        logger.info(
            "Sending %d unresolved conversation(s) to agent for %s",
            len(new_threads),
            issue_key,
        )
        prompt = build_review_prompt(issue_key, pr.pr_url, new_threads)
        await self._event_bus.publish(
            Event(
                type=EventType.REVIEW_SENT,
                task_key=issue_key,
                data={"thread_count": len(new_threads), "pr_url": pr.pr_url},
            )
        )

        # Mark threads as seen BEFORE sending to agent
        pr.seen_thread_ids.update(t.id for t in new_threads)

        try:
            lock = self._session_locks.setdefault(issue_key, asyncio.Lock())
            async with lock:
                async with self._semaphore:
                    result = await pr.session.send(prompt)
                    result = await pr.session.drain_pending_messages(result)
        except Exception:
            logger.warning(
                "Failed to send review prompt to agent for %s (threads marked as seen)",
                issue_key,
                exc_info=True,
            )
            self._persist_tracking(issue_key)
            return

        if result.proposals:
            await self._proposal_manager.process_proposals(issue_key, result.proposals)

        pr.last_check_at = time.monotonic()
        self._persist_tracking(issue_key)

        if result.success:
            logger.info("Agent addressed reviews for %s", issue_key)
        else:
            logger.warning(
                "Agent failed to address reviews for %s: %s",
                issue_key,
                result.output[:200],
            )

    async def _process_failed_checks(self, issue_key: str, pr: TrackedPR) -> None:
        """Process failed CI checks for a tracked PR."""
        if pr.removed:
            return
        try:
            failed_checks = await asyncio.to_thread(self._github.get_failed_checks, pr.owner, pr.repo, pr.pr_number)
        except requests.RequestException:
            logger.warning("Failed to get CI checks for %s", issue_key)
            return

        # Filter: only failures we haven't reported yet (keyed by check name + conclusion)
        new_failures = [c for c in failed_checks if f"{c.name}:{c.conclusion}" not in pr.seen_failed_checks]
        if not new_failures:
            return

        logger.info(
            "Sending %d failed CI check(s) to agent for %s",
            len(new_failures),
            issue_key,
        )
        pipeline_prompt = build_pipeline_failure_prompt(issue_key, pr.pr_url, new_failures)
        await self._event_bus.publish(
            Event(
                type=EventType.PIPELINE_FAILED,
                task_key=issue_key,
                data={
                    "check_count": len(new_failures),
                    "checks": [c.name for c in new_failures],
                    "pr_url": pr.pr_url,
                },
            )
        )

        # Mark as seen BEFORE sending to agent (dedup based on detection, not delivery)
        pr.seen_failed_checks.update(f"{c.name}:{c.conclusion}" for c in new_failures)

        # Wrap session.send in try/except — failures already marked as "seen"
        try:
            lock = self._session_locks.setdefault(issue_key, asyncio.Lock())
            async with lock:
                async with self._semaphore:
                    result = await pr.session.send(pipeline_prompt)
                    result = await pr.session.drain_pending_messages(result)
        except Exception:
            logger.warning(
                "Failed to send CI failure prompt to agent for %s (failures marked as seen)",
                issue_key,
                exc_info=True,
            )
            self._persist_tracking(issue_key)
            return

        if result.proposals:
            await self._proposal_manager.process_proposals(issue_key, result.proposals)

        pr.last_check_at = time.monotonic()

        if result.success:
            # Clear seen failures after successful fix — new push will
            # trigger new CI runs with fresh check results
            pr.seen_failed_checks.clear()
            # Cancel stale review before resetting flags — prevents a
            # running review sub-agent from overwriting the reset.
            self._cancel_review_task(issue_key)
            # Reset review/merge flags — new push needs fresh review cycle
            pr.reset_review_flags()
            self._persist_tracking(issue_key)
            logger.info("Agent addressed CI failures for %s", issue_key)
        else:
            self._persist_tracking(issue_key)
            logger.warning(
                "Agent failed to address CI failures for %s: %s",
                issue_key,
                result.output[:200],
            )

    async def _check_human_gate(
        self,
        tracked: TrackedPR,
    ) -> tuple[bool, str]:
        """Check if a PR should be blocked by the human gate.

        Returns:
            (should_block, reason) — reason is empty when not blocked.
        """
        threshold = self._config.human_gate_max_diff_lines
        patterns = self._config.human_gate_sensitive_path_list

        # Gate disabled when threshold is 0 and no patterns
        if threshold <= 0 and not patterns:
            return False, ""

        files = await asyncio.to_thread(
            self._github.get_pr_files,
            tracked.owner,
            tracked.repo,
            tracked.pr_number,
        )

        # Check diff size
        if threshold > 0:
            total_lines = sum(f.additions + f.deletions for f in files)
            if total_lines > threshold:
                return (
                    True,
                    f"Diff size {total_lines} lines exceeds threshold of {threshold}",
                )

        # Check sensitive paths
        if patterns:
            for f in files:
                for pat in patterns:
                    if fnmatch.fnmatch(f.filename, pat):
                        return (
                            True,
                            f"Sensitive path matched: {f.filename} (pattern: {pat})",
                        )

        return False, ""

    async def _process_auto_merge(
        self,
        issue_key: str,
        pr: TrackedPR,
        status: PRStatus,
    ) -> None:
        """Attempt auto-merge when all conditions are met."""
        if pr.removed:
            return
        if not self._config.auto_merge_enabled:
            return

        if pr.auto_merge_attempted:
            return

        # Human gate: block auto-merge for large/sensitive PRs.
        # Skip re-check if already passed (avoids redundant API call).
        if not pr.human_gate_passed:
            try:
                blocked, reason = await self._check_human_gate(
                    pr,
                )
            except Exception:
                logger.warning(
                    "Human gate check failed for %s — skipping gate",
                    issue_key,
                    exc_info=True,
                )
                blocked = False
                reason = ""
        else:
            blocked = False
            reason = ""

        if blocked:
            logger.warning(
                "Human gate blocked auto-merge for %s: %s",
                issue_key,
                reason,
            )
            await self._event_bus.publish(
                Event(
                    type=EventType.HUMAN_GATE_TRIGGERED,
                    task_key=issue_key,
                    data={
                        "pr_url": pr.pr_url,
                        "reason": reason,
                    },
                )
            )
            if self._config.human_gate_notify_comment:
                comment_body = (
                    "## Human Gate — Auto-merge Blocked\n\n"
                    f"**Reason:** {reason}\n\n"
                    "This PR requires manual review and merge."
                )
                await asyncio.to_thread(
                    self._github.post_review,
                    pr.owner,
                    pr.repo,
                    pr.pr_number,
                    body=comment_body,
                    event="COMMENT",
                )
            pr.auto_merge_attempted = True
            return

        pr.human_gate_passed = True

        # Pre-merge review gate: check verdict BEFORE calling
        # check_merge_readiness to avoid a wasteful GraphQL call
        # every cycle while waiting for the review sub-agent.
        if self._config.pre_merge_review_enabled and self._reviewer:
            if not pr.pre_merge_review_requested:
                # First encounter — request review; skip merge
                # readiness check since we need the verdict first.
                await self._request_pre_merge_review(
                    issue_key,
                    pr,
                )
                return  # check verdict on next cycle
            if pr.pre_merge_review_verdict == "reject":
                pr.auto_merge_attempted = True
                return
            if pr.pre_merge_review_verdict != "approve":
                return  # still waiting for verdict

            # Dismiss stale REQUEST_CHANGES if bot previously
            # posted one — otherwise GitHub reviewDecision stays
            # CHANGES_REQUESTED and blocks auto-merge.
            if pr.request_changes_posted:
                await self._dismiss_stale_request_changes(
                    issue_key,
                    pr,
                )

        readiness = await asyncio.to_thread(
            self._github.check_merge_readiness,
            pr.owner,
            pr.repo,
            pr.pr_number,
        )

        if not readiness.is_ready:
            # After restart the ephemeral request_changes_posted
            # flag is lost, but GitHub may still report
            # CHANGES_REQUESTED from our bot's stale review.
            # If internal verdict approved and the only blocker
            # is the review decision — dismiss and re-check.
            if (
                pr.pre_merge_review_verdict == "approve"
                and readiness.review_decision == "CHANGES_REQUESTED"
                and readiness.mergeable == "MERGEABLE"
                and not readiness.has_failed_checks
                and not readiness.has_pending_checks
                and not readiness.has_unresolved_threads
            ):
                await self._dismiss_stale_request_changes(
                    issue_key,
                    pr,
                )
                # Re-check after dismissing
                readiness = await asyncio.to_thread(
                    self._github.check_merge_readiness,
                    pr.owner,
                    pr.repo,
                    pr.pr_number,
                )
                if not readiness.is_ready:
                    return
            else:
                return

        # Require approval if configured (redundant with is_ready
        # for CHANGES_REQUESTED, but guards the case where
        # review_decision is "" — no reviews submitted yet).
        if self._config.auto_merge_require_approval and readiness.review_decision != "APPROVED":
            return

        pr.auto_merge_attempted = True

        if pr.removed:
            return  # type: ignore[unreachable]
        method = self._config.auto_merge_method.upper()
        success = await asyncio.to_thread(
            self._github.enable_auto_merge,
            pr.owner,
            pr.repo,
            pr.pr_number,
            method,
        )

        if success:
            await self._event_bus.publish(
                Event(
                    type=EventType.PR_AUTO_MERGE_ENABLED,
                    task_key=issue_key,
                    data={"pr_url": pr.pr_url},
                )
            )
            logger.info(
                "Auto-merge enabled for %s (%s)",
                issue_key,
                pr.pr_url,
            )
            return

        # Fallback: direct merge (REST expects lowercase method)
        if pr.removed:
            return  # type: ignore[unreachable]
        merged = await asyncio.to_thread(
            self._github.merge_pr,
            pr.owner,
            pr.repo,
            pr.pr_number,
            self._config.auto_merge_method.lower(),
        )

        if merged:
            await self._event_bus.publish(
                Event(
                    type=EventType.PR_DIRECT_MERGED,
                    task_key=issue_key,
                    data={
                        "pr_url": pr.pr_url,
                        "method": "direct",
                        "merged": True,
                    },
                )
            )
            logger.info(
                "Direct merge succeeded for %s (%s)",
                issue_key,
                pr.pr_url,
            )
        else:
            await self._event_bus.publish(
                Event(
                    type=EventType.PR_AUTO_MERGE_FAILED,
                    task_key=issue_key,
                    data={
                        "pr_url": pr.pr_url,
                        "reason": "Both auto-merge and direct merge failed",
                    },
                )
            )
            logger.warning(
                "Auto-merge failed for %s (%s)",
                issue_key,
                pr.pr_url,
            )

    async def _request_pre_merge_review(
        self,
        issue_key: str,
        pr: TrackedPR,
    ) -> None:
        """Launch pre-merge review as a background task."""
        # Guard: already has an in-flight review for this issue
        existing = self._review_tasks.get(issue_key)
        if existing is not None and not existing.done():
            return

        active_reviews = sum(1 for t in self._review_tasks.values() if not t.done())
        if active_reviews >= self._config.max_concurrent_reviews:
            logger.debug(
                "Too many concurrent reviews (%d) — skipping %s",
                active_reviews,
                issue_key,
            )
            return

        await self._event_bus.publish(
            Event(
                type=EventType.PR_REVIEW_STARTED,
                task_key=issue_key,
                data={"pr_url": pr.pr_url},
            )
        )

        async def _run_review() -> None:
            try:
                async with asyncio.timeout(
                    self._config.pre_merge_review_timeout_seconds,
                ):
                    if self._reviewer is None:
                        return
                    if pr.removed:
                        return
                    verdict = await self._reviewer.review(
                        owner=pr.owner,
                        repo=pr.repo,
                        pr_number=pr.pr_number,
                        issue_key=issue_key,
                        issue_summary=pr.issue_summary,
                        repo_paths=pr.workspace_state.repo_paths,
                    )

                    if pr.removed:
                        return  # type: ignore[unreachable]

                    await self._event_bus.publish(
                        Event(
                            type=EventType.PR_REVIEW_COMPLETED,
                            task_key=issue_key,
                            data={
                                "pr_url": pr.pr_url,
                                "decision": verdict.decision,
                                "summary": verdict.summary,
                                "issue_count": len(verdict.issues),
                                "issues": [
                                    {
                                        "severity": iss.severity,
                                        "category": iss.category,
                                        "file_path": iss.file_path,
                                        "description": iss.description,
                                        "suggestion": iss.suggestion,
                                    }
                                    for iss in verdict.issues
                                ],
                                "confidence": verdict.confidence,
                                "cost_usd": verdict.cost_usd,
                                "duration_seconds": verdict.duration_seconds,
                            },
                        )
                    )

                    if pr.removed:
                        return  # type: ignore[unreachable]
                    if verdict.decision == "reject":
                        await self._post_review_comments(
                            issue_key,
                            pr,
                            verdict,
                        )
                        if pr.removed:
                            return  # type: ignore[unreachable]
                        await self._send_rejection_to_worker(
                            issue_key,
                            pr,
                            verdict,
                        )

                    logger.info(
                        "Pre-merge review for %s: %s (%d issues, confidence=%.2f)",
                        issue_key,
                        verdict.decision,
                        len(verdict.issues),
                        verdict.confidence,
                    )

                    if not pr.removed:
                        pr.pre_merge_review_verdict = verdict.decision
            except asyncio.CancelledError:
                logger.info(
                    "Pre-merge review cancelled for %s (new commit)",
                    issue_key,
                )
            except TimeoutError:
                if not pr.removed:
                    fail_open = self._config.pre_merge_review_fail_open
                    pr.pre_merge_review_verdict = "approve" if fail_open else "reject"
                    logger.warning(
                        "Pre-merge review timed out for %s — %s",
                        issue_key,
                        pr.pre_merge_review_verdict,
                    )
            except Exception:
                if not pr.removed:
                    fail_open = self._config.pre_merge_review_fail_open
                    pr.pre_merge_review_verdict = "approve" if fail_open else "reject"
                    logger.warning(
                        "Pre-merge review failed for %s — %s",
                        issue_key,
                        pr.pre_merge_review_verdict,
                        exc_info=True,
                    )

        def _on_review_done(t: asyncio.Task[None]) -> None:
            self._review_tasks.pop(issue_key, None)

        # Run in background — don't block the monitor loop.
        # Set the flag AFTER task creation so it's never True
        # without a corresponding background task or verdict.
        task = asyncio.get_running_loop().create_task(
            _run_review(),
            name=f"pre-merge-review-{issue_key}",
        )
        self._review_tasks[issue_key] = task
        task.add_done_callback(_on_review_done)
        pr.pre_merge_review_requested = True

    async def _post_review_comments(
        self,
        issue_key: str,
        pr: TrackedPR,
        verdict: ReviewVerdict,
    ) -> None:
        """Post review comments on GitHub for a rejected PR."""
        body_lines = [
            "## Pre-Merge Code Review — Rejected",
            "",
            f"**Summary:** {verdict.summary}",
            f"**Confidence:** {verdict.confidence:.0%}",
            "",
        ]

        if verdict.issues:
            body_lines.append("### Issues")
            for issue in verdict.issues:
                loc = f" `{issue.file_path}`:" if issue.file_path else ":"
                body_lines.append(f"- **[{issue.severity}]**{loc} {issue.description}")
                if issue.suggestion:
                    body_lines.append(f"  - Suggestion: {issue.suggestion}")
            body_lines.append("")

        body = "\n".join(body_lines)

        success = await asyncio.to_thread(
            self._github.post_review,
            pr.owner,
            pr.repo,
            pr.pr_number,
            body=body,
            event="REQUEST_CHANGES",
        )
        if success:
            pr.request_changes_posted = True
        else:
            logger.warning(
                "Failed to post review comments on %s for %s",
                pr.pr_url,
                issue_key,
            )

    async def _send_rejection_to_worker(
        self,
        issue_key: str,
        pr: TrackedPR,
        verdict: ReviewVerdict,
    ) -> None:
        """Send rejection feedback to the worker agent session.

        Follows the same lock/semaphore/drain pattern as
        ``_process_failed_checks``.
        """
        prompt = build_pre_merge_rejection_prompt(
            issue_key,
            pr.pr_url,
            verdict,
        )
        try:
            lock = self._session_locks.setdefault(
                issue_key,
                asyncio.Lock(),
            )
            async with lock:
                async with self._semaphore:
                    result = await pr.session.send(prompt)
                    result = await pr.session.drain_pending_messages(
                        result,
                    )
        except Exception:
            logger.warning(
                "Failed to send rejection prompt to agent for %s",
                issue_key,
                exc_info=True,
            )
            return

        if result.proposals:
            await self._proposal_manager.process_proposals(
                issue_key,
                result.proposals,
            )

    async def _dismiss_stale_request_changes(
        self,
        issue_key: str,
        pr: TrackedPR,
    ) -> None:
        """Post APPROVE review to dismiss bot's stale REQUEST_CHANGES.

        After a reject→fix→re-review approve cycle, the bot's
        REQUEST_CHANGES review keeps ``reviewDecision`` at
        ``CHANGES_REQUESTED`` on GitHub.  Posting an APPROVE
        review from the same bot account dismisses it.
        """
        success = await asyncio.to_thread(
            self._github.post_review,
            pr.owner,
            pr.repo,
            pr.pr_number,
            body=("Pre-merge review approved — dismissing previous request for changes."),
            event="APPROVE",
        )
        if success:
            pr.request_changes_posted = False
            logger.info(
                "Dismissed stale REQUEST_CHANGES on %s for %s",
                pr.pr_url,
                issue_key,
            )
        else:
            logger.warning(
                "Failed to dismiss stale REQUEST_CHANGES on %s for %s",
                pr.pr_url,
                issue_key,
            )
