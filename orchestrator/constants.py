"""Constants and enums for the orchestrator."""

from enum import StrEnum
from typing import Literal, TypedDict


class EventType(StrEnum):
    """Event types published to the event bus.

    Event data contracts:
    - PR_MERGED: data must include "pr_url" (str) — the PR URL that was merged.
    """

    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_SKIPPED = "task_skipped"
    PR_TRACKED = "pr_tracked"
    PR_MERGED = "pr_merged"
    NEEDS_INFO = "needs_info"
    REVIEW_SENT = "review_sent"
    PIPELINE_FAILED = "pipeline_failed"
    TASK_PROPOSED = "task_proposed"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    AGENT_OUTPUT = "agent_output"
    AGENT_RESULT = "agent_result"
    USER_MESSAGE = "user_message"
    NEEDS_INFO_RESPONSE = "needs_info_response"
    SUPERVISOR_STARTED = "supervisor_started"
    SUPERVISOR_COMPLETED = "supervisor_completed"
    SUPERVISOR_FAILED = "supervisor_failed"
    SUPERVISOR_TASK_CREATED = "supervisor_task_created"
    MODEL_SELECTED = "model_selected"
    EPIC_DETECTED = "epic_detected"
    EPIC_CHILD_READY = "epic_child_ready"
    EPIC_CHILD_BLOCKED = "epic_child_blocked"
    EPIC_AWAITING_PLAN = "epic_awaiting_plan"
    EPIC_COMPLETED = "epic_completed"
    SUPERVISOR_CHAT_USER = "supervisor_chat_user"
    SUPERVISOR_CHAT_CHUNK = "supervisor_chat_chunk"
    SUPERVISOR_CHAT_DONE = "supervisor_chat_done"
    SUPERVISOR_CHAT_ERROR = "supervisor_chat_error"
    SUPERVISOR_CHAT_THINKING = "supervisor_chat_thinking"
    SUPERVISOR_CHAT_TOOL_USE = "supervisor_chat_tool_use"
    ORCHESTRATOR_DECISION = "orchestrator_decision"
    COMPACTION_TRIGGERED = "compaction_triggered"
    AGENT_MESSAGE_SENT = "agent_message_sent"
    AGENT_MESSAGE_REPLIED = "agent_message_replied"
    EPIC_CHILD_RESET = "epic_child_reset"
    MERGE_CONFLICT = "merge_conflict"
    TASK_DEFERRED = "task_deferred"
    TASK_UNBLOCKED = "task_unblocked"
    HEARTBEAT = "heartbeat"
    PR_AUTO_MERGE_ENABLED = "pr_auto_merge_enabled"
    PR_AUTO_MERGE_FAILED = "pr_auto_merge_failed"
    PR_DIRECT_MERGED = "pr_direct_merged"
    EPIC_NEEDS_DECOMPOSITION = "epic_needs_decomposition"
    PR_REVIEW_STARTED = "pr_review_started"
    PR_REVIEW_COMPLETED = "pr_review_completed"
    SESSION_RECREATED = "session_recreated"
    HUMAN_GATE_TRIGGERED = "human_gate_triggered"
    TASK_VERIFIED = "task_verified"
    VERIFICATION_FAILED = "verification_failed"
    CONTINUATION_TRIGGERED = "continuation_triggered"


# Supervisor chat channel identifiers
ChannelId = Literal["chat", "tasks", "heartbeat"]

CHAT_CHANNEL_KEY: str = "supervisor-chat"
TASKS_CHANNEL_KEY: str = "supervisor-tasks"
HEARTBEAT_CHANNEL_KEY: str = "supervisor-heartbeat"


class _ChannelEntry(TypedDict):
    """Metadata entry for a single supervisor channel."""

    task_key: str
    display: str


CHANNEL_META: dict[ChannelId, _ChannelEntry] = {
    "chat": {"task_key": CHAT_CHANNEL_KEY, "display": "Чат"},
    "tasks": {"task_key": TASKS_CHANNEL_KEY, "display": "Задачи"},
    "heartbeat": {"task_key": HEARTBEAT_CHANNEL_KEY, "display": "Мониторинг"},
}

# Maximum number of compaction cycles per task dispatch to prevent infinite loops
MAX_COMPACTION_CYCLES = 3

# Maximum continuation turns when agent completes without PR
MAX_CONTINUATION_TURNS = 3

# Link relationship hints for detecting child tasks in epic/subtask hierarchies
CHILD_LINK_HINTS = ("subtask", "parent", "epic")

# Link relationship hints for detecting dependency (blocker) links
DEPENDENCY_LINK_HINTS = ("depends on", "is blocked by", "blocked by")

# Terminal status keys — issues in these statuses are considered done
# and their worktrees can be safely cleaned up on startup.
# Must stay aligned with _RESOLVED_STATUS_HINTS / _CANCELLED_STATUS_HINTS.
TERMINAL_STATUS_KEYS: frozenset[str] = frozenset({"resolved", "closed", "cancelled", "done", "fixed"})

# Status keywords for resolved/cancelled issue detection
_RESOLVED_STATUS_HINTS = ("done", "closed", "fixed", "resolved")
_CANCELLED_STATUS_HINTS = ("cancel", "отмен")
_REVIEW_STATUS_HINTS = ("review", "ревью", "на проверк", "testing")


def is_resolved_status(status: str) -> bool:
    """Check if a Tracker status indicates resolution."""
    lowered = status.lower()
    return any(hint in lowered for hint in _RESOLVED_STATUS_HINTS)


def is_cancelled_status(status: str) -> bool:
    """Check if a Tracker status indicates cancellation."""
    lowered = status.lower()
    return any(hint in lowered for hint in _CANCELLED_STATUS_HINTS)


def is_review_status(status: str) -> bool:
    """Check if a Tracker status indicates review."""
    lowered = status.lower()
    return any(hint in lowered for hint in _REVIEW_STATUS_HINTS)


# Terminal event types — events that close a task run on the dashboard.
# A task with task_started but no subsequent terminal event is "orphaned".
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
        EventType.PR_TRACKED,
        EventType.PR_MERGED,
        EventType.TASK_SKIPPED,
        EventType.TASK_DEFERRED,
    }
)


class ResolutionType(StrEnum):
    """Tracker issue resolution types."""

    FIXED = "fixed"
    WONT_FIX = "wontFix"
    DUPLICATE = "duplicate"
    CANNOT_REPRODUCE = "cannotReproduce"
    WORKS_AS_DESIGNED = "worksAsDesigned"


class PRState(StrEnum):
    """GitHub Pull Request states."""

    OPEN = "OPEN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"
