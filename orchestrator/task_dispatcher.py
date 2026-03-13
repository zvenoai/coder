"""Task dispatcher — polls Tracker and dispatches agents for new tasks."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests

from orchestrator.agent_runner import AgentResult, AgentSession, merge_results
from orchestrator.compaction import build_continuation_prompt, should_compact, summarize_output
from orchestrator.constants import (
    MAX_COMPACTION_CYCLES,
    MAX_CONTINUATION_TURNS,
    EventType,
    PRState,
    ResolutionType,
    is_cancelled_status,
    is_resolved_status,
)
from orchestrator.event_bus import Event
from orchestrator.needs_info_monitor import is_needs_info_status as _is_needs_info_status
from orchestrator.pr_monitor import parse_pr_url
from orchestrator.prompt_builder import (
    PeerInfo,
    build_task_continuation_prompt,
    build_task_prompt,
)
from orchestrator.tracker_tools import ToolState
from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

if TYPE_CHECKING:
    from orchestrator.agent_mailbox import AgentMailbox
    from orchestrator.agent_runner import AgentRunner
    from orchestrator.config import Config
    from orchestrator.dependency_manager import DependencyManager
    from orchestrator.epic_coordinator import EpicCoordinator
    from orchestrator.event_bus import EventBus
    from orchestrator.github_client import GitHubClient
    from orchestrator.preflight_checker import PreflightChecker
    from orchestrator.recovery import RecoveryManager
    from orchestrator.repo_resolver import RepoResolver
    from orchestrator.tracker_client import TrackerClient, TrackerIssue
    from orchestrator.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


# Type aliases for callbacks
HandleResultCallback = Callable[
    ["TrackerIssue", "AgentResult", AgentSession, WorkspaceState, ToolState],
    Awaitable[None],
]
ResumeNeedsInfoCallback = Callable[["TrackerIssue", AgentSession, WorkspaceState, ToolState], Awaitable[bool]]
FindExistingPRCallback = Callable[[str], str | None]
CleanupWorktreesCallback = Callable[[str, list[Path]], None]
FindSessionIdCallback = Callable[[str], str | None]


class TaskDispatcher:
    """Polls Tracker for new tasks and dispatches agents to execute them."""

    def __init__(
        self,
        tracker: TrackerClient,
        agent: AgentRunner,
        github: GitHubClient,
        resolver: RepoResolver,
        workspace: WorkspaceManager,
        recovery: RecoveryManager,
        event_bus: EventBus,
        config: Config,
        semaphore: asyncio.Semaphore,
        dispatched_set: set[str],
        tasks_set: set[asyncio.Task],
        handle_result_callback: HandleResultCallback,
        resume_needs_info_callback: ResumeNeedsInfoCallback,
        find_existing_pr_callback: FindExistingPRCallback,
        cleanup_worktrees_callback: CleanupWorktreesCallback,
        epic_coordinator: EpicCoordinator | None = None,
        find_pr_session_id_callback: FindSessionIdCallback | None = None,
        find_ni_session_id_callback: FindSessionIdCallback | None = None,
        mailbox: AgentMailbox | None = None,
        preflight_checker: PreflightChecker | None = None,
        dependency_manager: DependencyManager | None = None,
    ) -> None:
        self._tracker = tracker
        self._agent = agent
        self._github = github
        self._resolver = resolver
        self._workspace = workspace
        self._recovery = recovery
        self._event_bus = event_bus
        self._config = config
        self._semaphore = semaphore
        self._dispatched = dispatched_set
        self._tasks = tasks_set
        self._handle_result = handle_result_callback
        self._resume_needs_info = resume_needs_info_callback
        self._find_existing_pr = find_existing_pr_callback
        self._cleanup_worktrees = cleanup_worktrees_callback
        self._running_sessions: dict[str, AgentSession] = {}
        self._epic_coordinator = epic_coordinator
        self._find_pr_session_id = find_pr_session_id_callback
        self._find_ni_session_id = find_ni_session_id_callback
        self._mailbox = mailbox
        self._preflight_checker = preflight_checker
        self._dependency_manager = dependency_manager
        self._task_costs: dict[str, float] = {}

    async def poll_and_dispatch(self) -> None:
        """One polling iteration: search and dispatch new tasks."""
        # Recheck deferred tasks first — unblock any whose dependencies resolved
        if self._dependency_manager:
            try:
                unblocked = await self._dependency_manager.recheck_deferred()
                if unblocked:
                    logger.info("Unblocked %d deferred task(s): %s", len(unblocked), ", ".join(unblocked))
            except Exception:
                logger.warning("Error rechecking deferred tasks", exc_info=True)

        cfg = self._config
        query = f'Tags: "{cfg.tracker_tag}" AND Queue: "{cfg.tracker_queue}" AND Resolution: unresolved()'
        logger.info("Searching: %s", query)

        issues = await asyncio.to_thread(self._tracker.search, query)
        if not issues:
            logger.info("No tasks found")
            return

        new_issues = [
            i
            for i in issues
            if i.key not in self._dispatched
            and self._recovery.get_state(i.key).should_retry
            and not (self._dependency_manager and self._dependency_manager.is_deferred(i.key))
        ]
        if not new_issues:
            logger.info("Found %d task(s), none ready for dispatch", len(issues))
            return

        logger.info("Dispatching %d new task(s)", len(new_issues))
        epic_issues = [i for i in new_issues if getattr(i, "type_key", "") == "epic"]
        regular_issues = [i for i in new_issues if getattr(i, "type_key", "") != "epic"]

        # Register epics and run analysis to discover children.
        for issue in epic_issues:
            if self._epic_coordinator:
                self._dispatched.add(issue.key)
                try:
                    await self._epic_coordinator.register_epic(issue)
                    await self._epic_coordinator.discover_children(issue.key)
                except Exception as e:
                    logger.error("Failed to register/analyze epic %s: %s", issue.key, e)
                    state = self._recovery.record_failure(issue.key, e)
                    if state.should_retry:
                        self._dispatched.discard(issue.key)
                    await self._event_bus.publish(
                        Event(
                            type=EventType.TASK_FAILED,
                            task_key=issue.key,
                            data={"error": str(e), "retryable": state.should_retry},
                        )
                    )
                continue

        # Batch dependency checks for non-epic-child tasks (parallel API calls)
        dep_check_issues: list[TrackerIssue] = []
        ready_issues: list[TrackerIssue] = []

        for issue in regular_issues:
            is_epic_child = self._epic_coordinator and self._epic_coordinator.is_epic_child(issue.key)

            if is_epic_child and self._epic_coordinator:
                if not self._epic_coordinator.is_child_ready_for_dispatch(issue.key):
                    logger.info("Skipping blocked epic child %s", issue.key)
                    continue
                self._epic_coordinator.mark_child_dispatched(issue.key)
                ready_issues.append(issue)
                continue

            # Non-epic-child: needs dependency check
            if self._dependency_manager:
                dep_check_issues.append(issue)
            else:
                ready_issues.append(issue)

        # Run all dependency checks in parallel
        if dep_check_issues and self._dependency_manager:
            dep_mgr = self._dependency_manager

            async def _safe_check(iss: TrackerIssue) -> tuple[TrackerIssue, bool]:
                try:
                    return (iss, await dep_mgr.check_dependencies(iss.key, iss.summary, description=iss.description))
                except Exception:
                    logger.warning("Dependency check failed for %s, allowing dispatch", iss.key, exc_info=True)
                    return (iss, False)

            results = await asyncio.gather(*(_safe_check(iss) for iss in dep_check_issues))
            for iss, is_deferred in results:
                if is_deferred:
                    logger.info("Deferred %s due to unresolved dependencies", iss.key)
                else:
                    ready_issues.append(iss)

        for issue in ready_issues:
            # Run preflight check if configured
            if self._preflight_checker:
                try:
                    preflight_result = await self._preflight_checker.check(issue)
                except Exception:
                    logger.warning(
                        "Preflight check failed for %s, allowing dispatch",
                        issue.key,
                        exc_info=True,
                    )
                    preflight_result = None

                # Evidence found — defer for supervisor review
                if preflight_result and preflight_result.needs_review:
                    if self._dependency_manager:
                        evidence_summary = preflight_result.reason[:200]
                        await self._dependency_manager.defer_task(
                            issue.key,
                            issue.summary,
                            f"preflight_review: {evidence_summary}",
                        )
                    else:
                        logger.warning(
                            "Preflight evidence for %s but no dependency manager — allowing dispatch",
                            issue.key,
                        )
                    continue

            self._dispatched.add(issue.key)
            task = asyncio.create_task(self._dispatch_task(issue), name=f"agent-{issue.key}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def get_running_session(self, key: str) -> AgentSession | None:
        """Return the running session for a task, or None."""
        return self._running_sessions.get(key)

    def get_running_sessions(self) -> list[str]:
        """Return keys of all currently running sessions."""
        return list(self._running_sessions.keys())

    def remove_running_session(self, key: str) -> None:
        """Remove a running session (e.g. after abort)."""
        self._running_sessions.pop(key, None)

    def mark_as_processed(self, key: str) -> None:
        """Mark a task as processed (won't be dispatched again)."""
        self._dispatched.add(key)

    def remove_from_processed(self, key: str) -> None:
        """Remove a task from the processed set for re-dispatch."""
        self._dispatched.discard(key)

    def get_task_cost(self, key: str) -> float | None:
        """Return accumulated cost for a running task."""
        return self._task_costs.get(key)

    async def _create_session_with_fallback(
        self,
        issue: TrackerIssue,
        tool_state: ToolState | None = None,
        workspace_server: object | None = None,
        cwd: str | None = None,
        resume_session_id: str | None = None,
    ) -> AgentSession | None:
        """Create a session with resume, falling back to fresh on failure.

        Returns None if session creation fails entirely (publishes TASK_FAILED).
        """
        try:
            return await self._agent.create_session(
                issue,
                event_bus=self._event_bus,
                tool_state=tool_state,
                workspace_server=workspace_server,
                cwd=cwd,
                resume_session_id=resume_session_id,
                mailbox=self._mailbox,
            )
        except Exception as e:
            if resume_session_id:
                logger.warning(
                    "Failed to resume session %s for %s, falling back to fresh session: %s",
                    resume_session_id,
                    issue.key,
                    e,
                )
                try:
                    return await self._agent.create_session(
                        issue,
                        event_bus=self._event_bus,
                        tool_state=tool_state,
                        workspace_server=workspace_server,
                        cwd=cwd,
                        mailbox=self._mailbox,
                    )
                except Exception as e2:
                    e = e2
            logger.error("Failed to create agent session for %s: %s", issue.key, e)
            state = self._recovery.record_failure(issue.key, e)
            if state.should_retry:
                self._dispatched.discard(issue.key)
            await self._event_bus.publish(
                Event(
                    type=EventType.TASK_FAILED,
                    task_key=issue.key,
                    data={"error": str(e), "retryable": state.should_retry},
                )
            )
            return None

    async def _dispatch_task(self, issue: TrackerIssue) -> None:
        """Dispatch a single task with semaphore control."""
        async with self._semaphore:
            logger.info("Processing %s: %s", issue.key, issue.summary)
            await self._event_bus.publish(
                Event(
                    type=EventType.TASK_STARTED,
                    task_key=issue.key,
                    data={"summary": issue.summary, "components": issue.components},
                )
            )

            # Create workspace state (empty — worktrees created lazily)
            workspace_state = WorkspaceState(issue_key=issue.key)
            all_repos = self._config.repos_config.all_repos
            if not all_repos:
                logger.warning("No repos configured for %s", issue.key)
                state = self._recovery.record_failure(issue.key, "No repos configured")
                if state.should_retry:
                    self._dispatched.discard(issue.key)
                await self._event_bus.publish(
                    Event(
                        type=EventType.TASK_FAILED,
                        task_key=issue.key,
                        data={"error": "No repos configured", "retryable": state.should_retry},
                    )
                )
                return

            # Build workspace MCP server
            workspace_server = build_workspace_server(
                self._resolver,
                self._workspace,
                all_repos,
                workspace_state,
                github_token=self._config.github_token,
            )

            # Create cwd directory for agent
            cwd = Path(self._config.worktree_base_dir) / issue.key
            cwd.mkdir(parents=True, exist_ok=True)

            # Check for existing open PR (resume after restart)
            existing_pr = await asyncio.to_thread(self._find_existing_pr, issue.key)
            if existing_pr:
                resumed = await self._try_resume_pr(
                    issue,
                    existing_pr,
                    workspace_state,
                    workspace_server,
                    str(cwd),
                )
                if resumed:
                    return

            # Check for needs-info status (resume monitoring after restart).
            # Must happen before wait_for_retry so needs-info tasks aren't delayed by backoff.
            # Session created only if actually in needs-info status.
            if _is_needs_info_status(issue.status):
                tool_state = ToolState()
                # Look up persisted session_id for needs-info resumption
                ni_resume_session_id: str | None = None
                if self._find_ni_session_id:
                    ni_resume_session_id = self._find_ni_session_id(issue.key)
                if ni_resume_session_id:
                    logger.info("Resuming needs-info for %s with session %s", issue.key, ni_resume_session_id)
                ni_session = await self._create_session_with_fallback(
                    issue,
                    tool_state=tool_state,
                    workspace_server=workspace_server,
                    cwd=str(cwd),
                    resume_session_id=ni_resume_session_id,
                )
                if ni_session is None:
                    self._cleanup_worktrees(issue.key, workspace_state.repo_paths)
                    return

                resumed = await self._resume_needs_info(issue, ni_session, workspace_state, tool_state)
                if resumed:
                    return
                # Not actually needs-info — close the session to avoid leaking it.
                await ni_session.close()

            # Wait for backoff if retrying
            await self._recovery.wait_for_retry(issue.key)

            tool_state = ToolState()
            try:
                session = await self._agent.create_session(
                    issue,
                    event_bus=self._event_bus,
                    tool_state=tool_state,
                    workspace_server=workspace_server,
                    cwd=str(cwd),
                    mailbox=self._mailbox,
                )
            except Exception as e:
                logger.error("Failed to create agent session for %s: %s", issue.key, e)
                state = self._recovery.record_failure(issue.key, e)
                self._cleanup_worktrees(issue.key, workspace_state.repo_paths)
                if state.should_retry:
                    self._dispatched.discard(issue.key)
                await self._event_bus.publish(
                    Event(
                        type=EventType.TASK_FAILED,
                        task_key=issue.key,
                        data={"error": str(e), "retryable": state.should_retry},
                    )
                )
                return

            # Transition to in-progress
            try:
                await asyncio.to_thread(self._tracker.transition_to_in_progress, issue.key)
            except requests.RequestException:
                logger.warning("Failed to transition %s to in-progress", issue.key)

            # Collect running peers for coordination prompt
            peers: list[PeerInfo] | None = None
            if self._mailbox is not None:
                try:
                    agents = await self._mailbox.list_agents()
                    peers = [
                        PeerInfo(
                            task_key=a.task_key,
                            summary=a.task_summary,
                            status=a.status,
                        )
                        for a in agents
                        if a.task_key != issue.key
                    ] or None  # empty list → None to skip prompt section
                except Exception:
                    logger.warning(
                        "Failed to list peers for %s",
                        issue.key,
                        exc_info=True,
                    )

            prompt = build_task_prompt(issue, all_repos, peers=peers)

            model = self._config.agent_model
            self._running_sessions[issue.key] = session
            if self._mailbox is not None:
                component = issue.components[0] if issue.components else None
                repo_slug: str | None = None
                if all_repos:
                    parsed = urlparse(all_repos[0].url)
                    path = parsed.path.strip("/").removesuffix(".git")
                    if "/" in path:
                        repo_slug = path
                self._mailbox.register_agent(
                    issue.key,
                    component=component,
                    repo=repo_slug,
                )
            aborted = False
            try:
                result = await session.send(prompt)
                self._task_costs[issue.key] = result.cost_usd or 0

                session, result = await self._run_compaction_loop(
                    session,
                    result,
                    issue,
                    tool_state,
                    workspace_server,
                    str(cwd),
                    model,
                )

                # Multi-turn continuation: retry when agent
                # completed without PR and task is still open.
                continuation_turn = 0
                while (
                    result.success
                    and not result.pr_url
                    and not result.needs_info
                    and not result.is_rate_limited
                    and not tool_state.task_complete
                    and continuation_turn < MAX_CONTINUATION_TURNS
                ):
                    # Cap covers the entire dispatch (initial +
                    # continuations), not continuation-only cost.
                    cost_cap = self._config.max_continuation_cost
                    if cost_cap is not None and (result.cost_usd or 0) >= cost_cap:
                        break

                    try:
                        current = await asyncio.to_thread(
                            self._tracker.get_issue,
                            issue.key,
                        )
                    except Exception:
                        current = None

                    if current is not None and (
                        is_resolved_status(current.status) or is_cancelled_status(current.status)
                    ):
                        result = dataclasses.replace(
                            result,
                            success=False,
                            externally_resolved=True,
                            output=(f"Task externally moved to '{current.status}'"),
                        )
                        break

                    continuation_turn += 1
                    await self._event_bus.publish(
                        Event(
                            type=EventType.CONTINUATION_TRIGGERED,
                            task_key=issue.key,
                            data={
                                "turn": continuation_turn,
                            },
                        )
                    )

                    cont_prompt = build_task_continuation_prompt(
                        issue.key,
                        issue.summary,
                        continuation_turn,
                        MAX_CONTINUATION_TURNS,
                    )
                    cont_result = await session.send(
                        cont_prompt,
                    )
                    cont_failed = not cont_result.success
                    result = merge_results(result, cont_result)
                    self._task_costs[issue.key] = result.cost_usd or 0

                    if cont_failed:
                        break

                    session, result = await self._run_compaction_loop(
                        session,
                        result,
                        issue,
                        tool_state,
                        workspace_server,
                        str(cwd),
                        model,
                    )

                    result = await session.drain_pending_messages(
                        result,
                    )

                    if result.pr_url or result.needs_info or result.is_rate_limited:
                        break

                # Signal exhaustion if all turns used
                if (
                    continuation_turn >= MAX_CONTINUATION_TURNS
                    and not result.pr_url
                    and not result.needs_info
                    and not tool_state.task_complete
                    and result.success
                ):
                    result = dataclasses.replace(
                        result,
                        continuation_exhausted=True,
                    )

                # Drain any messages queued via interrupt_with_message.
                result = await session.drain_pending_messages(result)
            finally:
                self._task_costs.pop(issue.key, None)
                # If the session was already removed by _abort_task, skip result handling
                # to avoid publishing a second terminal event.
                aborted = issue.key not in self._running_sessions
                self._running_sessions.pop(issue.key, None)
                # NOTE: Do NOT unregister from mailbox here. The session may be
                # transferred to PR monitor or needs-info monitor, where it must
                # remain reachable for inter-agent communication and user messages.
                # Mailbox unregistration happens in the terminal paths:
                # - OrchestratorAgent.handle_result (complete/fail)
                # - PRMonitor.cleanup (PR merged/closed)
                # - NeedsInfoMonitor._handle_success/_handle_failure

            if aborted:
                logger.info("Task %s was aborted — skipping result handling", issue.key)
                self._cleanup_worktrees(issue.key, workspace_state.repo_paths)
                return

            # Delegate result handling to orchestrator (may transfer session to monitors)
            await self._handle_result(issue, result, session, workspace_state, tool_state)

    async def _run_compaction_loop(
        self,
        session: AgentSession,
        result: AgentResult,
        issue: TrackerIssue,
        tool_state: ToolState,
        workspace_server: object,
        cwd: str,
        model: str,
    ) -> tuple[AgentSession, AgentResult]:
        """Run compaction loop, returning updated session and result."""
        compaction_count = 0
        while (
            should_compact(result, self._config, model)
            and compaction_count < MAX_COMPACTION_CYCLES
            and result.success
            and not result.pr_url
            and not result.needs_info
            and not result.is_rate_limited
        ):
            compaction_count += 1
            logger.info(
                "Compacting context for %s (cycle %d)",
                issue.key,
                compaction_count,
            )
            await self._event_bus.publish(
                Event(
                    type=EventType.COMPACTION_TRIGGERED,
                    task_key=issue.key,
                    data={
                        "cycle": compaction_count,
                        "tokens": result.total_tokens,
                    },
                )
            )

            summary = await summarize_output(
                result.output,
                self._config,
            )
            old_session = session
            session = await self._agent.create_session(
                issue,
                event_bus=self._event_bus,
                tool_state=tool_state,
                workspace_server=workspace_server,
                cwd=cwd,
                mailbox=self._mailbox,
            )
            self._running_sessions[issue.key] = session
            old_session.transfer_pending_messages(session)
            old_session.transfer_cumulative_tokens(session)
            await old_session.close()

            continuation = build_continuation_prompt(
                issue.key,
                issue.summary,
                summary,
            )
            new_result = await session.send(continuation)
            result = merge_results(result, new_result)
            self._task_costs[issue.key] = result.cost_usd or 0
        return session, result

    async def _try_resume_pr(
        self,
        issue: TrackerIssue,
        pr_url: str,
        workspace_state: WorkspaceState,
        workspace_server: object,
        cwd: str,
    ) -> bool:
        """Try to resume monitoring an existing PR.

        Returns True when the existing PR path is handled.
        """
        parsed = parse_pr_url(pr_url)
        if not parsed:
            return False

        owner, repo, pr_number = parsed
        try:
            status = await asyncio.to_thread(self._github.get_pr_status, owner, repo, pr_number)
        except requests.RequestException:
            return False

        if status.state != PRState.OPEN:
            if status.state == PRState.MERGED:
                # PR already merged — close the task, don't dispatch again
                logger.info("PR %s already merged for %s — closing task", pr_url, issue.key)
                try:
                    await asyncio.to_thread(
                        lambda: self._tracker.transition_to_closed(
                            issue.key,
                            resolution=ResolutionType.FIXED,
                            comment=f"PR merged: {pr_url}",
                        )
                    )
                except requests.RequestException:
                    logger.warning("Failed to close %s after detecting merged PR", issue.key)
                self._recovery.clear(issue.key)
                await self._event_bus.publish(
                    Event(
                        type=EventType.TASK_COMPLETED,
                        task_key=issue.key,
                        data={"pr_url": pr_url, "merged": True},
                    )
                )
                return True  # Handled — skip dispatch
            # PR is CLOSED (rejected) — allow normal dispatch to retry
            return False

        # Look up persisted session_id for resumption
        resume_session_id: str | None = None
        if self._find_pr_session_id:
            resume_session_id = self._find_pr_session_id(issue.key)

        if resume_session_id:
            logger.info("Resuming PR %s for %s with session %s", pr_url, issue.key, resume_session_id)
        else:
            logger.info("Resuming PR %s for %s (fresh session)", pr_url, issue.key)

        session = await self._create_session_with_fallback(
            issue,
            workspace_server=workspace_server,
            cwd=cwd,
            resume_session_id=resume_session_id,
        )
        if session is None:
            self._cleanup_worktrees(issue.key, workspace_state.repo_paths)
            return True

        # Notify orchestrator about resumed PR via callback pattern
        result = AgentResult(
            success=True,
            output="",
            pr_url=pr_url,
            needs_info=False,
            resumed=True,
            cost_usd=0.0,
            duration_seconds=0.0,
        )
        tool_state = ToolState()
        try:
            await self._handle_result(issue, result, session, workspace_state, tool_state)
        except Exception as e:
            logger.error("Failed to handle resumed PR for %s: %s", issue.key, e)
            await session.close()
            state = self._recovery.record_failure(issue.key, e)
            self._cleanup_worktrees(issue.key, workspace_state.repo_paths)
            if state.should_retry:
                self._dispatched.discard(issue.key)
            await self._event_bus.publish(
                Event(
                    type=EventType.TASK_FAILED,
                    task_key=issue.key,
                    data={"error": str(e), "retryable": state.should_retry},
                )
            )
        return True
