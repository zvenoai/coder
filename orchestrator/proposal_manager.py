"""Proposal management for agent improvement suggestions."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests

from orchestrator._persistence import BackgroundPersistenceMixin
from orchestrator.constants import EventType
from orchestrator.event_bus import Event

if TYPE_CHECKING:
    from orchestrator.config import Config
    from orchestrator.event_bus import EventBus
    from orchestrator.storage import Storage
    from orchestrator.tracker_client import TrackerClient

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = frozenset({"tooling", "documentation", "process", "testing", "infrastructure"})


@dataclass
class StoredProposal:
    """An improvement proposal from an AI agent, pending human review."""

    id: str
    source_task_key: str
    summary: str
    description: str
    component: str
    category: str
    status: str = "pending"
    tracker_issue_key: str | None = None
    created_at: float = 0.0


class ProposalManager(BackgroundPersistenceMixin):
    """Manages agent improvement proposals with approval workflow."""

    def __init__(
        self,
        tracker: TrackerClient,
        event_bus: EventBus,
        config: Config,
        storage: Storage | None = None,
    ) -> None:
        self._tracker = tracker
        self._event_bus = event_bus
        self._config = config
        self._storage = storage
        self._proposals: dict[str, StoredProposal] = {}
        self._proposal_locks: dict[str, asyncio.Lock] = {}
        self._init_persistence()

    def get_all(self) -> dict[str, StoredProposal]:
        """Get all proposals (for web API)."""
        return self._proposals

    def get_pending_snapshot(self) -> list[dict[str, str]]:
        """Get pending proposals as a list of dicts for supervisor/orchestrator context."""
        return [
            {
                "summary": p.summary,
                "description": p.description,
                "component": p.component,
                "category": p.category,
            }
            for p in self._proposals.values()
            if p.status == "pending"
        ]

    async def load(self) -> None:
        """Load proposals from storage. Call after Storage.open()."""
        if not self._storage:
            return
        try:
            rows = await self._storage.load_proposals()
            for row in rows:
                self._proposals[row["proposal_id"]] = StoredProposal(
                    id=row["proposal_id"],
                    source_task_key=row["source_task_key"],
                    summary=row["summary"],
                    description=row["description"],
                    component=row["component"],
                    category=row["category"],
                    status=row["status"],
                    tracker_issue_key=row["tracker_issue_key"],
                    created_at=row["created_at"],
                )
            if rows:
                logger.info("Loaded %d proposals from storage", len(rows))
        except Exception:
            logger.warning("Failed to load proposals from storage", exc_info=True)

    def _persist_proposal(self, proposal_id: str) -> None:
        """Schedule async persistence of a proposal."""
        if not self._storage:
            return

        from orchestrator.stats_models import ProposalRecord

        proposal = self._proposals.get(proposal_id)
        if not proposal:
            return

        record = ProposalRecord(
            proposal_id=proposal.id,
            source_task_key=proposal.source_task_key,
            summary=proposal.summary,
            category=proposal.category,
            status=proposal.status,
            created_at=proposal.created_at,
            description=proposal.description,
            component=proposal.component,
            tracker_issue_key=proposal.tracker_issue_key,
        )

        async def _write() -> None:
            async with self._key_locks[proposal_id]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.upsert_proposal(record)
                except Exception:
                    logger.warning("Failed to persist proposal %s", proposal_id, exc_info=True)

        self._schedule_task(_write())

    async def process_proposals(self, task_key: str, proposals: list[dict[str, str]]) -> None:
        """Store agent proposals and publish events.

        Args:
            task_key: Source task that generated proposals
            proposals: List of proposal dicts with summary, description, component, category
        """
        for raw in proposals:
            component = raw.get("component", "backend").lower().strip()
            category = raw.get("category", "tooling").lower().strip()

            # Validate and normalize
            if component not in self._config.component_assignee_map:
                component = "backend"
            if category not in _VALID_CATEGORIES:
                category = "tooling"

            proposal_id = uuid.uuid4().hex[:12]
            proposal = StoredProposal(
                id=proposal_id,
                source_task_key=task_key,
                summary=raw.get("summary", ""),
                description=raw.get("description", ""),
                component=component,
                category=category,
                created_at=time.time(),
            )
            self._proposals[proposal_id] = proposal
            self._persist_proposal(proposal_id)

            logger.info("Proposal %s from %s: %s", proposal_id, task_key, proposal.summary)

            await self._event_bus.publish(
                Event(
                    type=EventType.TASK_PROPOSED,
                    task_key=task_key,
                    data={
                        "proposal_id": proposal_id,
                        "summary": proposal.summary,
                        "component": component,
                        "category": category,
                    },
                )
            )

    async def approve(self, proposal_id: str) -> StoredProposal:
        """Approve a proposal — create a Tracker issue and link it to the source task.

        Args:
            proposal_id: Proposal ID to approve

        Returns:
            Updated StoredProposal with tracker_issue_key

        Raises:
            ValueError: If proposal not found or already processed
        """
        lock = self._proposal_locks.setdefault(proposal_id, asyncio.Lock())
        async with lock:
            proposal = self._proposals.get(proposal_id)
            if not proposal or proposal.status != "pending":
                raise ValueError(f"Proposal {proposal_id} not found or already processed")

            assignee_map = self._config.component_assignee_map
            default_entry = next(iter(assignee_map.values()), None)
            comp_name, assignee = assignee_map.get(
                proposal.component,
                default_entry or ("", ""),
            )

            description = (
                f"{proposal.description}\n\n---\n"
                f"_Предложено AI-агентом при работе над {proposal.source_task_key}. "
                f"Задача на доработку среды оркестратора (coder)._"
            )

            result = await asyncio.to_thread(
                lambda: self._tracker.create_issue(
                    queue=self._config.tracker_queue,
                    summary=f"[{proposal.category}] {proposal.summary}",
                    description=description,
                    components=[comp_name],
                    assignee=assignee,
                    project_id=self._config.tracker_project_id,
                    boards=self._config.tracker_boards,
                )
            )
            new_key = result["key"]

            # Link new issue to source task
            try:
                await asyncio.to_thread(self._tracker.add_link, new_key, proposal.source_task_key, "relates")
            except requests.RequestException:
                logger.warning("Failed to link %s to %s", new_key, proposal.source_task_key)

            proposal.status = "approved"
            proposal.tracker_issue_key = new_key
            self._persist_proposal(proposal_id)

            await self._event_bus.publish(
                Event(
                    type=EventType.PROPOSAL_APPROVED,
                    task_key=proposal.source_task_key,
                    data={"proposal_id": proposal_id, "issue_key": new_key},
                )
            )

            logger.info("Proposal %s approved → %s", proposal_id, new_key)
            return proposal

    async def reject(self, proposal_id: str) -> StoredProposal:
        """Reject a proposal.

        Args:
            proposal_id: Proposal ID to reject

        Returns:
            Updated StoredProposal

        Raises:
            ValueError: If proposal not found or already processed
        """
        proposal = self._proposals.get(proposal_id)
        if not proposal or proposal.status != "pending":
            raise ValueError(f"Proposal {proposal_id} not found or already processed")

        proposal.status = "rejected"
        self._persist_proposal(proposal_id)

        await self._event_bus.publish(
            Event(
                type=EventType.PROPOSAL_REJECTED,
                task_key=proposal.source_task_key,
                data={"proposal_id": proposal_id},
            )
        )

        logger.info("Proposal %s rejected", proposal_id)
        return proposal
