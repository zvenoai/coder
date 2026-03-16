"""Main entry point — async orchestrator with concurrent task dispatch."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import signal
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Literal

import requests
from pythonjsonlogger.json import JsonFormatter as _JsonFormatter

from orchestrator.agent_mailbox import AgentInfo, AgentMailbox
from orchestrator.agent_runner import AgentRunner, AgentSession
from orchestrator.config import load_config
from orchestrator.constants import (
    CHAT_CHANNEL_KEY,
    HEARTBEAT_CHANNEL_KEY,
    TASKS_CHANNEL_KEY,
    TERMINAL_STATUS_KEYS,
    ChannelId,
    EventType,
    is_cancelled_status,
    is_resolved_status,
    is_review_status,
)
from orchestrator.dependency_manager import DependencyManager
from orchestrator.epic_coordinator import EpicCoordinator
from orchestrator.event_bus import Event, EventBus
from orchestrator.github_client import GitHubClient
from orchestrator.heartbeat import HeartbeatMonitor
from orchestrator.needs_info_monitor import NeedsInfoMonitor, TrackedNeedsInfo, is_needs_info_status
from orchestrator.orchestrator_agent import OrchestratorAgent
from orchestrator.post_merge_verifier import PostMergeVerifier
from orchestrator.pr_monitor import PRMonitor, find_pr_url_in_comments
from orchestrator.pre_merge_reviewer import PreMergeReviewer
from orchestrator.preflight_checker import PreflightChecker
from orchestrator.prompt_builder import build_fallback_context_prompt
from orchestrator.proposal_manager import ProposalManager, StoredProposal
from orchestrator.recovery import RecoveryManager
from orchestrator.repo_resolver import RepoResolver
from orchestrator.sqlite_storage import SQLiteStorage
from orchestrator.stats_collector import StatsCollector
from orchestrator.supervisor import SupervisorRunner
from orchestrator.supervisor_chat import SupervisorChatManager
from orchestrator.task_dispatcher import TaskDispatcher
from orchestrator.tracker_client import TrackerClient
from orchestrator.tracker_tools import ToolState
from orchestrator.tracker_types import TrackerCommentDict
from orchestrator.web import start_web_server
from orchestrator.workspace import WorkspaceManager
from orchestrator.workspace_tools import WorkspaceState


def _ensure_root_json_handler() -> logging.Handler:
    """Attach the orchestrator JSON root handler once."""
    for h in logging.root.handlers:
        if getattr(h, "_orch_json", False):
            return h
    handler = logging.StreamHandler()
    handler.setFormatter(
        _JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={
                "asctime": "ts",
                "levelname": "level",
                "name": "logger",
            },
        )
    )
    handler._orch_json = True  # type: ignore[attr-defined]
    logging.root.addHandler(handler)
    return handler


_handler = _ensure_root_json_handler()
logging.root.setLevel(logging.INFO)
logger = logging.getLogger(__name__)

RECONCILE_EVERY_N_POLLS = 5

# Terminal Tracker statuses that trigger agent cleanup
_TERMINAL_TRACKER_STATUSES = frozenset({"closed", "resolved", "done", "cancelled"})


def _build_key_query(keys: list[str]) -> str:
    """Build Tracker query for multiple issue keys."""
    quoted = ", ".join(f'"{k}"' for k in keys)
    return f"Key: {quoted}"


def _scan_worktree_dirs(base: Path, queue_prefix: str) -> list[str]:
    """Return issue keys from worktree base directory.

    Sync helper for ``_cleanup_stale_worktrees`` — keeps
    blocking pathlib I/O off the event loop.
    """
    if not base.exists():
        return []
    return [d.name for d in base.iterdir() if d.is_dir() and d.name.startswith(queue_prefix)]


def _format_event(e: Event) -> dict[str, Any]:
    """Format an Event into a serializable dict."""
    return {
        "type": e.type,
        "task_key": e.task_key,
        "timestamp": e.timestamp,
        "data": e.data,
    }


class Orchestrator:
    """Async orchestrator that polls Tracker and dispatches agents."""

    def __init__(self) -> None:
        self._config = load_config()
        self._tracker = TrackerClient(self._config.tracker_token, self._config.tracker_org_id)
        self._resolver = RepoResolver()
        self._workspace = WorkspaceManager(self._config.worktree_base_dir)
        self._storage: SQLiteStorage | None = SQLiteStorage(self._config.db_path)
        self._agent = AgentRunner(self._config, self._tracker, storage=self._storage)
        self._github = GitHubClient(self._config.github_token)
        self._event_bus = EventBus()
        self._max_concurrent_agents = self._config.max_concurrent_agents
        self._semaphore = asyncio.Semaphore(self._max_concurrent_agents)
        self._shutdown = asyncio.Event()
        self._dispatched: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._bot_login: str = ""
        self._mailbox = AgentMailbox()
        self._proposal_manager = ProposalManager(self._tracker, self._event_bus, self._config, storage=self._storage)
        self._epic_coordinator = EpicCoordinator(
            tracker=self._tracker,
            event_bus=self._event_bus,
            config=self._config,
            dispatched_set=self._dispatched,
            storage=self._storage,
        )
        self._dependency_manager = DependencyManager(
            tracker=self._tracker,
            event_bus=self._event_bus,
            storage=self._storage,
            config=self._config,
        )
        # Monitors and dispatcher will be initialized after bot_login is set
        self._needs_info_monitor: NeedsInfoMonitor | None = None
        self._pr_monitor: PRMonitor | None = None
        self._dispatcher: TaskDispatcher | None = None
        self._recovery = RecoveryManager(storage=self._storage)
        self._collector: StatsCollector | None = None
        # Supervisor: only memory system is used (watch/run/trigger removed)
        self._supervisor: SupervisorRunner | None = None
        if self._config.supervisor_enabled:
            self._supervisor = SupervisorRunner(
                config=self._config,
                storage=self._storage,
            )
        # Kubernetes client for supervisor pod inspection
        self._k8s_client = None
        if self._config.k8s_logs_enabled:
            from orchestrator.k8s_client import K8sClient

            self._k8s_client = K8sClient(namespace=self._config.k8s_namespace)
            if not self._k8s_client.available:
                logger.info("K8s client created but not available (not in cluster)")

        self._preflight_checker = PreflightChecker(
            tracker=self._tracker,
            github=self._github,
            config=self._config,
            storage=self._storage,
        )
        self._heartbeat: HeartbeatMonitor | None = None
        self._chat_manager: SupervisorChatManager | None = None
        self._tasks_manager: SupervisorChatManager | None = None
        self._heartbeat_manager: SupervisorChatManager | None = None
        if self._config.supervisor_enabled and self._supervisor is not None:
            _mgr_kwargs: dict[str, Any] = dict(
                config=self._config,
                event_bus=self._event_bus,
                tracker=self._tracker,
                get_pending_proposals=self._proposal_manager.get_pending_snapshot,
                get_recent_events=self._get_chat_recent_events,
                tracker_queue=self._config.tracker_queue,
                tracker_project_id=self._config.tracker_project_id,
                tracker_boards=self._config.tracker_boards,
                tracker_tag=self._config.tracker_tag,
                storage=self._storage,
                github=self._github,
                memory=self._supervisor.memory_index,
                embedder=self._supervisor.embedder,
                list_running_tasks_callback=self._list_running_tasks,
                send_message_callback=self.send_message_to_agent,
                abort_task_callback=self._abort_task,
                cancel_task_callback=self._cancel_task,
                epic_coordinator=self._epic_coordinator,
                mailbox=self._mailbox,
                k8s_client=self._k8s_client,
                dependency_manager=self._dependency_manager,
                preflight_checker=self._preflight_checker,
                mark_dispatched_callback=self._dispatched.add,
                remove_dispatched_callback=self._dispatched.discard,
                clear_recovery_callback=self._recovery.clear,
                get_state_callback=self.get_state,
                get_task_events_callback=self._get_task_events,
            )
            self._chat_manager = SupervisorChatManager(
                CHAT_CHANNEL_KEY,
                **_mgr_kwargs,
            )
            self._tasks_manager = SupervisorChatManager(
                TASKS_CHANNEL_KEY,
                **_mgr_kwargs,
            )
            self._heartbeat_manager = SupervisorChatManager(
                HEARTBEAT_CHANNEL_KEY,
                **_mgr_kwargs,
            )
            self._heartbeat = HeartbeatMonitor(
                config=self._config,
                event_bus=self._event_bus,
                list_running_tasks_callback=self._list_running_tasks,
                chat_manager=self._heartbeat_manager,
            )
            # Wire heartbeat_monitor into chat_manager for get_agent_health tool
            self._chat_manager.set_heartbeat_monitor(self._heartbeat)
        # On-demand sessions for chatting with completed tasks
        self._on_demand_sessions: dict[str, AgentSession] = {}
        # OrchestratorAgent replaces _handle_agent_result + supervisor triggers
        self._orchestrator_agent: OrchestratorAgent | None = None
        # Tracker status cache for heartbeat and reconciliation
        self._tracker_status_cache: dict[str, str] = {}

    async def run(self) -> None:
        """Main polling loop with graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._signal_handler)

        cfg = self._config
        logger.info(
            "AI Swarm Orchestrator started. Queue=%s, Tag=%s, Poll=%ds, MaxAgents=%d",
            cfg.tracker_queue,
            cfg.tracker_tag,
            cfg.poll_interval_seconds,
            cfg.max_concurrent_agents,
        )

        # Open stats database and start collector
        try:
            storage = self._storage
            if storage is not None:
                Path(self._config.db_path).parent.mkdir(parents=True, exist_ok=True)
                await storage.open()
                self._event_bus.set_storage(storage)
                await self._event_bus.cleanup_old_events()
                await self._event_bus.load()
                await self._event_bus.start()
                self._collector = StatsCollector(storage, self._event_bus)
                await self._collector.start()
                await self._recovery.load()
                await self._epic_coordinator.load()
                await self._proposal_manager.load()
                await self._dependency_manager.load()
                logger.info("Storage and stats collector started (db: %s)", self._config.db_path)
        except Exception:
            logger.warning("Failed to initialize stats — continuing without persistence", exc_info=True)
            # Stop collector if it was started before the failure
            if self._collector is not None:
                try:
                    await self._collector.stop()
                except Exception:
                    pass
            # Close storage if it was opened before the failure
            if storage is not None:
                try:
                    await storage.close()
                except Exception:
                    pass
            self._storage = None
            self._collector = None
            # Clear stale storage references in dependent components
            self._event_bus.disable_storage()
            self._recovery.disable_storage()
            self._epic_coordinator.disable_storage()
            self._proposal_manager.disable_storage()
            self._dependency_manager.disable_storage()
            if self._supervisor:
                self._supervisor.disable_storage()

        # Reconcile epic children stuck in DISPATCHED after restart
        try:
            await self._epic_coordinator.reconcile_dispatched_children()
        except Exception:
            logger.warning(
                "Failed to reconcile dispatched epic children",
                exc_info=True,
            )

        # Reconcile orphaned tasks (stuck "Running" after restart)
        try:
            await self._reconcile_orphaned_tasks()
        except Exception:
            logger.warning(
                "Failed to reconcile orphaned tasks",
                exc_info=True,
            )

        # Clean up worktrees for issues in terminal statuses
        await self._cleanup_stale_worktrees()

        # Identify bot login for comment filtering
        try:
            self._bot_login = await asyncio.to_thread(self._tracker.get_myself_login)
            logger.info("Bot identity: %s", self._bot_login)
        except requests.RequestException:
            logger.warning("Could not determine bot login — comment filtering may not work")

        # Initialize PreMergeReviewer for code review before auto-merge
        self._reviewer: PreMergeReviewer | None = None
        if self._config.auto_merge_enabled and self._config.pre_merge_review_enabled:
            self._reviewer = PreMergeReviewer(
                github=self._github,
                tracker=self._tracker,
                config=self._config,
            )

        # Initialize PostMergeVerifier for deployment verification
        self._verifier: PostMergeVerifier | None = None
        if self._config.post_merge_verification_enabled:
            self._verifier = PostMergeVerifier(
                github=self._github,
                tracker=self._tracker,
                k8s_client=self._k8s_client,
                storage=self._storage,
                config=self._config,
                event_bus=self._event_bus,
            )

        # Initialize PRMonitor after bot_login is set
        self._pr_monitor = PRMonitor(
            tracker=self._tracker,
            github=self._github,
            event_bus=self._event_bus,
            proposal_manager=self._proposal_manager,
            config=self._config,
            semaphore=self._semaphore,
            session_locks=self._session_locks,
            shutdown_event=self._shutdown,
            cleanup_worktrees_callback=self._cleanup_worktrees,
            dispatched_set=self._dispatched,
            storage=self._storage,
            mailbox=self._mailbox,
            reviewer=self._reviewer,
            verifier=self._verifier,
        )

        # Initialize NeedsInfoMonitor after bot_login is set
        self._needs_info_monitor = NeedsInfoMonitor(
            tracker=self._tracker,
            event_bus=self._event_bus,
            proposal_manager=self._proposal_manager,
            config=self._config,
            semaphore=self._semaphore,
            session_locks=self._session_locks,
            shutdown_event=self._shutdown,
            bot_login=self._bot_login,
            track_pr_callback=self._pr_monitor.track,
            cleanup_worktrees_callback=self._cleanup_worktrees,
            get_latest_comment_id_callback=self._get_latest_comment_id,
            dispatched_set=self._dispatched,
            record_failure_callback=self._recovery.record_failure,
            clear_recovery_callback=self._recovery.clear,
            storage=self._storage,
            mailbox=self._mailbox,
        )

        # Initialize OrchestratorAgent (replaces _handle_agent_result + supervisor triggers)

        def cleanup_session_lock(key: str) -> None:
            self._session_locks.pop(key, None)

        self._orchestrator_agent = OrchestratorAgent(
            event_bus=self._event_bus,
            tracker=self._tracker,
            pr_monitor=self._pr_monitor,
            needs_info_monitor=self._needs_info_monitor,
            recovery=self._recovery,
            dispatched_set=self._dispatched,
            cleanup_worktrees_callback=self._cleanup_worktrees,
            cleanup_session_lock=cleanup_session_lock,
            proposal_manager=self._proposal_manager,
            epic_state_store=self._epic_coordinator,
            mailbox=self._mailbox,
        )

        # Load persisted state for monitors
        if self._storage:
            try:
                await self._pr_monitor.load()
                await self._needs_info_monitor.load()
            except Exception:
                logger.warning("Failed to load monitor state from storage", exc_info=True)

        # Initialize TaskDispatcher
        self._dispatcher = TaskDispatcher(
            tracker=self._tracker,
            agent=self._agent,
            github=self._github,
            resolver=self._resolver,
            workspace=self._workspace,
            recovery=self._recovery,
            event_bus=self._event_bus,
            config=self._config,
            semaphore=self._semaphore,
            dispatched_set=self._dispatched,
            tasks_set=self._tasks,
            handle_result_callback=self._orchestrator_agent.handle_result,
            resume_needs_info_callback=self._resume_needs_info,
            find_existing_pr_callback=self._pr_monitor.find_existing_pr,
            cleanup_worktrees_callback=self._cleanup_worktrees,
            epic_coordinator=self._epic_coordinator,
            find_pr_session_id_callback=self._pr_monitor.get_persisted_session_id,
            find_ni_session_id_callback=self._needs_info_monitor.get_persisted_session_id,
            mailbox=self._mailbox,
            preflight_checker=self._preflight_checker,
            dependency_manager=self._dependency_manager,
        )

        # Set mailbox callbacks and event bus now that dispatcher and monitors are initialized
        self._mailbox.set_callbacks(
            list_agents=self._list_agent_info,
            interrupt_agent=self._interrupt_agent_for_comm,
        )
        self._mailbox.set_event_bus(self._event_bus)

        _web_managers: dict[ChannelId, Any] = {}
        if self._chat_manager is not None:
            _web_managers["chat"] = self._chat_manager
        if self._tasks_manager is not None:
            _web_managers["tasks"] = self._tasks_manager
        if self._heartbeat_manager is not None:
            _web_managers["heartbeat"] = self._heartbeat_manager
        web_task = asyncio.create_task(
            start_web_server(
                self._config,
                self._event_bus,
                self,
                storage=self._storage,
                chat_managers=_web_managers,
                tracker_client=self._tracker,
            ),
            name="web-server",
        )
        review_watcher = asyncio.create_task(self._pr_monitor.watch(), name="review-watcher")
        needs_info_watcher = asyncio.create_task(self._needs_info_monitor.watch(), name="needs-info-watcher")
        epic_event_forwarder = asyncio.create_task(self._watch_epic_events(), name="epic-event-forwarder")

        heartbeat_task: asyncio.Task | None = None
        if self._heartbeat is not None:
            heartbeat_task = asyncio.create_task(
                self._heartbeat.run(self._shutdown),
                name="heartbeat",
            )

        mailbox_cleanup_task = asyncio.create_task(
            self._periodic_mailbox_cleanup(),
            name="mailbox-cleanup",
        )

        reconcile_counter = 0

        while not self._shutdown.is_set():
            try:
                await self._dispatcher.poll_and_dispatch()
            except Exception:
                logger.exception("Error in polling loop")

            reconcile_counter += 1
            if reconcile_counter >= RECONCILE_EVERY_N_POLLS:
                reconcile_counter = 0
                try:
                    await self._epic_coordinator.reconcile_dispatched_children()
                except Exception:
                    logger.warning(
                        "Periodic epic reconciliation failed",
                        exc_info=True,
                    )
                try:
                    await self._reconcile_tracker_statuses()
                except Exception:
                    logger.warning(
                        "Tracker status reconciliation failed",
                        exc_info=True,
                    )

            # Sleep with shutdown awareness
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=cfg.poll_interval_seconds,
                )
            except TimeoutError:
                pass

        web_task.cancel()
        review_watcher.cancel()
        needs_info_watcher.cancel()
        epic_event_forwarder.cancel()
        mailbox_cleanup_task.cancel()
        cancel_tasks = [web_task, review_watcher, needs_info_watcher, epic_event_forwarder, mailbox_cleanup_task]
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            cancel_tasks.append(heartbeat_task)
        for t in cancel_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        # Drain fire-and-forget tasks BEFORE closing sessions —
        # in-flight _fire_and_forget_send holds session references and
        # would fail if sessions are closed underneath.
        if self._tasks:
            logger.info("Waiting for %d running task(s) to finish...", len(self._tasks))
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Close all supervisor chat channel sessions
        for _mgr in (self._chat_manager, self._tasks_manager, self._heartbeat_manager):
            if _mgr is not None:
                await _mgr.close()

        # Close all tracked sessions
        if self._pr_monitor:
            await self._pr_monitor.close_all()
        if self._needs_info_monitor:
            await self._needs_info_monitor.close_all()

        # Close all on-demand sessions
        for key in list(self._on_demand_sessions):
            await self._close_on_demand_session(key)

        # Final safety net: clear any remaining session locks
        self._session_locks.clear()

        # Drain pending background persistence writes
        for component in (self._epic_coordinator, self._proposal_manager, self._recovery, self._dependency_manager):
            await component.drain_background_tasks()
        if self._pr_monitor:
            await self._pr_monitor.drain_background_tasks()
        if self._needs_info_monitor:
            await self._needs_info_monitor.drain_background_tasks()

        # Close stats
        await self._event_bus.stop()
        if self._collector:
            await self._collector.stop()
        if self._storage:
            await self._storage.close()

        logger.info("Orchestrator stopped")

    async def _resume_needs_info(
        self,
        issue,
        session: AgentSession,
        workspace_state: WorkspaceState,
        tool_state: ToolState,
    ) -> bool:
        """Try to resume needs-info monitoring. Returns True if resumed."""
        if not self._needs_info_monitor or not self._needs_info_monitor.is_needs_info_status(issue.status):
            return False

        logger.info("Resuming needs-info monitoring for %s", issue.key)
        last_comment_id = await asyncio.to_thread(self._get_latest_comment_id, issue.key)
        self._needs_info_monitor.add(
            TrackedNeedsInfo(
                issue_key=issue.key,
                session=session,
                workspace_state=workspace_state,
                tool_state=tool_state,
                last_check_at=time.monotonic(),
                last_seen_comment_id=last_comment_id,
                issue_summary=issue.summary,
            )
        )
        await self._event_bus.publish(
            Event(
                type=EventType.NEEDS_INFO,
                task_key=issue.key,
                data={"resumed": True},
            )
        )
        return True

    def _get_latest_comment_id(self, issue_key: str) -> int:
        """Get the ID of the latest comment on an issue."""
        try:
            comments = self._tracker.get_comments(issue_key)
            if comments:
                return max(c.get("id", 0) for c in comments)
        except requests.RequestException:
            logger.warning("Failed to get latest comment ID for %s", issue_key)
        return 0

    def _cleanup_worktrees(self, issue_key: str, repo_paths: list[Path]) -> None:
        """Clean up worktrees for an issue."""
        try:
            self._workspace.cleanup_issue(issue_key, repo_paths)
        except Exception:
            logger.warning("Failed to cleanup worktrees for %s", issue_key)

    async def _cleanup_stale_worktrees(self) -> None:
        """Remove worktrees for issues in terminal statuses.

        Best-effort startup cleanup — Tracker API errors are logged
        but do not prevent the orchestrator from starting.
        """
        base = Path(self._config.worktree_base_dir)
        queue_prefix = f"{self._config.tracker_queue}-"
        issue_keys = await asyncio.to_thread(_scan_worktree_dirs, base, queue_prefix)
        if not issue_keys:
            return

        # Batch-query Tracker for all issue statuses
        try:
            keys_csv = ", ".join(f'"{k}"' for k in issue_keys)
            query = f"Key: {keys_csv}"
            issues = await asyncio.to_thread(self._tracker.search, query)
        except Exception:
            logger.warning(
                "Failed to query Tracker for stale worktree cleanup",
                exc_info=True,
            )
            return

        # Build set of found issue keys and their statuses
        found: dict[str, str] = {issue.key: issue.status for issue in issues}

        stale_keys: set[str] = set()
        for key in issue_keys:
            # Skip currently dispatched issues
            if key in self._dispatched:
                continue
            status = found.get(key)
            if status is None or status in TERMINAL_STATUS_KEYS:
                stale_keys.add(key)

        if not stale_keys:
            return

        repo_paths = [Path(r.path) for r in self._config.repos_config.all_repos]
        removed = await asyncio.to_thread(self._workspace.cleanup_stale, stale_keys, repo_paths)
        logger.info(
            "Stale worktree cleanup: removed %d/%d (total on disk: %d)",
            removed,
            len(stale_keys),
            len(issue_keys),
        )

    async def _reconcile_orphaned_tasks(self) -> None:
        """Reconcile tasks stuck as "Running" after a restart.

        After a pod restart, tasks that were in-flight lose their agent
        sessions.  The ``task_started`` event was persisted but no
        terminal event was written before the process died.  This method
        detects such orphans and publishes the appropriate terminal
        event based on the current Tracker status.
        """
        orphaned_keys = self._event_bus.get_orphaned_tasks()
        if not orphaned_keys:
            return

        # Batch-query Tracker for current statuses
        keys_csv = ", ".join(f'"{k}"' for k in orphaned_keys)
        query = f"Key: {keys_csv}"
        try:
            issues = await asyncio.to_thread(
                self._tracker.search,
                query,
            )
        except Exception:
            logger.warning(
                "Failed to query Tracker for orphaned task reconciliation",
                exc_info=True,
            )
            return

        status_map: dict[str, str] = {issue.key: issue.status for issue in issues}

        completed = 0
        failed = 0
        skipped = 0
        for key in orphaned_keys:
            status = status_map.get(key, "")
            if not status:
                logger.debug(
                    "Task %s not found in Tracker — treating as orphaned",
                    key,
                )
            if is_resolved_status(status):
                await self._event_bus.publish(
                    Event(
                        type=EventType.TASK_COMPLETED,
                        task_key=key,
                        data={"reconciled": True},
                    )
                )
                completed += 1
            elif is_cancelled_status(status):
                await self._event_bus.publish(
                    Event(
                        type=EventType.TASK_FAILED,
                        task_key=key,
                        data={
                            "reconciled": True,
                            "cancelled": True,
                            "error": "Cancelled (reconciled after restart)",
                        },
                    )
                )
                failed += 1
            elif is_needs_info_status(status):
                # Task is waiting for human input — not orphaned.
                # The poll loop will resume monitoring once monitors
                # are initialized.
                logger.info(
                    "Task %s is needs-info — skipping reconciliation",
                    key,
                )
                skipped += 1
            elif is_review_status(status):
                # Check if a PR exists in comments — if so, the agent
                # completed its work and the PR is under review.
                pr_url = await asyncio.to_thread(find_pr_url_in_comments, self._tracker, key)
                if pr_url:
                    logger.info(
                        "Task %s in review with PR %s — skipping reconciliation",
                        key,
                        pr_url,
                    )
                    skipped += 1
                else:
                    # Review status but no PR — truly orphaned
                    await self._event_bus.publish(
                        Event(
                            type=EventType.TASK_FAILED,
                            task_key=key,
                            data={
                                "reconciled": True,
                                "orphaned": True,
                                "error": "Orphaned after restart",
                            },
                        )
                    )
                    failed += 1
            else:
                # Still open — publish failure so the poll loop
                # can re-dispatch if appropriate.
                await self._event_bus.publish(
                    Event(
                        type=EventType.TASK_FAILED,
                        task_key=key,
                        data={
                            "reconciled": True,
                            "orphaned": True,
                            "error": "Orphaned after restart",
                        },
                    )
                )
                failed += 1

        logger.info(
            "Reconciled %d orphaned tasks (%d completed, %d failed, %d skipped)",
            len(orphaned_keys),
            completed,
            failed,
            skipped,
        )

    def get_state(self) -> dict:
        """Return a snapshot of orchestrator state for the dashboard."""
        return {
            "dispatched": list(self._dispatched),
            "active_tasks": [t.get_name() for t in self._tasks],
            "tracked_prs": (
                {
                    k: {
                        "pr_url": v.pr_url,
                        "issue_key": v.issue_key,
                        "last_check": v.last_check_at,
                    }
                    for k, v in self._pr_monitor.get_tracked().items()
                }
                if self._pr_monitor
                else {}
            ),
            "tracked_needs_info": (
                {
                    k: {
                        "issue_key": v.issue_key,
                        "last_check": v.last_check_at,
                        "last_seen_comment_id": v.last_seen_comment_id,
                    }
                    for k, v in self._needs_info_monitor.get_tracked().items()
                }
                if self._needs_info_monitor
                else {}
            ),
            "proposals": {
                pid: {
                    "id": p.id,
                    "source_task_key": p.source_task_key,
                    "summary": p.summary,
                    "description": p.description,
                    "component": p.component,
                    "category": p.category,
                    "status": p.status,
                    "created_at": p.created_at,
                    "tracker_issue_key": p.tracker_issue_key,
                }
                for pid, p in self._proposal_manager.get_all().items()
            },
            "supervisor": (
                {
                    "enabled": self._config.supervisor_enabled,
                    "running": self._supervisor.is_running,
                    "last_run_at": self._supervisor.last_run_at,
                    "queue_size": self._supervisor.queue_size,
                }
                if self._supervisor
                else None
            ),
            "running_sessions": (self._dispatcher.get_running_sessions() if self._dispatcher else []),
            "on_demand_sessions": list(self._on_demand_sessions.keys()),
            "epics": self._epic_coordinator.get_state() if self._epic_coordinator else {},
            "supervisor_chat": (
                dataclasses.asdict(info) if (info := self._chat_manager.get_session_info()) is not None else None
            )
            if self._chat_manager
            else None,
            "deferred_tasks": (
                {
                    k: {
                        "blockers": list(v.blockers),
                        "summary": v.issue_summary,
                        "manual": v.manual,
                    }
                    for k, v in (self._dependency_manager.get_deferred().items())
                }
                if self._dependency_manager
                else {}
            ),
            "config": {
                "queue": self._config.tracker_queue,
                "tag": self._config.tracker_tag,
                "max_agents": self._max_concurrent_agents,
            },
        }

    def set_max_agents(self, new_max: int) -> None:
        """Change max concurrent agents at runtime by resizing the semaphore."""
        if new_max < 1:
            raise ValueError("max_agents must be at least 1")
        old_max = self._max_concurrent_agents
        delta = new_max - old_max
        if delta > 0:
            for _ in range(delta):
                self._semaphore.release()
        elif delta < 0:
            self._semaphore._value = max(0, self._semaphore._value + delta)
        self._max_concurrent_agents = new_max

    def _fire_and_forget_supervisor(self, coro: Coroutine[Any, Any, Any], label: str, task_key: str) -> None:
        """Schedule a supervisor auto_send as a fire-and-forget task (non-blocking)."""

        async def _safe() -> None:
            try:
                await coro
            except Exception:
                logger.warning("Failed to send %s to supervisor for %s", label, task_key, exc_info=True)

        task = asyncio.get_running_loop().create_task(_safe())
        # prevent GC of the task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _watch_epic_events(self) -> None:
        """Forward task result events to epic coordinator and trigger supervisor for epic planning."""
        from orchestrator.supervisor_prompt_builder import (
            build_auto_merge_failed_prompt,
            build_epic_completion_prompt,
            build_epic_decompose_prompt,
            build_epic_plan_prompt,
            build_pre_merge_review_prompt,
            build_preflight_skip_prompt,
            build_task_deferred_prompt,
            build_task_unblocked_prompt,
        )

        queue = self._event_bus.subscribe_global()
        try:
            while not self._shutdown.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                # Epic child result forwarding (must be awaited — state transition)
                if self._orchestrator_agent:
                    if event.type in (EventType.TASK_COMPLETED, EventType.PR_MERGED):
                        await self._orchestrator_agent.handle_epic_child_event(event.task_key, "completed")
                    elif event.type == EventType.TASK_FAILED:
                        child_event: Literal["failed", "cancelled"] = (
                            "cancelled" if event.data.get("cancelled") else "failed"
                        )
                        await self._orchestrator_agent.handle_epic_child_event(event.task_key, child_event)

                # Clean up PR tracking on terminal task completion
                if event.type == EventType.TASK_COMPLETED and self._pr_monitor:
                    await self._pr_monitor.cleanup(event.task_key)

                # Supervisor autonomous epic planning (must be awaited — needs response before proceeding)
                if event.type == EventType.EPIC_AWAITING_PLAN and self._tasks_manager:
                    children = event.data.get("children", [])
                    epic_summary = ""
                    state = self._epic_coordinator.get_epic_state(event.task_key)
                    if state:
                        epic_summary = state.epic_summary
                    prompt = build_epic_plan_prompt(event.task_key, epic_summary, children)
                    try:
                        await self._tasks_manager.auto_send(prompt)
                    except Exception:
                        logger.warning(
                            "Failed to send epic plan request to supervisor for %s",
                            event.task_key,
                            exc_info=True,
                        )

                # Fire-and-forget notifications — don't block event processing

                # Clean up PR tracking for skipped tasks
                if event.type == EventType.TASK_SKIPPED and self._pr_monitor:
                    await self._pr_monitor.cleanup(event.task_key)

                # Supervisor notification: epic child skipped by preflight
                if event.type == EventType.TASK_SKIPPED and self._tasks_manager:
                    task_key = event.task_key
                    if self._epic_coordinator.is_epic_child(task_key):
                        epic_key = self._epic_coordinator.get_parent_epic_key(task_key)
                        if epic_key:
                            reason = str(event.data.get("reason", "unknown"))
                            source = str(event.data.get("source", "preflight_checker"))
                            prompt = build_preflight_skip_prompt(task_key, epic_key, reason, source)
                            self._fire_and_forget_supervisor(
                                self._tasks_manager.auto_send(prompt), "preflight skip", task_key
                            )

                # Epic completion: cleanup child PRs and notify supervisor
                if event.type == EventType.EPIC_COMPLETED:
                    epic_key = event.task_key
                    state = self._epic_coordinator.get_epic_state(epic_key)
                    if state:
                        # Clean up all child PRs to avoid stale PR polling
                        if self._pr_monitor:
                            for child_key in state.children:
                                await self._pr_monitor.cleanup(child_key)
                        # Supervisor notification: validate all children ran
                        if self._tasks_manager:
                            children_summary = [
                                {"key": c.key, "status": c.status.value} for c in state.children.values()
                            ]
                            prompt = build_epic_completion_prompt(epic_key, children_summary)
                            self._fire_and_forget_supervisor(
                                self._tasks_manager.auto_send(prompt), "epic completion", epic_key
                            )

                # Supervisor notification: task deferred due to dependencies
                if event.type == EventType.TASK_DEFERRED and self._tasks_manager:
                    blockers = event.data.get("blockers", [])
                    summary = str(event.data.get("summary", ""))
                    prompt = build_task_deferred_prompt(event.task_key, summary, blockers)
                    self._fire_and_forget_supervisor(
                        self._tasks_manager.auto_send(prompt), "task deferred", event.task_key
                    )

                # Supervisor notification: deferred task unblocked
                if event.type == EventType.TASK_UNBLOCKED and self._tasks_manager:
                    summary = str(event.data.get("summary", ""))
                    previous_blockers = event.data.get("previous_blockers", [])
                    prompt = build_task_unblocked_prompt(event.task_key, summary, previous_blockers)
                    self._fire_and_forget_supervisor(
                        self._tasks_manager.auto_send(prompt), "task unblocked", event.task_key
                    )

                # Supervisor autonomous epic decomposition
                if event.type == EventType.EPIC_NEEDS_DECOMPOSITION and self._tasks_manager:
                    epic_key = event.task_key
                    epic_summary = str(event.data.get("epic_summary", ""))
                    # Fetch full description for decomposition prompt
                    description = ""
                    try:
                        issue = await asyncio.to_thread(self._tracker.get_issue, epic_key)
                        description = issue.description or ""
                    except Exception:
                        logger.warning("Failed to fetch epic %s for decomposition", epic_key)
                    prompt = build_epic_decompose_prompt(epic_key, epic_summary, description)
                    self._fire_and_forget_supervisor(
                        self._tasks_manager.auto_send(prompt),
                        "epic decomposition",
                        epic_key,
                    )

                # Supervisor notification: auto-merge failed
                if event.type == EventType.PR_AUTO_MERGE_FAILED and self._tasks_manager:
                    pr_url = str(event.data.get("pr_url", ""))
                    reason = str(event.data.get("reason", "unknown"))
                    prompt = build_auto_merge_failed_prompt(event.task_key, pr_url, reason)
                    self._fire_and_forget_supervisor(
                        self._tasks_manager.auto_send(prompt), "auto-merge failed", event.task_key
                    )

                # Supervisor notification: pre-merge review rejected
                if (
                    event.type == EventType.PR_REVIEW_COMPLETED
                    and self._tasks_manager
                    and event.data.get("decision") == "reject"
                ):
                    pr_url = str(event.data.get("pr_url", ""))
                    summary = str(event.data.get("summary", ""))
                    issues = event.data.get("issues", [])
                    prompt = build_pre_merge_review_prompt(
                        event.task_key,
                        pr_url,
                        summary,
                        issues,
                    )
                    self._fire_and_forget_supervisor(
                        self._tasks_manager.auto_send(prompt),
                        "pre-merge review rejected",
                        event.task_key,
                    )
        finally:
            self._event_bus.unsubscribe_global(queue)

    async def _create_on_demand_session(self, task_key: str) -> tuple[AgentSession, str | None]:
        """Create an on-demand session by resuming a previous session_id.

        Registers the session in the mailbox for inter-agent communication.

        Returns:
            Tuple of (session, context_prompt). context_prompt is None when
            session resume succeeded (conversation history is intact). For
            fresh sessions (resume failed), context_prompt contains task
            description, comments, and message history.

        Raises:
            ValueError: If no session_id is available for the task.
        """
        session_id: str | None = None

        # 1. Check task_runs in storage
        if self._storage is not None:
            session_id = await self._storage.get_latest_session_id(task_key)

        # 2. Fallback to PR monitor persisted session_id
        if session_id is None and self._pr_monitor is not None:
            session_id = self._pr_monitor.get_persisted_session_id(task_key)

        # 3. Fallback to needs-info monitor persisted session_id
        if session_id is None and self._needs_info_monitor is not None:
            session_id = self._needs_info_monitor.get_persisted_session_id(
                task_key,
            )

        if session_id is None:
            raise ValueError(f"No session_id available for {task_key}")

        # Fetch issue from Tracker for session creation
        issue = await asyncio.to_thread(
            self._tracker.get_issue,
            task_key,
        )

        # Try to resume the previous session
        resumed = False
        try:
            session = await self._agent.create_session(
                issue,
                event_bus=self._event_bus,
                resume_session_id=session_id,
                mailbox=self._mailbox,
            )
            resumed = True
        except Exception:
            logger.warning(
                "Failed to resume session for %s, creating fresh",
                task_key,
                exc_info=True,
            )
            session = await self._agent.create_session(
                issue,
                event_bus=self._event_bus,
                mailbox=self._mailbox,
            )

        self._on_demand_sessions[task_key] = session
        logger.info("Created on-demand session for %s", task_key)

        # Register in mailbox for inter-agent communication
        component = issue.components[0] if issue.components else None
        self._mailbox.register_agent(task_key, component=component)

        # Build context prompt for fresh sessions only
        context_prompt: str | None = None
        if not resumed:
            # Fetch comments with timeout (only when needed)
            comments: list[TrackerCommentDict] | None = None
            try:
                async with asyncio.timeout(5):
                    comments = await asyncio.to_thread(
                        self._tracker.get_comments,
                        task_key,
                    )
            except Exception:
                logger.warning(
                    "Failed to fetch comments for %s fallback context",
                    task_key,
                    exc_info=True,
                )

            # Gather inter-agent message history
            messages = self._mailbox.get_all_messages(task_key)
            message_lines: list[str] = []
            for m in messages:
                line = f"[{m.sender_task_key} -> {m.target_task_key}] ({m.msg_type}): {m.text}"
                message_lines.append(line)
                if m.reply_text:
                    message_lines.append(f"  Reply: {m.reply_text}")

            context_prompt = build_fallback_context_prompt(
                issue,
                comments=comments,
                message_history=message_lines or None,
            )

        return session, context_prompt

    async def _close_on_demand_session(self, task_key: str) -> None:
        """Close an on-demand session and unregister from mailbox."""
        session = self._on_demand_sessions.pop(task_key, None)

        if session is not None:
            # Unregister from mailbox only if we actually had an on-demand session
            await self._mailbox.unregister_agent(task_key)
            try:
                await session.close()
            except Exception:
                logger.warning(
                    "Error closing on-demand session for %s",
                    task_key,
                    exc_info=True,
                )
            else:
                logger.info("Closed on-demand session for %s", task_key)
            self._session_locks.pop(task_key, None)

    async def _recreate_on_demand_session(self, task_key: str) -> tuple[AgentSession, str | None] | None:
        """Close a dead on-demand session and create a replacement.

        Preserves context by resuming from the last known session_id.
        Returns (new_session, context_prompt) tuple, or None if recreation fails.
        context_prompt is None when session resume succeeded, non-None for fresh sessions.

        Note: This method is called while holding the session lock,
        so it does NOT remove the lock to prevent race conditions.
        """
        logger.info(
            "Dead on-demand session detected for %s, recreating",
            task_key,
        )
        # Close the dead session manually without removing the lock
        # (we're being called from within the lock context)
        old_session = self._on_demand_sessions.pop(task_key, None)
        if old_session is not None:
            await self._mailbox.unregister_agent(task_key)
            try:
                await old_session.close()
            except Exception:
                logger.warning(
                    "Error closing on-demand session for %s",
                    task_key,
                    exc_info=True,
                )
            else:
                logger.info("Closed on-demand session for %s", task_key)
            # NOTE: Do NOT pop the lock here - caller holds it

        try:
            new_session, context_prompt = await self._create_on_demand_session(task_key)
        except Exception:
            logger.warning(
                "Failed to recreate on-demand session for %s",
                task_key,
                exc_info=True,
            )
            return None

        # Publish event for observability (failure is non-fatal)
        try:
            await self._event_bus.publish(
                Event(
                    type=EventType.SESSION_RECREATED,
                    task_key=task_key,
                    data={"task_key": task_key},
                )
            )
        except Exception:
            logger.warning(
                "Failed to publish SESSION_RECREATED event for %s",
                task_key,
                exc_info=True,
            )

        return new_session, context_prompt

    async def send_message_to_agent(self, task_key: str, message: str) -> None:
        """Send a user message to a running agent session.

        Lookup order:
        1. Dispatcher running sessions (interrupt — synchronous)
        2. PR monitor tracked sessions (fire-and-forget send)
        3. Needs-info monitor tracked sessions (fire-and-forget send)
        4. Existing on-demand session (fire-and-forget send)
        5. Create new on-demand session (fire-and-forget send)

        Session resolution and on-demand creation are synchronous so
        that ValueError propagates to the caller.  The actual
        ``session.send()`` is scheduled as a background task so the
        HTTP handler returns immediately.

        Raises:
            ValueError: If no session_id is available to create
                an on-demand session.
        """
        # Check dispatcher's running sessions first (agent actively executing)
        if self._dispatcher:
            running_session = self._dispatcher.get_running_session(task_key)
            if running_session is not None:
                await self._event_bus.publish(
                    Event(
                        type=EventType.USER_MESSAGE,
                        task_key=task_key,
                        data={"text": message},
                    )
                )
                await running_session.interrupt_with_message(message)
                return

        # Fall through to existing PR/needs-info session lookup
        session: AgentSession | None = None
        if self._pr_monitor and task_key in self._pr_monitor.get_tracked():
            session = self._pr_monitor.get_tracked()[task_key].session
        elif self._needs_info_monitor and task_key in self._needs_info_monitor.get_tracked():
            session = self._needs_info_monitor.get_tracked()[task_key].session

        # Fallback to on-demand session (reuse or create).
        # Pre-lock check avoids contention when _fire_and_forget_send
        # holds the lock during a long-running session.send().
        context_prompt: str | None = None
        if session is None:
            session = self._on_demand_sessions.get(task_key)

        # Lock only needed for session creation (double-checked locking).
        if session is None:
            lock = self._session_locks.setdefault(
                task_key,
                asyncio.Lock(),
            )
            async with lock:
                # Re-check after acquiring lock — another coroutine
                # may have created the session while we waited.
                session = self._on_demand_sessions.get(task_key)
                if session is None:
                    session, context_prompt = await self._create_on_demand_session(
                        task_key,
                    )

        # Prepend fallback context if this is a fresh session
        final_message = message
        if context_prompt is not None:
            final_message = context_prompt + "\n\n---\n\n" + message

        # Publish user_message event so it appears in the terminal
        await self._event_bus.publish(
            Event(
                type=EventType.USER_MESSAGE,
                task_key=task_key,
                data={"text": message},  # original message, not combined
            )
        )

        # Fire-and-forget: schedule send in background so the HTTP
        # handler returns immediately.  Agent output streams via
        # EventBus → WebSocket anyway.
        already_has_context = context_prompt is not None
        self._fire_and_forget_send(session, task_key, final_message, already_has_context)

    def _fire_and_forget_send(
        self,
        session: AgentSession,
        task_key: str,
        message: str,
        already_has_context: bool = False,
    ) -> None:
        """Schedule session.send() as a background task.

        Args:
            already_has_context: If True, the message already has context
                prepended (from send_message_to_agent). Recreation should
                not prepend again to avoid double-prepending.

        Multiple sends to the same task_key are serialized by the
        per-task session lock — no messages are silently dropped.

        If the session is dead (send() raises or returns success=False),
        and the session is an on-demand session, it will be recreated
        and the message will be retried.
        """

        async def _safe() -> None:
            current_session = session
            lock = self._session_locks.setdefault(
                task_key,
                asyncio.Lock(),
            )

            # Try to send message, with session recreation on failure
            result = None
            send_succeeded = False
            try:
                async with lock:
                    async with self._semaphore:
                        result = await current_session.send(message)
                    send_succeeded = True
                    # Detect dead session by failed result
                    if not result.success and task_key in self._on_demand_sessions:
                        # Check if session is still the same (no concurrent recreation)
                        if self._on_demand_sessions[task_key] is current_session:
                            # We detected the dead session first — recreate it
                            recreation_result = await self._recreate_on_demand_session(
                                task_key,
                            )
                            if recreation_result is not None:
                                new_session, context_prompt = recreation_result
                                # Prepend context only if message doesn't already have it
                                retry_message = message
                                if not already_has_context and context_prompt is not None:
                                    retry_message = context_prompt + "\n\n---\n\n" + message
                                try:
                                    async with self._semaphore:
                                        result = await new_session.send(retry_message)
                                except Exception:
                                    logger.warning(
                                        "Retry after recreation failed for %s",
                                        task_key,
                                        exc_info=True,
                                    )
                                    return
                            else:
                                logger.warning(
                                    "Failed to recreate session for %s after success=False",
                                    task_key,
                                )
                                return
                        else:
                            # Session already replaced by concurrent sender — use replacement
                            logger.info(
                                "Session for %s already recreated by concurrent sender after success=False, using replacement",
                                task_key,
                            )
                            replacement_session = self._on_demand_sessions.get(task_key)
                            if replacement_session is not None:
                                try:
                                    async with self._semaphore:
                                        result = await replacement_session.send(message)
                                except Exception:
                                    logger.warning(
                                        "Send on replacement session also failed for %s after success=False",
                                        task_key,
                                        exc_info=True,
                                    )
                                    return
                            else:
                                logger.warning(
                                    "Replacement session disappeared for %s after success=False",
                                    task_key,
                                )
                                return
            except Exception:
                # Session threw — attempt recreation if it's an on-demand session
                # Must re-acquire lock before recreation to prevent race conditions
                if task_key in self._on_demand_sessions:
                    async with lock:
                        # Re-check session identity inside lock — another concurrent
                        # sender may have already recreated it while we waited
                        if (
                            task_key not in self._on_demand_sessions
                            or self._on_demand_sessions[task_key] is not current_session
                        ):
                            # Session already replaced by another sender — use it
                            logger.info(
                                "Session for %s already recreated by concurrent sender, using new session",
                                task_key,
                            )
                            replacement_session = self._on_demand_sessions.get(task_key)
                            if replacement_session is not None:
                                try:
                                    async with self._semaphore:
                                        result = await replacement_session.send(message)
                                    send_succeeded = True
                                except Exception:
                                    logger.warning(
                                        "Send on replacement session also failed for %s",
                                        task_key,
                                        exc_info=True,
                                    )
                                    return
                            else:
                                logger.warning(
                                    "Replacement session disappeared for %s",
                                    task_key,
                                    exc_info=True,
                                )
                                return
                        else:
                            # We won the race — recreate the session ourselves
                            recreation_result = await self._recreate_on_demand_session(
                                task_key,
                            )
                            if recreation_result is not None:
                                new_session, context_prompt = recreation_result
                                # Prepend context only if message doesn't already have it
                                retry_message = message
                                if not already_has_context and context_prompt is not None:
                                    retry_message = context_prompt + "\n\n---\n\n" + message
                                try:
                                    async with self._semaphore:
                                        result = await new_session.send(retry_message)
                                    send_succeeded = True
                                except Exception:
                                    logger.warning(
                                        "Retry after recreation also failed for %s",
                                        task_key,
                                        exc_info=True,
                                    )
                                    return
                            else:
                                logger.warning(
                                    "Failed to send user message to %s",
                                    task_key,
                                    exc_info=True,
                                )
                                return
                else:
                    logger.warning(
                        "Failed to send user message to %s",
                        task_key,
                        exc_info=True,
                    )
                    return

            # Process result (only if send succeeded)
            if send_succeeded and result is not None:
                try:
                    if result.proposals:
                        await self._proposal_manager.process_proposals(
                            task_key,
                            result.proposals,
                        )
                    logger.info(
                        "User message to %s: success=%s, output_len=%d",
                        task_key,
                        result.success,
                        len(result.output),
                    )
                except Exception:
                    logger.warning(
                        "Failed to process proposals or log result for %s",
                        task_key,
                        exc_info=True,
                    )

        task = asyncio.create_task(_safe())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reconcile_tracker_statuses(self) -> None:
        """Check Tracker statuses for all active tasks and kill stale ones.

        Phase-aware cleanup ensures exactly one terminal event per task:
        - Running: publishes TASK_FAILED via _cancel_task
        - PR-tracked: cleanup only, NO new terminal event (PR_TRACKED already published)
        - Needs-info: publishes TASK_FAILED
        """
        from orchestrator.recovery import ErrorCategory

        # Collect all active task keys
        active_keys: list[str] = []
        if self._dispatcher:
            active_keys.extend(self._dispatcher.get_running_sessions())
        if self._pr_monitor:
            active_keys.extend(self._pr_monitor.get_tracked())
        if self._needs_info_monitor:
            active_keys.extend(self._needs_info_monitor.get_tracked())

        if not active_keys:
            return

        # Batch-query Tracker for current statuses
        try:
            query = _build_key_query(active_keys)
            issues = await asyncio.to_thread(
                self._tracker.search,
                query,
            )
        except Exception:
            logger.warning(
                "Tracker status reconciliation failed — skipping",
                exc_info=True,
            )
            return

        status_map: dict[str, str] = {issue.key: issue.status for issue in issues}

        # Populate cache for heartbeat
        self._tracker_status_cache.update(status_map)

        for key, status in status_map.items():
            if not (is_resolved_status(status) or is_cancelled_status(status)):
                continue

            # Phase-aware cleanup: mutually exclusive branches
            # Check each phase using LIVE sets (not stale snapshot)
            if self._dispatcher and self._dispatcher.get_running_session(key) is not None:
                # Running phase: _cancel_task publishes TASK_FAILED
                logger.info(
                    "Task %s is %s in Tracker — cancelling running agent",
                    key,
                    status,
                )
                try:
                    await self._cancel_task(
                        key,
                        f"Task closed in Tracker (status: {status})",
                    )
                except Exception:
                    logger.warning(
                        "Failed to cancel running task %s",
                        key,
                        exc_info=True,
                    )

            # NOTE: access _tracked_prs/_tracked directly (not
            # get_tracked()) to see ALL entries including removed
            # ones — avoids missing a concurrent removal race.
            elif self._pr_monitor and key in self._pr_monitor._tracked_prs:
                # PR-tracked phase: PR_TRACKED already published
                # — cleanup only, NO terminal event
                logger.info(
                    "Task %s is %s in Tracker — removing tracked PR",
                    key,
                    status,
                )
                try:
                    await self._pr_monitor.remove(key)
                    if self._storage:
                        await self._storage.record_pr_cancelled(
                            key,
                            time.time(),
                        )
                    self._dispatched.discard(key)
                    self._recovery.record_failure(
                        key,
                        f"Cancelled in Tracker: {status}",
                        category=ErrorCategory.CANCELLED,
                    )
                    try:
                        await asyncio.to_thread(
                            self._tracker.add_comment,
                            key,
                            "PR tracking stopped: task closed in Tracker.",
                        )
                    except requests.RequestException:
                        logger.warning(
                            "Failed to comment on %s",
                            key,
                        )
                except Exception:
                    logger.warning(
                        "Failed to remove tracked PR for %s",
                        key,
                        exc_info=True,
                    )

            elif self._needs_info_monitor and key in self._needs_info_monitor._tracked:
                # Needs-info phase: NEEDS_INFO is intermediate
                # — publish TASK_FAILED
                logger.info(
                    "Task %s is %s in Tracker — removing needs-info monitor",
                    key,
                    status,
                )
                try:
                    await self._needs_info_monitor.remove(key)
                    self._recovery.record_failure(
                        key,
                        f"Cancelled in Tracker: {status}",
                        category=ErrorCategory.CANCELLED,
                    )
                    await self._event_bus.publish(
                        Event(
                            type=EventType.TASK_FAILED,
                            task_key=key,
                            data={
                                "error": (f"Task closed in Tracker (status: {status})"),
                                "cancelled": True,
                                "retryable": False,
                            },
                        )
                    )
                except Exception:
                    logger.warning(
                        "Failed to remove needs-info for %s",
                        key,
                        exc_info=True,
                    )

    def _list_running_tasks(self) -> list[dict[str, object]]:
        """List all agent tasks with active sessions."""
        tasks: list[dict[str, object]] = []
        seen: set[str] = set()
        cache = self._tracker_status_cache
        if self._dispatcher:
            for key in self._dispatcher.get_running_sessions():
                session = self._dispatcher.get_running_session(key)
                tasks.append(
                    {
                        "task_key": key,
                        "status": "running",
                        "tracker_status": cache.get(
                            key,
                            "",
                        ),
                        "cost_usd": (
                            self._dispatcher.get_task_cost(
                                key,
                            )
                        ),
                        "input_tokens": (session.cumulative_input_tokens if session else 0),
                        "output_tokens": (session.cumulative_output_tokens if session else 0),
                    }
                )
                seen.add(key)
        if self._pr_monitor:
            for key, info in self._pr_monitor.get_tracked().items():
                tasks.append(
                    {
                        "task_key": key,
                        "status": "in_review",
                        "pr_url": info.pr_url,
                        "tracker_status": cache.get(
                            key,
                            "",
                        ),
                        "cost_usd": None,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    }
                )
                seen.add(key)
        if self._needs_info_monitor:
            for key in self._needs_info_monitor.get_tracked():
                tasks.append(
                    {
                        "task_key": key,
                        "status": "needs_info",
                        "tracker_status": cache.get(
                            key,
                            "",
                        ),
                        "cost_usd": None,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    }
                )
                seen.add(key)
        for key in self._on_demand_sessions:
            if key not in seen:
                tasks.append(
                    {
                        "task_key": key,
                        "status": "on_demand",
                        "tracker_status": cache.get(
                            key,
                            "",
                        ),
                        "cost_usd": None,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    }
                )
        return tasks

    async def _periodic_mailbox_cleanup(self) -> None:
        """Remove old terminal messages from the mailbox every 30 minutes."""
        while True:
            await asyncio.sleep(1800)
            removed = self._mailbox.cleanup_terminal_messages()
            if removed > 0:
                logger.info(
                    "Mailbox cleanup: removed %d terminal messages",
                    removed,
                )

    async def _list_agent_info(self) -> list[AgentInfo]:
        """List all agents with active sessions for inter-agent discovery.

        Includes agents in all states: actively running, waiting for PR review,
        waiting for human info, and on-demand sessions. All have live sessions
        reachable via mailbox.
        """
        agents: list[AgentInfo] = []
        seen: set[str] = set()
        if self._dispatcher:
            for key in self._dispatcher.get_running_sessions():
                if not self._mailbox.is_registered(key):
                    continue
                summary = self._get_task_summary(key)
                meta = self._mailbox.get_agent_metadata(key)
                agents.append(
                    AgentInfo(
                        task_key=key,
                        task_summary=summary,
                        status="running",
                        component=meta.get("component"),
                        repo=meta.get("repo"),
                    )
                )
                seen.add(key)
        if self._pr_monitor:
            for key, tracked_pr in self._pr_monitor.get_tracked().items():
                if key in seen or not self._mailbox.is_registered(key):
                    continue
                summary = tracked_pr.issue_summary or self._get_task_summary(key)
                meta = self._mailbox.get_agent_metadata(key)
                agents.append(
                    AgentInfo(
                        task_key=key,
                        task_summary=summary,
                        status="in_review",
                        component=meta.get("component"),
                        repo=meta.get("repo"),
                    )
                )
                seen.add(key)
        if self._needs_info_monitor:
            for key, _tracked_ni in self._needs_info_monitor.get_tracked().items():
                if key in seen or not self._mailbox.is_registered(key):
                    continue
                summary = self._get_task_summary(key)
                meta = self._mailbox.get_agent_metadata(key)
                agents.append(
                    AgentInfo(
                        task_key=key,
                        task_summary=summary,
                        status="needs_info",
                        component=meta.get("component"),
                        repo=meta.get("repo"),
                    )
                )
                seen.add(key)

        # Include on-demand sessions
        for key in self._on_demand_sessions:
            if key in seen or not self._mailbox.is_registered(key):
                continue
            summary = self._get_task_summary(key)
            meta = self._mailbox.get_agent_metadata(key)
            agents.append(
                AgentInfo(
                    task_key=key,
                    task_summary=summary,
                    status="on_demand",
                    component=meta.get("component"),
                    repo=meta.get("repo"),
                )
            )
            seen.add(key)

        return agents

    async def _interrupt_agent_for_comm(self, task_key: str, message: str) -> None:
        """Interrupt an agent session to deliver an inter-agent message.

        Lookup order:
        1. Dispatcher / PR monitor / needs-info live sessions (interrupt)
        2. On-demand sessions (interrupt)
        3. Resume saved session via send_message_to_agent (fire-and-forget)
        """
        session: AgentSession | None = None
        if self._dispatcher:
            session = self._dispatcher.get_running_session(task_key)
        if session is None and self._pr_monitor:
            tracked_pr = self._pr_monitor.get_tracked().get(task_key)
            if tracked_pr is not None:
                session = tracked_pr.session
        if session is None and self._needs_info_monitor:
            tracked_ni = self._needs_info_monitor.get_tracked().get(task_key)
            if tracked_ni is not None:
                session = tracked_ni.session
        if session is not None:
            await session.interrupt_with_message(message)
            return

        # No live session — check on-demand sessions
        on_demand = self._on_demand_sessions.get(task_key)
        if on_demand is not None:
            try:
                await on_demand.interrupt_with_message(message)
                return
            except Exception:
                logger.warning(
                    "Dead on-demand session for %s, cleaning up",
                    task_key,
                    exc_info=True,
                )
                await self._close_on_demand_session(task_key)
                # Fall through to send_message_to_agent which
                # will create a new on-demand session

        # Last resort: resume saved session via send_message_to_agent.
        # The agent gets the message in full conversation context and
        # can act on it.  Output streams to the dashboard terminal.
        try:
            await self.send_message_to_agent(task_key, message)
        except Exception:
            logger.warning(
                "Cannot deliver inter-agent message to %s: no active session or fallback failed",
                task_key,
                exc_info=True,
            )

    def _get_task_summary(self, task_key: str) -> str:
        """Get task summary from event history."""
        history = self._event_bus.get_task_history(task_key)
        for event in history:
            if event.type == EventType.TASK_STARTED:
                return str(event.data.get("summary", task_key))
        return task_key

    async def _abort_task(self, task_key: str, *, cancelled: bool = False) -> None:
        """Abort a running agent task — interrupt session and clean up.

        Args:
            cancelled: If True, marks the event as a cancellation (not just a failure).
        """
        if self._dispatcher:
            session = self._dispatcher.get_running_session(task_key)
            if session is not None:
                try:
                    await session.interrupt()
                except Exception:
                    logger.warning("Error interrupting session for %s", task_key)
                await session.close()
                self._dispatcher.remove_running_session(task_key)
                await self._mailbox.unregister_agent(task_key)
                self._dispatched.discard(task_key)
                self._session_locks.pop(task_key, None)
                data: dict[str, object] = {"error": "Aborted by supervisor", "retryable": False}
                if cancelled:
                    data["cancelled"] = True
                await self._event_bus.publish(Event(type=EventType.TASK_FAILED, task_key=task_key, data=data))
                return
        raise ValueError(f"No running session for {task_key}")

    async def _cancel_task(self, task_key: str, reason: str) -> None:
        """Cancel a task — abort if running, remove from dispatch, post comment."""
        from orchestrator.recovery import ErrorCategory

        # Abort if currently running
        aborted = False
        try:
            await self._abort_task(task_key, cancelled=True)
            aborted = True
        except ValueError:
            pass  # Not running — that's fine

        # Remove from dispatched set to prevent re-dispatch
        self._dispatched.discard(task_key)

        # Record as CANCELLED (non-retryable) to prevent re-dispatch on next poll
        self._recovery.record_failure(task_key, f"Cancelled: {reason}", category=ErrorCategory.CANCELLED)

        # If task wasn't running, publish cancellation event for epic coordinator
        if not aborted:
            await self._event_bus.publish(
                Event(
                    type=EventType.TASK_FAILED,
                    task_key=task_key,
                    data={"error": f"Cancelled: {reason}", "retryable": False, "cancelled": True},
                )
            )

        # Post comment to tracker
        try:
            await asyncio.to_thread(
                self._tracker.add_comment,
                task_key,
                f"Task cancelled by supervisor: {reason}",
            )
        except requests.RequestException:
            logger.warning("Failed to comment cancellation on %s", task_key)

    async def approve_proposal(self, proposal_id: str) -> StoredProposal:
        """Approve a proposal — delegates to ProposalManager."""
        return await self._proposal_manager.approve(proposal_id)

    async def reject_proposal(self, proposal_id: str) -> StoredProposal:
        """Reject a proposal — delegates to ProposalManager."""
        return await self._proposal_manager.reject(proposal_id)

    def _get_chat_recent_events(
        self,
        count: int,
    ) -> list[dict[str, Any]]:
        """Get recent events snapshot for supervisor chat tools."""
        events = self._event_bus.get_global_history()[-count:]
        return [_format_event(e) for e in events]

    def _get_task_events(
        self,
        task_key: str,
    ) -> list[dict[str, Any]]:
        """Get event history for a specific task."""
        events = self._event_bus.get_task_history(task_key)
        return [_format_event(e) for e in events]

    def _signal_handler(self) -> None:
        logger.info("Shutdown signal received")
        self._shutdown.set()


def main() -> None:
    """Entry point."""
    orchestrator = Orchestrator()
    asyncio.run(orchestrator.run())


if __name__ == "__main__":
    main()
