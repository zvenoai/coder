"""Opus orchestrator agent — replaces hardcoded decision tree with LLM decisions.

This agent handles:
- Result processing: decides track_pr / retry / escalate / fail / complete
- Epic coordination: analyzes children, manages dependencies, activates
- Task classification: selects model (sonnet/opus) per task
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import requests

from orchestrator.constants import EventType
from orchestrator.event_bus import Event
from orchestrator.orchestrator_tools import (
    complete_task_impl,
    fail_task_impl,
    monitor_needs_info_impl,
    track_pr_impl,
)

if TYPE_CHECKING:
    from orchestrator.agent_mailbox import AgentMailbox
    from orchestrator.agent_runner import AgentResult, AgentSession
    from orchestrator.epic_coordinator import EpicCoordinator
    from orchestrator.event_bus import EventBus
    from orchestrator.needs_info_monitor import NeedsInfoMonitor
    from orchestrator.pr_monitor import PRMonitor
    from orchestrator.proposal_manager import ProposalManager
    from orchestrator.recovery import ErrorCategory, RecoveryManager
    from orchestrator.tracker_client import TrackerClient, TrackerIssue
    from orchestrator.tracker_tools import ToolState
    from orchestrator.workspace_tools import WorkspaceState

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    """Opus-level orchestrator that decides what to do with worker results.

    Phase 1: Direct decision logic replacing _handle_agent_result().
    Future: Will be backed by an actual Opus agent with MCP tools.
    """

    def __init__(
        self,
        event_bus: EventBus,
        tracker: TrackerClient,
        pr_monitor: PRMonitor,
        needs_info_monitor: NeedsInfoMonitor,
        recovery: RecoveryManager,
        dispatched_set: set[str],
        cleanup_worktrees_callback: Callable[[str, list[Path]], None],
        cleanup_session_lock: Callable[[str], None] | None = None,
        proposal_manager: ProposalManager | None = None,
        epic_state_store: EpicCoordinator | None = None,
        mailbox: AgentMailbox | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._tracker = tracker
        self._pr_monitor = pr_monitor
        self._needs_info_monitor = needs_info_monitor
        self._recovery = recovery
        self._dispatched = dispatched_set
        self._cleanup_worktrees = cleanup_worktrees_callback
        self._cleanup_session_lock = cleanup_session_lock
        self._proposal_manager = proposal_manager
        self._epic_state_store = epic_state_store
        self._mailbox = mailbox

    async def handle_result(
        self,
        issue: TrackerIssue,
        result: AgentResult,
        session: AgentSession,
        workspace_state: WorkspaceState,
        tool_state: ToolState,
    ) -> None:
        """Process worker agent result and decide next action.

        Decision logic:
        - is_rate_limited → treat as retryable failure
        - needs_info → monitor_needs_info
        - success + pr_url → track_pr
        - success + no pr → complete_task (agent-driven completion)
        - failure + retryable → retry
        - failure + non-retryable → fail permanently
        """
        # Process proposals
        if result.proposals and self._proposal_manager:
            await self._proposal_manager.process_proposals(issue.key, result.proposals)

        # Publish orchestrator decision event
        await self._event_bus.publish(
            Event(
                type=EventType.ORCHESTRATOR_DECISION,
                task_key=issue.key,
                data={
                    "success": result.success,
                    "has_pr": bool(result.pr_url),
                    "needs_info": result.needs_info,
                    "cost": result.cost_usd,
                },
            )
        )

        # Rate-limited results should be retried, even if success=True
        if result.is_rate_limited:
            from orchestrator.recovery import ErrorCategory

            await self._handle_failure(issue, result, session, workspace_state, error_category=ErrorCategory.RATE_LIMIT)
            return

        if result.externally_resolved:
            await self._handle_externally_resolved(
                issue,
                result,
                session,
                workspace_state,
            )
            return

        if result.needs_info:
            await self._handle_needs_info(issue, result, session, workspace_state, tool_state)
            return

        if result.success:
            if result.pr_url:
                await self._handle_success_with_pr(issue, result, session, workspace_state)
            else:
                await self._handle_success_no_pr(issue, result, session, workspace_state)
        else:
            await self._handle_failure(issue, result, session, workspace_state)

    async def _handle_needs_info(
        self,
        issue: TrackerIssue,
        result: AgentResult,
        session: AgentSession,
        workspace_state: WorkspaceState,
        tool_state: ToolState,
    ) -> None:
        """Agent requested info → enter needs-info monitoring."""
        logger.info("Agent requested info for %s — entering needs-info monitoring", issue.key)
        await monitor_needs_info_impl(
            issue_key=issue.key,
            session=session,
            workspace_state=workspace_state,
            tool_state=tool_state,
            event_bus=self._event_bus,
            needs_info_monitor=self._needs_info_monitor,
            tracker=self._tracker,
            issue_summary=issue.summary,
        )

    async def _handle_success_with_pr(
        self,
        issue: TrackerIssue,
        result: AgentResult,
        session: AgentSession,
        workspace_state: WorkspaceState,
    ) -> None:
        """Agent succeeded with PR → track for review monitoring."""
        logger.info("Agent completed %s with PR %s", issue.key, result.pr_url)
        if not result.pr_url:
            raise ValueError(f"Success with PR but pr_url is None for {issue.key}")
        await track_pr_impl(
            issue_key=issue.key,
            pr_url=result.pr_url,
            session=session,
            workspace_state=workspace_state,
            event_bus=self._event_bus,
            tracker=self._tracker,
            pr_monitor=self._pr_monitor,
            recovery=self._recovery,
            issue_summary=issue.summary,
            cost_usd=result.cost_usd,
            duration_seconds=result.duration_seconds,
            resumed=result.resumed,
        )

    async def _unregister_agent(self, issue_key: str) -> None:
        """Unregister agent from mailbox when session is truly ending."""
        if self._mailbox is not None:
            await self._mailbox.unregister_agent(issue_key)

    async def _handle_externally_resolved(
        self,
        issue: TrackerIssue,
        result: AgentResult,
        session: AgentSession,
        workspace_state: WorkspaceState,
    ) -> None:
        """Task was externally resolved/cancelled in Tracker."""
        from orchestrator.recovery import ErrorCategory

        self._recovery.record_failure(
            issue.key,
            result.output or "Externally resolved",
            category=ErrorCategory.CANCELLED,
        )
        await self._unregister_agent(issue.key)
        self._dispatched.discard(issue.key)
        session_id = session.session_id
        await session.close()
        self._cleanup_worktrees(
            issue.key,
            workspace_state.repo_paths,
        )
        if self._cleanup_session_lock is not None:
            self._cleanup_session_lock(issue.key)
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_FAILED,
                task_key=issue.key,
                data={
                    "error": result.output[:500],
                    "cancelled": True,
                    "cost": result.cost_usd,
                    "duration": result.duration_seconds,
                    "session_id": session_id,
                },
            )
        )

    async def _handle_success_no_pr(
        self,
        issue: TrackerIssue,
        result: AgentResult,
        session: AgentSession,
        workspace_state: WorkspaceState,
    ) -> None:
        """Agent succeeded without PR → check if retry needed or permanent failure.

        Records no-PR completion and determines if the task should be retried
        (e.g., provider rate limit, repeated failures to create PR) or if it's
        a legitimate completion (research, config, documentation).
        """
        from orchestrator.recovery import is_provider_rate_limit

        state = self._recovery.record_no_pr(issue.key, result.output, result.cost_usd or 0.0)
        # Unregister early: all exit paths (complete, retry, fail)
        # need it, and it's idempotent.
        await self._unregister_agent(issue.key)

        # Check for rate limit first (immediate failure, no retries)
        if state.last_output and is_provider_rate_limit(state.last_output):
            logger.error(
                "Agent hit provider rate limit for %s after %d attempt(s), cost=$%.2f",
                issue.key,
                state.no_pr_count,
                state.no_pr_cost,
            )
            comment = (
                f"AI Agent достиг лимита API провайдера.\n\n"
                f"Попыток без PR: {state.no_pr_count}\n"
                f"Суммарная стоимость: ${state.no_pr_cost:.2f}\n\n"
                f"Последний вывод:\n```\n{result.output[-500:]}\n```"
            )
            try:
                await asyncio.to_thread(self._tracker.add_comment, issue.key, comment)
            except Exception:
                logger.warning("Failed to comment on %s", issue.key)

            await fail_task_impl(
                issue_key=issue.key,
                error="Provider rate limit hit",
                session=session,
                workspace_state=workspace_state,
                event_bus=self._event_bus,
                recovery=self._recovery,
                cleanup_worktrees_callback=self._cleanup_worktrees,
                skip_record=True,  # Already recorded via record_no_pr
                cost_usd=result.cost_usd,
                duration_seconds=result.duration_seconds,
            )
            if self._cleanup_session_lock is not None:
                self._cleanup_session_lock(issue.key)
            return

        # Continuation exhausted (multiple turns without PR)
        if result.continuation_exhausted and state.no_pr_count > 1:
            if state.should_retry_no_pr:
                sid = session.session_id
                cost = result.cost_usd or 0.0
                duration = result.duration_seconds or 0.0
                self._dispatched.discard(issue.key)
                await session.close()
                self._cleanup_worktrees(
                    issue.key,
                    workspace_state.repo_paths,
                )
                if self._cleanup_session_lock is not None:
                    self._cleanup_session_lock(issue.key)
                await self._event_bus.publish(
                    Event(
                        type=EventType.TASK_FAILED,
                        task_key=issue.key,
                        data={
                            "error": ("Continuation exhausted without PR"),
                            "retryable": True,
                            "continuation_exhausted": True,
                            "cost": cost,
                            "duration": duration,
                            "session_id": sid,
                        },
                    )
                )
            else:
                await fail_task_impl(
                    issue_key=issue.key,
                    error=(f"Continuation exhausted after {state.no_pr_count} attempts"),
                    session=session,
                    workspace_state=workspace_state,
                    event_bus=self._event_bus,
                    recovery=self._recovery,
                    cleanup_worktrees_callback=(self._cleanup_worktrees),
                    skip_record=True,
                    cost_usd=result.cost_usd,
                    duration_seconds=result.duration_seconds,
                )
                if self._cleanup_session_lock is not None:
                    self._cleanup_session_lock(issue.key)
            return

        # First no-PR completion → treat as legitimate success
        if state.no_pr_count == 1:
            logger.info(
                "Agent completed %s without PR (legitimate completion), cost=$%.2f",
                issue.key,
                result.cost_usd or 0.0,
            )
            duration_str = f"{result.duration_seconds:.0f}с" if result.duration_seconds else "N/A"
            cost_str = f"${result.cost_usd:.2f}" if result.cost_usd else "$0.00"
            comment = (
                f"AI Agent завершил работу без создания PR.\n\n"
                f"Стоимость: {cost_str}\n"
                f"Длительность: {duration_str}\n\n"
                f"Последний вывод:\n```\n{result.output[-500:]}\n```"
            )
            try:
                await asyncio.to_thread(self._tracker.add_comment, issue.key, comment)
            except Exception:
                logger.warning("Failed to comment on %s", issue.key)

            await complete_task_impl(
                issue_key=issue.key,
                summary=f"Completed without PR: {issue.summary}",
                session=session,
                workspace_state=workspace_state,
                event_bus=self._event_bus,
                cleanup_worktrees_callback=self._cleanup_worktrees,
            )
            # Don't clear recovery state to prevent infinite retries if re-dispatched
            self._dispatched.discard(issue.key)
            if self._cleanup_session_lock is not None:
                self._cleanup_session_lock(issue.key)
            return

        # Repeated no-PR (2nd+ attempt) → retry or fail
        if state.should_retry_no_pr:
            logger.info(
                "Agent completed %s without PR (retryable, attempt %d/%d)",
                issue.key,
                state.no_pr_count,
                3,  # MAX_NO_PR_ATTEMPTS
            )
            # Unblock from dispatched for re-dispatch
            self._dispatched.discard(issue.key)
            session_id = session.session_id
            await session.close()
            self._cleanup_worktrees(issue.key, workspace_state.repo_paths)
            if self._cleanup_session_lock is not None:
                self._cleanup_session_lock(issue.key)
            await self._event_bus.publish(
                Event(
                    type=EventType.TASK_FAILED,
                    task_key=issue.key,
                    data={
                        "error": f"No PR created (attempt {state.no_pr_count})",
                        "retryable": True,
                        "cost": result.cost_usd,
                        "duration": result.duration_seconds,
                        "session_id": session_id,
                    },
                )
            )
        else:
            # Max attempts reached → permanent failure
            logger.error(
                "Agent failed to create PR for %s after %d attempts, cost=$%.2f",
                issue.key,
                state.no_pr_count,
                state.no_pr_cost,
            )
            comment = (
                f"AI Agent завершил работу без создания PR после {state.no_pr_count} попытки без PR.\n\n"
                f"Суммарная стоимость: ${state.no_pr_cost:.2f}\n\n"
                f"Последний вывод:\n```\n{result.output[-500:]}\n```"
            )
            try:
                await asyncio.to_thread(self._tracker.add_comment, issue.key, comment)
            except Exception:
                logger.warning("Failed to comment on %s", issue.key)

            await fail_task_impl(
                issue_key=issue.key,
                error=f"No PR after {state.no_pr_count} attempts",
                session=session,
                workspace_state=workspace_state,
                event_bus=self._event_bus,
                recovery=self._recovery,
                cleanup_worktrees_callback=self._cleanup_worktrees,
                skip_record=True,  # Already recorded via record_no_pr
                cost_usd=result.cost_usd,
                duration_seconds=result.duration_seconds,
            )
            if self._cleanup_session_lock is not None:
                self._cleanup_session_lock(issue.key)

    async def _handle_failure(
        self,
        issue: TrackerIssue,
        result: AgentResult,
        session: AgentSession,
        workspace_state: WorkspaceState,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """Agent failed → decide retry or permanent failure.

        Args:
            error_category: Explicit error category to prevent misclassification.
                When provided, skips classify_error() pattern matching.
        """
        state = self._recovery.record_failure(issue.key, result.output, category=error_category)
        await self._unregister_agent(issue.key)

        if state.should_retry:
            logger.info("Agent failed for %s (retryable, attempt %d)", issue.key, state.attempt_count)
            self._dispatched.discard(issue.key)
            session_id = session.session_id
            await session.close()
            self._cleanup_worktrees(issue.key, workspace_state.repo_paths)
            if self._cleanup_session_lock is not None:
                self._cleanup_session_lock(issue.key)
            await self._event_bus.publish(
                Event(
                    type=EventType.TASK_FAILED,
                    task_key=issue.key,
                    data={
                        "error": result.output[:500],
                        "retryable": True,
                        "cost": result.cost_usd,
                        "duration": result.duration_seconds,
                        "session_id": session_id,
                    },
                )
            )
        else:
            logger.error("Agent permanently failed for %s: %s", issue.key, result.output[:200])
            try:
                await asyncio.to_thread(
                    self._tracker.add_comment,
                    issue.key,
                    f"AI Agent failed after {state.attempt_count} attempt(s):\n\n{result.output[:500]}",
                )
            except requests.RequestException:
                logger.warning("Failed to comment on %s", issue.key)

            await fail_task_impl(
                issue_key=issue.key,
                error=result.output[:500],
                session=session,
                workspace_state=workspace_state,
                event_bus=self._event_bus,
                recovery=self._recovery,
                cleanup_worktrees_callback=self._cleanup_worktrees,
                skip_record=True,  # Already recorded above
                cost_usd=result.cost_usd,
                duration_seconds=result.duration_seconds,
            )
            if self._cleanup_session_lock is not None:
                self._cleanup_session_lock(issue.key)

    async def handle_epic_child_event(
        self,
        child_key: str,
        event_type: Literal["completed", "failed", "cancelled"],
    ) -> None:
        """Handle epic child completion/failure/cancellation events.

        Delegates to EpicCoordinator methods.
        """
        if not self._epic_state_store:
            return
        if not self._epic_state_store.is_epic_child(child_key):
            return

        if event_type == "completed":
            await self._epic_state_store.on_task_completed(child_key)
        elif event_type == "failed":
            await self._epic_state_store.on_task_failed(child_key)
        elif event_type == "cancelled":
            await self._epic_state_store.on_task_cancelled(child_key)
