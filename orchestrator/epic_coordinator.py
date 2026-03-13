"""Epic coordinator — state store + child readiness management for epic tasks."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

import requests

from orchestrator._persistence import BackgroundPersistenceMixin
from orchestrator.constants import EventType

if TYPE_CHECKING:
    from orchestrator.config import Config
    from orchestrator.event_bus import EventBus
    from orchestrator.storage import Storage
    from orchestrator.tracker_client import TrackerClient, TrackerIssue
    from orchestrator.tracker_types import TrackerLinkDict

from orchestrator.constants import CHILD_LINK_HINTS, is_cancelled_status, is_resolved_status
from orchestrator.event_bus import Event

logger = logging.getLogger(__name__)

EpicPhase = Literal[
    "analyzing",
    "awaiting_plan",
    "needs_decomposition",
    "executing",
    "completed",
]


class ChildStatus(StrEnum):
    """Child task execution status inside epic flow."""

    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ChildTask:
    """Tracked child task."""

    key: str
    summary: str
    status: ChildStatus
    depends_on: list[str]
    tracker_status: str
    last_comment_id: int = 0
    tags: list[str] = field(default_factory=list)


@dataclass
class EpicState:
    """Tracked epic state."""

    epic_key: str
    epic_summary: str
    children: dict[str, ChildTask]
    phase: EpicPhase = "analyzing"
    created_at: float = 0.0


class EpicCoordinator(BackgroundPersistenceMixin):
    """State store + child readiness management for epic tasks.

    Manages the full epic lifecycle: registration, child discovery
    and analysis (via analyze_and_activate), state persistence,
    child status transitions, and readiness tagging.
    """

    def __init__(
        self,
        tracker: TrackerClient,
        event_bus: EventBus,
        config: Config,
        dispatched_set: set[str],
        storage: Storage | None = None,
    ) -> None:
        self._tracker = tracker
        self._event_bus = event_bus
        self._config = config
        self._dispatched = dispatched_set
        self._storage = storage
        self._epics: dict[str, EpicState] = {}
        self._init_persistence()

    async def load(self) -> None:
        """Load epic state from storage. Call after Storage.open()."""
        if not self._storage:
            return
        try:
            records = await self._storage.load_epic_states()
            for rec in records:
                children_records = await self._storage.load_epic_children(rec.epic_key)
                children: dict[str, ChildTask] = {}
                for cr in children_records:
                    children[cr.child_key] = ChildTask(
                        key=cr.child_key,
                        summary=cr.summary,
                        status=ChildStatus(cr.status),
                        depends_on=cr.depends_on,
                        tracker_status=cr.tracker_status,
                        last_comment_id=cr.last_comment_id,
                        tags=cr.tags,
                    )
                self._epics[rec.epic_key] = EpicState(
                    epic_key=rec.epic_key,
                    epic_summary=rec.epic_summary,
                    children=children,
                    phase=cast(EpicPhase, rec.phase),
                    created_at=rec.created_at,
                )
                self._dispatched.add(rec.epic_key)
            if records:
                logger.info("Loaded %d epic(s) from storage", len(records))
        except Exception:
            logger.warning("Failed to load epic state from storage", exc_info=True)

    def _persist_epic(self, epic_key: str) -> None:
        """Schedule async persistence of epic state + children to storage."""
        if not self._storage:
            return

        from orchestrator.stats_models import EpicChildRecord, EpicStateRecord

        state = self._epics.get(epic_key)
        if not state:
            return

        record = EpicStateRecord(
            epic_key=state.epic_key,
            epic_summary=state.epic_summary,
            phase=state.phase,
            created_at=state.created_at,
        )
        child_records = [
            EpicChildRecord(
                child_key=c.key,
                summary=c.summary,
                status=c.status.value,
                depends_on=c.depends_on,
                tracker_status=c.tracker_status,
                last_comment_id=c.last_comment_id,
                tags=c.tags,
            )
            for c in state.children.values()
        ]

        async def _write() -> None:
            async with self._key_locks[epic_key]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.upsert_epic_state(record)
                    for cr in child_records:
                        await self._storage.upsert_epic_child(epic_key, cr)
                except Exception:
                    logger.warning("Failed to persist epic state for %s", epic_key, exc_info=True)

        self._schedule_task(_write())

    def _persist_delete_epic(self, epic_key: str) -> None:
        """Schedule async deletion of epic from storage."""
        if not self._storage:
            return

        async def _delete() -> None:
            async with self._key_locks[epic_key]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.delete_epic(epic_key)
                except Exception:
                    logger.warning("Failed to delete epic %s from storage", epic_key, exc_info=True)

        self._schedule_task(_delete())

    # ------------------------------------------------------------------
    # Registration & state transitions
    # ------------------------------------------------------------------

    async def register_epic(self, issue: TrackerIssue) -> None:
        """Register epic and block direct dispatch for it."""
        if issue.key in self._epics:
            return
        self._epics[issue.key] = EpicState(
            epic_key=issue.key,
            epic_summary=issue.summary,
            children={},
            phase="analyzing",
            created_at=time.time(),
        )
        self._dispatched.add(issue.key)
        self._persist_epic(issue.key)
        await self._event_bus.publish(
            Event(
                type=EventType.EPIC_DETECTED,
                task_key=issue.key,
                data={"summary": issue.summary},
            )
        )

    async def analyze_and_activate(self, epic_key: str) -> None:
        """Discover epic children, set up dependency graph, and tag ready children.

        Performs the full analysis pipeline:
        1. Fetch children from Tracker
        2. Build ChildTask objects (detecting already-resolved/cancelled)
        3. Set children on the epic state
        4. Set flat dependencies (no inter-child deps) and transition to 'executing'
        5. Tag ready children with ai-task for dispatch
        """
        state = self._epics.get(epic_key)
        if not state:
            logger.warning("analyze_and_activate called for unknown epic %s", epic_key)
            return

        child_issues = await asyncio.to_thread(self.fetch_children, epic_key)
        if not child_issues:
            logger.info("Epic %s has no children — nothing to activate", epic_key)
            state.phase = "executing"
            self._persist_epic(epic_key)
            await self._check_epic_completed(epic_key)
            return

        children: dict[str, ChildTask] = {}
        for issue in child_issues:
            children[issue.key] = self.build_child_task(issue)

        self.set_children(epic_key, children)
        self.set_child_dependencies(epic_key, {key: [] for key in children})
        await self._tag_ready_children(epic_key)
        logger.info(
            "Epic %s analyzed: %d children discovered, phase=%s",
            epic_key,
            len(children),
            state.phase,
        )

    async def discover_children(self, epic_key: str) -> None:
        """Discover epic children and wait for supervisor plan.

        Unlike analyze_and_activate, this method does NOT set dependencies
        or tag children. It sets phase to 'awaiting_plan' so the supervisor
        can set the dependency graph and activation order via MCP tools.
        """
        state = self._epics.get(epic_key)
        if not state:
            logger.warning("discover_children called for unknown epic %s", epic_key)
            return

        child_issues = await asyncio.to_thread(self.fetch_children, epic_key)

        children: dict[str, ChildTask] = {}
        for issue in child_issues:
            children[issue.key] = self.build_child_task(issue)

        if not children:
            # No children found — needs decomposition by supervisor
            state.phase = "needs_decomposition"
            self._persist_epic(epic_key)
            await self._event_bus.publish(
                Event(
                    type=EventType.EPIC_NEEDS_DECOMPOSITION,
                    task_key=epic_key,
                    data={"epic_summary": state.epic_summary},
                )
            )
            logger.info(
                "Epic %s has no children, needs decomposition",
                epic_key,
            )
            return

        self.set_children(epic_key, children)
        state.phase = "awaiting_plan"
        self._persist_epic(epic_key)

        children_summary = [{"key": c.key, "summary": c.summary, "status": c.status.value} for c in children.values()]
        await self._event_bus.publish(
            Event(
                type=EventType.EPIC_AWAITING_PLAN,
                task_key=epic_key,
                data={"children": children_summary},
            )
        )
        logger.info(
            "Epic %s discovered %d children, awaiting supervisor plan",
            epic_key,
            len(children),
        )

    async def activate_child(self, epic_key: str, child_key: str) -> bool:
        """Manually activate a single child task.

        Tags the child with ai-task and sets status to READY.
        Transitions epic phase to 'executing' if in 'awaiting_plan'.

        Returns True on success, False if child not found, already terminal, or tag failed.
        """
        state = self._epics.get(epic_key)
        if not state:
            logger.warning("activate_child called for unknown epic %s", epic_key)
            return False

        child = state.children.get(child_key)
        if not child:
            logger.warning("activate_child: child %s not found in epic %s", child_key, epic_key)
            return False

        if child.status in (ChildStatus.COMPLETED, ChildStatus.CANCELLED):
            logger.info("activate_child: child %s is already %s", child_key, child.status.value)
            return False

        # Tag child with ai-task
        if self._config.tracker_tag not in child.tags:
            updated_tags = [*child.tags, self._config.tracker_tag]
            try:
                await asyncio.to_thread(self._tracker.update_issue_tags, child.key, updated_tags)
            except requests.RequestException:
                logger.warning("Failed to tag child %s for activation", child.key)
                return False
            child.tags = updated_tags

        child.status = ChildStatus.READY

        # Transition phase to executing
        if state.phase in ("awaiting_plan", "needs_decomposition"):
            state.phase = "executing"

        self._persist_epic(epic_key)

        await self._event_bus.publish(
            Event(
                type=EventType.EPIC_CHILD_READY,
                task_key=epic_key,
                data={"child_key": child.key, "manual": True},
            )
        )
        return True

    def register_child(self, epic_key: str, child: ChildTask) -> bool:
        """Register a newly created child task in the epic state.

        Used after creating a subtask via Tracker API.
        Returns True on success, False if epic not found.
        """
        state = self._epics.get(epic_key)
        if not state:
            logger.warning(
                "register_child: epic %s not found",
                epic_key,
            )
            return False

        state.children[child.key] = child
        # Transition from needs_decomposition → awaiting_plan now that
        # at least one child exists for the supervisor to plan.
        if state.phase == "needs_decomposition":
            state.phase = "awaiting_plan"
        self._persist_epic(epic_key)
        logger.info(
            "Registered child %s in epic %s",
            child.key,
            epic_key,
        )
        return True

    async def rediscover_children(self, epic_key: str) -> int:
        """Re-fetch children from Tracker, merging with existing state.

        New children are added as PENDING. Existing children keep
        their current status.

        Returns the number of new children discovered.
        """
        state = self._epics.get(epic_key)
        if not state:
            logger.warning(
                "rediscover_children: epic %s not found",
                epic_key,
            )
            return 0

        child_issues = await asyncio.to_thread(self.fetch_children, epic_key)
        new_count = 0
        for issue in child_issues:
            if issue.key not in state.children:
                state.children[issue.key] = self.build_child_task(issue)
                new_count += 1

        if new_count:
            self._persist_epic(epic_key)
            logger.info(
                "Rediscovered %d new children for epic %s",
                new_count,
                epic_key,
            )
        return new_count

    def set_children(self, epic_key: str, children: dict[str, ChildTask]) -> None:
        """Set children for an epic (called after discovery by orchestrator agent)."""
        state = self._epics.get(epic_key)
        if not state:
            logger.warning("set_children called for unknown epic %s", epic_key)
            return
        state.children = children
        self._persist_epic(epic_key)

    def set_child_dependencies(self, epic_key: str, dependencies: dict[str, list[str]]) -> None:
        """Set dependency map for epic children and transition to executing."""
        state = self._epics.get(epic_key)
        if not state:
            logger.warning("set_child_dependencies called for unknown epic %s", epic_key)
            return
        for child_key, deps in dependencies.items():
            if child_key in state.children:
                state.children[child_key].depends_on = deps
        state.phase = "executing"
        self._persist_epic(epic_key)

    async def on_task_completed(self, task_key: str) -> None:
        """Handle child completion and unblock dependents."""
        located = self._find_epic_for_child(task_key)
        if not located:
            return
        epic_key, child = located
        if child.status in (ChildStatus.COMPLETED, ChildStatus.CANCELLED):
            return
        child.status = ChildStatus.COMPLETED
        self._persist_epic(epic_key)
        await self._tag_ready_children(epic_key)
        await self._check_epic_completed(epic_key)

    async def on_task_failed(self, task_key: str) -> None:
        """Handle child failure and keep dependents blocked."""
        located = self._find_epic_for_child(task_key)
        if not located:
            return
        epic_key, child = located
        if child.status in (ChildStatus.COMPLETED, ChildStatus.CANCELLED, ChildStatus.FAILED):
            return
        child.status = ChildStatus.FAILED
        self._persist_epic(epic_key)

    async def on_task_cancelled(self, task_key: str) -> None:
        """Handle child cancellation and unblock dependents."""
        located = self._find_epic_for_child(task_key)
        if not located:
            return
        epic_key, child = located
        if child.status in (ChildStatus.COMPLETED, ChildStatus.CANCELLED):
            return
        child.status = ChildStatus.CANCELLED
        self._persist_epic(epic_key)
        await self._tag_ready_children(epic_key)
        await self._check_epic_completed(epic_key)

    # ------------------------------------------------------------------
    # State lookups and queries
    # ------------------------------------------------------------------

    async def reset_child(self, epic_key: str, child_key: str) -> bool:
        """Reset a terminal child back to PENDING for re-dispatch.

        Only accepts children in terminal states (COMPLETED, FAILED, CANCELLED).
        Does NOT reset DISPATCHED or READY children (agent running or about to).

        Removes child from dispatched set, re-runs _tag_ready_children if
        phase is 'executing', and publishes EPIC_CHILD_RESET event.

        Returns True on success, False if epic/child not found or not in terminal state.
        """
        state = self._epics.get(epic_key)
        if not state:
            logger.warning("reset_child called for unknown epic %s", epic_key)
            return False

        child = state.children.get(child_key)
        if not child:
            logger.warning("reset_child: child %s not found in epic %s", child_key, epic_key)
            return False

        terminal_statuses = (ChildStatus.COMPLETED, ChildStatus.FAILED, ChildStatus.CANCELLED)
        if child.status not in terminal_statuses:
            logger.info(
                "reset_child: child %s is %s (not terminal) — refusing reset",
                child_key,
                child.status.value,
            )
            return False

        previous_status = child.status.value
        child.status = ChildStatus.PENDING
        self._dispatched.discard(child_key)
        self._persist_epic(epic_key)

        await self._event_bus.publish(
            Event(
                type=EventType.EPIC_CHILD_RESET,
                task_key=epic_key,
                data={"child_key": child_key, "previous_status": previous_status},
            )
        )

        if state.phase == "executing":
            await self._tag_ready_children(epic_key)

        logger.info("Reset child %s in epic %s from %s to PENDING", child_key, epic_key, previous_status)
        return True

    def revert_dispatched_to_ready(self, child_key: str) -> bool:
        """Revert a DISPATCHED child back to READY for re-dispatch.

        Used when a child was marked DISPATCHED but then deferred for
        preflight review. Supervisor approved dispatch, so child needs
        to go back to READY to be picked up by dispatcher.

        Returns True on success, False if not found or wrong status.
        """
        located = self._find_epic_for_child(child_key)
        if not located:
            return False
        epic_key, child = located
        if child.status != ChildStatus.DISPATCHED:
            return False
        child.status = ChildStatus.READY
        self._persist_epic(epic_key)
        return True

    def get_parent_epic_key(self, child_key: str) -> str | None:
        """Return the epic key for a child, or None if not an epic child."""
        located = self._find_epic_for_child(child_key)
        if located:
            return located[0]
        return None

    def is_epic_child(self, issue_key: str) -> bool:
        """Check if issue is currently tracked as a child of any epic."""
        return any(issue_key in state.children for state in self._epics.values())

    def is_child_ready_for_dispatch(self, issue_key: str) -> bool:
        """Check whether a tracked epic child is currently ready for dispatch."""
        located = self._find_epic_for_child(issue_key)
        if not located:
            return False
        _, child = located
        return child.status in (ChildStatus.READY, ChildStatus.FAILED)

    def mark_child_dispatched(self, issue_key: str) -> None:
        """Mark a ready epic child as dispatched."""
        located = self._find_epic_for_child(issue_key)
        if not located:
            return
        epic_key, child = located
        if child.status in (ChildStatus.READY, ChildStatus.FAILED):
            child.status = ChildStatus.DISPATCHED
            self._persist_epic(epic_key)

    def get_state(self) -> dict[str, dict[str, Any]]:
        """Return serializable epic state snapshot."""
        data: dict[str, dict[str, Any]] = {}
        for epic_key, state in self._epics.items():
            data[epic_key] = {
                "epic_key": state.epic_key,
                "epic_summary": state.epic_summary,
                "phase": state.phase,
                "created_at": state.created_at,
                "children": {
                    child_key: {
                        "key": child.key,
                        "summary": child.summary,
                        "status": child.status.value,
                        "depends_on": child.depends_on,
                        "tracker_status": child.tracker_status,
                        "last_comment_id": child.last_comment_id,
                        "tags": child.tags,
                    }
                    for child_key, child in state.children.items()
                },
            }
        return data

    def get_epic_state(self, epic_key: str) -> EpicState | None:
        """Return the EpicState for a given epic, or None."""
        return self._epics.get(epic_key)

    def _find_epic_for_child(self, child_key: str) -> tuple[str, ChildTask] | None:
        for epic_key, state in self._epics.items():
            child = state.children.get(child_key)
            if child:
                return epic_key, child
        return None

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    async def reconcile_dispatched_children(self) -> int:
        """Reconcile children stuck in DISPATCHED after restart.

        For each DISPATCHED child, fetches the current Tracker status.
        If resolved → on_task_completed.  If cancelled → on_task_cancelled.
        If no active session exists (key not in dispatched_set) and status
        is neither resolved nor cancelled → on_task_failed so the child
        can be re-dispatched on the next poll cycle.
        Returns the number of children reconciled.
        """
        reconciled = 0
        for state in list(self._epics.values()):
            for child in list(state.children.values()):
                if child.status != ChildStatus.DISPATCHED:
                    continue

                # Skip children with an active agent session.
                if child.key in self._dispatched:
                    continue

                try:
                    issue = await asyncio.to_thread(
                        self._tracker.get_issue,
                        child.key,
                    )
                except requests.RequestException:
                    logger.warning(
                        "Reconciliation: failed to fetch %s — skipping",
                        child.key,
                    )
                    continue

                if is_cancelled_status(issue.status):
                    logger.info(
                        "Reconciliation: %s is %s → cancelled",
                        child.key,
                        issue.status,
                    )
                    await self.on_task_cancelled(child.key)
                    reconciled += 1
                elif is_resolved_status(issue.status):
                    logger.info(
                        "Reconciliation: %s is %s → completed",
                        child.key,
                        issue.status,
                    )
                    await self.on_task_completed(child.key)
                    reconciled += 1
                else:
                    # No active session and not terminal — orphaned.
                    logger.info(
                        "Reconciliation: %s is %s with no active session → failed (will re-dispatch)",
                        child.key,
                        issue.status,
                    )
                    await self.on_task_failed(child.key)
                    reconciled += 1

        if reconciled:
            logger.info(
                "Reconciliation: fixed %d stuck dispatched children",
                reconciled,
            )
        return reconciled

    # ------------------------------------------------------------------
    # Child readiness and epic completion
    # ------------------------------------------------------------------

    async def _tag_ready_children(self, epic_key: str) -> None:
        state = self._epics.get(epic_key)
        if not state:
            return
        if state.phase != "executing":
            return
        for child in state.children.values():
            if child.status in (
                ChildStatus.COMPLETED,
                ChildStatus.CANCELLED,
                ChildStatus.FAILED,
                ChildStatus.READY,
                ChildStatus.DISPATCHED,
            ):
                continue
            blocked_by = [
                dep
                for dep in child.depends_on
                if dep in state.children
                and state.children[dep].status not in (ChildStatus.COMPLETED, ChildStatus.CANCELLED)
            ]
            if blocked_by:
                await self._event_bus.publish(
                    Event(
                        type=EventType.EPIC_CHILD_BLOCKED,
                        task_key=epic_key,
                        data={"child_key": child.key, "depends_on": blocked_by},
                    )
                )
                continue

            if self._config.tracker_tag not in child.tags:
                updated_tags = [*child.tags, self._config.tracker_tag]
                try:
                    await asyncio.to_thread(self._tracker.update_issue_tags, child.key, updated_tags)
                except requests.RequestException:
                    logger.warning("Failed to update tags for child %s", child.key)
                    continue
                child.tags = updated_tags
            child.status = ChildStatus.READY
            await self._event_bus.publish(
                Event(type=EventType.EPIC_CHILD_READY, task_key=epic_key, data={"child_key": child.key})
            )

    async def _check_epic_completed(self, epic_key: str) -> None:
        state = self._epics.get(epic_key)
        if not state or not state.children or state.phase == "completed":
            return
        if not all(c.status in (ChildStatus.COMPLETED, ChildStatus.CANCELLED) for c in state.children.values()):
            return

        cancelled_count = sum(1 for c in state.children.values() if c.status == ChildStatus.CANCELLED)
        try:
            await asyncio.to_thread(
                self._tracker.transition_to_closed,
                epic_key,
                comment="Все дочерние задачи эпика завершены или отменены.",
            )
        except requests.RequestException:
            logger.warning("Failed to close epic %s", epic_key)
        state.phase = "completed"
        self._dispatched.discard(epic_key)
        self._persist_delete_epic(epic_key)
        await self._event_bus.publish(
            Event(
                type=EventType.EPIC_COMPLETED,
                task_key=epic_key,
                data={"children_total": len(state.children), "cancelled": cancelled_count},
            )
        )

    # ------------------------------------------------------------------
    # Child discovery, link parsing, validation
    # ------------------------------------------------------------------

    def fetch_children(self, epic_key: str) -> list[TrackerIssue]:
        """Fetch child issues linked to epic (blocking — call via asyncio.to_thread)."""
        links = self._tracker.get_links(epic_key)
        child_keys = sorted(self._extract_child_keys(links, epic_key))
        children: list[TrackerIssue] = []
        for child_key in child_keys:
            try:
                children.append(self._tracker.get_issue(child_key))
            except requests.RequestException:
                logger.warning("Failed to fetch child issue %s for epic %s", child_key, epic_key)
        return children

    @staticmethod
    def build_child_task(issue: TrackerIssue) -> ChildTask:
        """Build a ChildTask from a TrackerIssue."""
        if EpicCoordinator.is_cancelled_status(issue.status):
            status = ChildStatus.CANCELLED
        elif EpicCoordinator.is_resolved_status(issue.status):
            status = ChildStatus.COMPLETED
        else:
            status = ChildStatus.PENDING
        return ChildTask(
            key=issue.key,
            summary=issue.summary,
            status=status,
            depends_on=[],
            tracker_status=issue.status,
            tags=list(issue.tags),
        )

    @staticmethod
    def extract_child_keys_strict(links: list[TrackerLinkDict], parent_key: str) -> set[str]:
        """Extract child task keys from links, filtering by CHILD_LINK_HINTS.

        No fallback — only returns tasks with explicit child relationships.
        Safe to use for non-epic tasks (e.g., auto-closing orphaned subtasks).
        """
        child_keys: set[str] = set()
        for link in links:
            relationship = str(link.get("relationship", "")).lower()
            if not any(hint in relationship for hint in CHILD_LINK_HINTS):
                continue
            linked = EpicCoordinator.extract_linked_issue_key(link)
            if linked and linked != parent_key:
                child_keys.add(linked)
        return child_keys

    @staticmethod
    def _extract_child_keys(links: list[TrackerLinkDict], epic_key: str) -> set[str]:
        # Use strict extraction first
        child_keys = EpicCoordinator.extract_child_keys_strict(links, epic_key)
        if child_keys:
            return child_keys
        # Fallback for unknown Tracker relationship names.
        for link in links:
            linked = EpicCoordinator.extract_linked_issue_key(link)
            if linked and linked != epic_key:
                child_keys.add(linked)
        return child_keys

    @staticmethod
    def extract_linked_issue_key(link: Mapping[str, Any]) -> str:
        """Extract the linked issue key from a Tracker link dict."""
        issue = link.get("issue") or {}
        if isinstance(issue, dict):
            key = issue.get("key")
            if isinstance(key, str):
                return key
        object_issue = link.get("object") or {}
        if isinstance(object_issue, dict):
            key = object_issue.get("key")
            if isinstance(key, str):
                return key
        return ""

    @staticmethod
    def validate_acyclic(dependencies: dict[str, list[str]]) -> bool:
        """Validate that a dependency graph has no cycles."""
        visited: set[str] = set()
        stack: set[str] = set()

        def visit(node: str) -> bool:
            if node in stack:
                return False
            if node in visited:
                return True
            stack.add(node)
            for dep in dependencies.get(node, []):
                if dep in dependencies and not visit(dep):
                    return False
            stack.remove(node)
            visited.add(node)
            return True

        return all(visit(node) for node in dependencies)

    @staticmethod
    def is_resolved_status(status: str) -> bool:
        """Check if a Tracker status indicates resolution."""
        return is_resolved_status(status)

    @staticmethod
    def is_cancelled_status(status: str) -> bool:
        """Check if a Tracker status indicates cancellation."""
        return is_cancelled_status(status)
