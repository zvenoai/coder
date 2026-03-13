"""Monitor for tasks in 'needs info' status waiting for human responses."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from orchestrator._persistence import BackgroundPersistenceMixin
from orchestrator.agent_runner import AgentSession
from orchestrator.constants import EventType
from orchestrator.event_bus import Event
from orchestrator.prompt_builder import build_needs_info_response_prompt
from orchestrator.tracker_tools import ToolState
from orchestrator.tracker_types import TrackerCommentDict
from orchestrator.workspace_tools import WorkspaceState

if TYPE_CHECKING:
    from orchestrator.agent_mailbox import AgentMailbox
    from orchestrator.config import Config
    from orchestrator.event_bus import EventBus
    from orchestrator.proposal_manager import ProposalManager
    from orchestrator.storage import Storage
    from orchestrator.tracker_client import TrackerClient

logger = logging.getLogger(__name__)

# Status key variants that indicate "needs info" (must be in normalized form:
# lowercase with all separators stripped, since is_needs_info_status normalizes input)
_NEEDS_INFO_STATUSES = frozenset({"needinfo", "needsinfo"})


def is_needs_info_status(status: str) -> bool:
    """Check if status indicates 'needs info'."""
    normalized = status.lower().replace("-", "").replace("_", "").replace(" ", "")
    return normalized in _NEEDS_INFO_STATUSES


@dataclass
class TrackedNeedsInfo:
    """A task in 'needs info' status being monitored for human responses."""

    issue_key: str
    session: AgentSession
    workspace_state: WorkspaceState
    tool_state: ToolState
    last_check_at: float
    last_seen_comment_id: int
    issue_summary: str = ""
    removed: bool = False


# Type aliases for callbacks
TrackPRCallback = Callable[[str, str, AgentSession, WorkspaceState, str], None]
CleanupWorktreesCallback = Callable[[str, list[Path]], None]
RecordFailureCallback = Callable[[str, str], object]
ClearRecoveryCallback = Callable[[str], None]


class NeedsInfoMonitor(BackgroundPersistenceMixin):
    """Monitors tasks in 'needs info' status for human responses."""

    def __init__(
        self,
        tracker: TrackerClient,
        event_bus: EventBus,
        proposal_manager: ProposalManager,
        config: Config,
        semaphore: asyncio.Semaphore,
        session_locks: dict[str, asyncio.Lock],
        shutdown_event: asyncio.Event,
        bot_login: str,
        track_pr_callback: TrackPRCallback,
        cleanup_worktrees_callback: CleanupWorktreesCallback,
        get_latest_comment_id_callback: Callable[[str], int],
        dispatched_set: set[str],
        record_failure_callback: RecordFailureCallback | None = None,
        clear_recovery_callback: ClearRecoveryCallback | None = None,
        storage: Storage | None = None,
        mailbox: AgentMailbox | None = None,
    ) -> None:
        self._tracker = tracker
        self._event_bus = event_bus
        self._proposal_manager = proposal_manager
        self._config = config
        self._semaphore = semaphore
        self._session_locks = session_locks
        self._shutdown = shutdown_event
        self._bot_login = bot_login
        self._track_pr = track_pr_callback
        self._cleanup_worktrees = cleanup_worktrees_callback
        self._get_latest_comment_id = get_latest_comment_id_callback
        self._dispatched = dispatched_set
        self._record_failure = record_failure_callback
        self._clear_recovery = clear_recovery_callback
        self._storage = storage
        self._mailbox = mailbox
        self._tracked: dict[str, TrackedNeedsInfo] = {}
        self._persisted_comment_ids: dict[str, tuple[int, str | None]] = {}
        self._init_persistence()

    def get_tracked(self) -> dict[str, TrackedNeedsInfo]:
        """Get all tracked needs-info tasks (for state snapshot)."""
        return self._tracked

    async def load(self) -> None:
        """Load last_seen_comment_id from storage.

        Restores dedup state only. TrackedNeedsInfo objects with sessions
        are NOT restored — they will be recreated when tasks resume.
        The loaded data is applied when add() is called.
        """
        if not self._storage:
            return
        try:
            records = await self._storage.load_needs_info_tracking()
            self._persisted_comment_ids.clear()
            for rec in records:
                self._persisted_comment_ids[rec.issue_key] = (rec.last_seen_comment_id, rec.session_id)
            if records:
                logger.info("Loaded needs-info tracking for %d issues from storage", len(records))
        except Exception:
            logger.warning("Failed to load needs-info tracking from storage", exc_info=True)

    def _persist_tracking(self, issue_key: str) -> None:
        """Schedule async persistence of needs-info tracking data."""
        if not self._storage:
            return

        from orchestrator.stats_models import NeedsInfoTrackingRecord

        ni = self._tracked.get(issue_key)
        if not ni:
            return

        record = NeedsInfoTrackingRecord(
            issue_key=issue_key,
            last_seen_comment_id=ni.last_seen_comment_id,
            issue_summary=ni.issue_summary,
            tracked_at=time.time(),
            session_id=ni.session.session_id,
        )

        async def _write() -> None:
            async with self._key_locks[issue_key]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.upsert_needs_info_tracking(record)
                except Exception:
                    logger.warning("Failed to persist needs-info tracking for %s", issue_key, exc_info=True)

        self._schedule_task(_write())

    def _persist_delete_tracking(self, issue_key: str) -> None:
        """Schedule async deletion of needs-info tracking data."""
        if not self._storage:
            return

        async def _delete() -> None:
            async with self._key_locks[issue_key]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.delete_needs_info_tracking(issue_key)
                except Exception:
                    logger.warning("Failed to delete needs-info tracking for %s", issue_key, exc_info=True)

        self._schedule_task(_delete())

    def add(self, needs_info: TrackedNeedsInfo) -> None:
        """Add a task to needs-info tracking."""
        # Restore persisted comment ID if available
        persisted = self._persisted_comment_ids.get(needs_info.issue_key)
        if persisted is not None:
            persisted_id, _session_id = persisted
            needs_info.last_seen_comment_id = max(needs_info.last_seen_comment_id, persisted_id)
            del self._persisted_comment_ids[needs_info.issue_key]
        self._tracked[needs_info.issue_key] = needs_info
        self._persist_tracking(needs_info.issue_key)
        logger.info("Now tracking needs-info for %s", needs_info.issue_key)

    def get_persisted_session_id(self, issue_key: str) -> str | None:
        """Get persisted session_id for an issue (for session resumption after restart)."""
        persisted = self._persisted_comment_ids.get(issue_key)
        if persisted:
            return persisted[1]
        return None

    @staticmethod
    def is_needs_info_status(status: str) -> bool:
        """Check if status indicates 'needs info'."""
        return is_needs_info_status(status)

    async def remove(self, key: str) -> None:
        """Remove and close a single tracked needs-info task.

        Acquires session lock to avoid closing a session
        while send() is in-flight.
        """
        lock = self._session_locks.get(key)
        if lock is not None:
            await lock.acquire()
        try:
            ni = self._tracked.pop(key, None)
            if ni is None:
                return
            ni.removed = True
            await ni.session.close()
            if self._mailbox is not None:
                await self._mailbox.unregister_agent(key)
        finally:
            if lock is not None:
                lock.release()
            self._session_locks.pop(key, None)
        self._persist_delete_tracking(key)
        self._cleanup_worktrees(
            key,
            ni.workspace_state.repo_paths,
        )
        self._dispatched.discard(key)

    async def close_all(self) -> None:
        """Close all tracked sessions (for graceful shutdown)."""
        # Capture keys before clearing for session lock cleanup
        tracked_keys = list(self._tracked.keys())
        for key, ni in self._tracked.items():
            await ni.session.close()
            if self._mailbox is not None:
                await self._mailbox.unregister_agent(key)
        self._tracked.clear()
        # Clean up session locks for all tracked needs-info sessions
        for key in tracked_keys:
            self._session_locks.pop(key, None)

    async def watch(self) -> None:
        """Background watcher loop — monitors needs-info tasks for human responses."""
        while True:
            await self._check_all()

            if self._shutdown.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._config.needs_info_check_delay_seconds,
                )
            except TimeoutError:
                pass
            if self._shutdown.is_set():
                break

    async def _check_all(self) -> None:
        """Single pass over tracked needs-info tasks — check for new human comments."""
        for issue_key, ni in list(self._tracked.items()):
            elapsed = time.monotonic() - ni.last_check_at
            if elapsed < self._config.needs_info_check_delay_seconds:
                continue

            ni.last_check_at = time.monotonic()

            try:
                comments: list[TrackerCommentDict] = await asyncio.to_thread(self._tracker.get_comments, issue_key)
            except requests.RequestException:
                logger.warning("Failed to get comments for needs-info %s", issue_key)
                continue

            # Filter new comments from non-bot users
            new_comments: list[TrackerCommentDict] = [
                c for c in comments if c.get("id", 0) > ni.last_seen_comment_id and not self._is_bot_author(c)
            ]
            if not new_comments:
                continue

            logger.info(
                "Found %d new human comment(s) for needs-info %s",
                len(new_comments),
                issue_key,
            )

            await self._handle_new_comments(issue_key, ni, new_comments, comments)

    async def _handle_new_comments(
        self,
        issue_key: str,
        ni: TrackedNeedsInfo,
        new_comments: list[TrackerCommentDict],
        all_comments: list[TrackerCommentDict],
    ) -> None:
        """Process new human comments for a needs-info task."""
        # Transition back to in-progress
        try:
            await asyncio.to_thread(self._tracker.transition_to_in_progress, issue_key)
        except requests.RequestException:
            logger.warning("Failed to transition %s back to in-progress", issue_key)

        # Update last seen comment ID
        ni.last_seen_comment_id = max(c.get("id", 0) for c in all_comments)
        self._persist_tracking(issue_key)

        # Send new comments to agent
        prompt = build_needs_info_response_prompt(issue_key, new_comments)
        await self._event_bus.publish(
            Event(
                type=EventType.NEEDS_INFO_RESPONSE,
                task_key=issue_key,
                data={"comment_count": len(new_comments)},
            )
        )

        lock = self._session_locks.setdefault(issue_key, asyncio.Lock())
        async with lock:
            async with self._semaphore:
                result = await ni.session.send(prompt)
                result = await ni.session.drain_pending_messages(result)

        if result.proposals:
            await self._proposal_manager.process_proposals(issue_key, result.proposals)

        # Handle result
        if result.needs_info:
            # Agent still needs more info — keep monitoring
            logger.info("Agent still needs info for %s", issue_key)
            ni.last_seen_comment_id = await asyncio.to_thread(self._get_latest_comment_id, issue_key)
            ni.last_check_at = time.monotonic()
            self._persist_tracking(issue_key)
        elif result.success and result.pr_url:
            # Agent created a PR — move to PR tracking
            await self._handle_pr_created(issue_key, ni, result.pr_url, result.cost_usd, result.duration_seconds)
        elif result.success:
            # Success without PR
            await self._handle_success(issue_key, ni, result.cost_usd, result.duration_seconds, result.output)
        else:
            # Failure
            await self._handle_failure(
                issue_key, ni, result.output, cost=result.cost_usd, duration=result.duration_seconds
            )

    async def _handle_pr_created(
        self, issue_key: str, ni: TrackedNeedsInfo, pr_url: str, cost: float | None, duration: float | None
    ) -> None:
        """Handle agent creating a PR after receiving info."""
        if ni.removed:
            return
        logger.info("Agent created PR %s for %s after info response", pr_url, issue_key)
        if self._clear_recovery:
            self._clear_recovery(issue_key)
        try:
            await asyncio.to_thread(self._tracker.transition_to_review, issue_key)
        except requests.RequestException:
            logger.warning("Failed to transition %s to review", issue_key)

        if ni.removed:
            return  # type: ignore[unreachable]
        self._track_pr(issue_key, pr_url, ni.session, ni.workspace_state, ni.issue_summary)
        self._tracked.pop(issue_key, None)
        self._persist_delete_tracking(issue_key)

        await self._event_bus.publish(
            Event(
                type=EventType.PR_TRACKED,
                task_key=issue_key,
                data={"pr_url": pr_url, "cost": cost, "duration": duration},
            )
        )

    async def _handle_success(
        self, issue_key: str, ni: TrackedNeedsInfo, cost: float | None, duration: float | None, output: str = ""
    ) -> None:
        """Handle agent completing task without PR after receiving info."""
        if ni.removed:
            return
        logger.info("Agent completed %s without PR after info response", issue_key)
        if self._mailbox is not None:
            await self._mailbox.unregister_agent(issue_key)
        await ni.session.close()
        self._tracked.pop(issue_key, None)
        self._persist_delete_tracking(issue_key)
        self._cleanup_worktrees(issue_key, ni.workspace_state.repo_paths)
        self._session_locks.pop(issue_key, None)

        if ni.removed:
            return  # type: ignore[unreachable]
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                task_key=issue_key,
                data={"cost": cost, "duration": duration},
            )
        )

        self._dispatched.discard(issue_key)

    async def _handle_failure(
        self, issue_key: str, ni: TrackedNeedsInfo, error: str, cost: float | None = None, duration: float | None = None
    ) -> None:
        """Handle agent failure after receiving info."""
        if ni.removed:
            return
        logger.warning("Agent failed for %s after info response: %s", issue_key, error[:200])
        state = self._record_failure(issue_key, error) if self._record_failure else None
        should_retry = getattr(state, "should_retry", False) if state else False
        if self._mailbox is not None:
            await self._mailbox.unregister_agent(issue_key)
        await ni.session.close()
        self._tracked.pop(issue_key, None)
        self._persist_delete_tracking(issue_key)
        self._cleanup_worktrees(issue_key, ni.workspace_state.repo_paths)
        self._session_locks.pop(issue_key, None)

        if ni.removed:
            return  # type: ignore[unreachable]
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key=issue_key,
                data={
                    "error": error[:500],
                    "retryable": should_retry,
                    "cost": cost,
                    "duration": duration,
                },
            )
        )
        if should_retry:
            self._dispatched.discard(issue_key)

    def _is_bot_author(self, comment: TrackerCommentDict) -> bool:
        """Check if comment is from the bot itself."""
        author_login = comment.get("createdBy", {}).get("login", "")
        return bool(self._bot_login and author_login == self._bot_login)
