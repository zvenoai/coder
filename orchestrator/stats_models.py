"""Data models for persistent statistics storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class TaskCostSummary(TypedDict):
    """Aggregated cost across all runs for a single task."""

    total_cost_usd: float
    run_count: int


@dataclass
class TaskRun:
    """A single agent task execution record."""

    task_key: str
    model: str
    cost_usd: float
    duration_seconds: float
    success: bool
    error_category: str | None
    pr_url: str | None
    needs_info: bool
    resumed: bool
    started_at: float  # wall-clock timestamp
    finished_at: float
    session_id: str | None = None


@dataclass
class SupervisorRun:
    """A single supervisor agent execution record."""

    trigger_task_keys: list[str]
    cost_usd: float
    duration_seconds: float
    success: bool
    tasks_created: list[str]
    started_at: float
    finished_at: float


@dataclass
class ErrorLogEntry:
    """An error occurrence during task execution."""

    task_key: str
    error_category: str
    error_message: str
    retryable: bool
    timestamp: float


@dataclass
class ProposalRecord:
    """A recorded improvement proposal."""

    proposal_id: str
    source_task_key: str
    summary: str
    category: str
    status: str  # "pending" | "approved" | "rejected"
    created_at: float
    resolved_at: float | None = None
    description: str = ""
    component: str = ""
    tracker_issue_key: str | None = None


@dataclass
class RecoveryRecord:
    """Per-issue recovery state snapshot for SQLite persistence."""

    issue_key: str
    attempt_count: int
    no_pr_count: int
    last_output: str | None
    updated_at: float
    no_pr_cost: float = 0.0


@dataclass
class PRTrackingData:
    """Persisted PR tracking dedup data."""

    task_key: str
    pr_url: str
    issue_summary: str
    seen_thread_ids: list[str]
    seen_failed_checks: list[str]
    session_id: str | None = None
    seen_merge_conflict: bool = False
    merge_conflict_retries: int = 0
    merge_conflict_head_sha: str = ""


@dataclass
class NeedsInfoTrackingRecord:
    """Persisted needs-info tracking state."""

    issue_key: str
    last_seen_comment_id: int
    issue_summary: str
    tracked_at: float
    session_id: str | None = None


@dataclass
class EpicStateRecord:
    """Persisted epic state snapshot."""

    epic_key: str
    epic_summary: str
    phase: str
    created_at: float


@dataclass
class EpicChildRecord:
    """Persisted epic child task snapshot."""

    child_key: str
    summary: str
    status: str
    depends_on: list[str]
    tracker_status: str
    last_comment_id: int
    tags: list[str]


@dataclass
class DeferredTaskRecord:
    """Persisted deferred task snapshot for SQLite persistence."""

    issue_key: str
    issue_summary: str
    blockers: list[str]
    deferred_at: float
    manual: bool = False
