"""Error classification, recovery state persistence, and exponential backoff."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from orchestrator._persistence import BackgroundPersistenceMixin

if TYPE_CHECKING:
    from orchestrator.storage import Storage

logger = logging.getLogger(__name__)

# Backoff config
BASE_BACKOFF_SECONDS = 30.0
BACKOFF_MULTIPLIER = 4.0
MAX_ATTEMPTS = 3
MAX_NO_PR_ATTEMPTS = 3


class ErrorCategory(Enum):
    """Classification of agent errors."""

    RATE_LIMIT = "rate_limit"
    AUTH = "auth"  # nosemgrep: hardcoded-password
    CONCURRENCY = "concurrency"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    CLI = "cli"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    CANCELLED = "cancelled"


# Categories that should not be retried
NON_RETRYABLE = {ErrorCategory.AUTH, ErrorCategory.BUDGET, ErrorCategory.CLI, ErrorCategory.CANCELLED}


def classify_error(error: Exception | str) -> ErrorCategory:
    """Classify an error into a category based on message patterns."""
    msg = str(error).lower()

    if "rate limit" in msg or "429" in msg or "too many requests" in msg:
        return ErrorCategory.RATE_LIMIT
    if "auth" in msg or "unauthorized" in msg or "401" in msg or "403" in msg:
        return ErrorCategory.AUTH
    if "concurrent" in msg or "already running" in msg:
        return ErrorCategory.CONCURRENCY
    if "timeout" in msg or "timed out" in msg:
        return ErrorCategory.TIMEOUT
    if "budget" in msg or "max_budget" in msg or "cost limit" in msg:
        return ErrorCategory.BUDGET
    if "cli" in msg or "not found" in msg or "command not found" in msg:
        return ErrorCategory.CLI
    if "connection" in msg or "network" in msg or "500" in msg or "502" in msg or "503" in msg:
        return ErrorCategory.TRANSIENT

    return ErrorCategory.PERMANENT


def is_provider_rate_limit(output: str) -> bool:
    """Detect provider rate-limit patterns in agent output.

    Uses specific patterns to avoid false positives from:
    - Numbers like 429 in PR/issue IDs or line numbers
    - Technical language using "resets" (e.g., "function resets counter")
    """
    msg = str(output).lower()

    # High-confidence patterns that don't need context
    specific_patterns = [
        "hit your limit",
        "rate limit",
        "rate_limit",
        "too many requests",
    ]
    if any(pattern in msg for pattern in specific_patterns):
        return True

    # HTTP 429 status code - require context to avoid false positives
    if "429" in msg:
        # Must be in HTTP/status context, not just any 429 digits
        http_429_contexts = [
            "http 429",
            "status 429",
            "429 too many",
            "error 429",
            "code 429",
        ]
        if any(context in msg for context in http_429_contexts):
            return True

    # "resets" keyword - require rate limit context
    if "resets" in msg:
        # Must be in rate limit context, not technical resets
        reset_contexts = [
            "limit",  # e.g., "limit resets"
            "resets at",  # e.g., "resets at 2pm"
            "resets in",  # e.g., "resets in 1 hour"
            "· resets",  # Anthropic format: "hit your limit · resets 2pm"
        ]
        if any(context in msg for context in reset_contexts):
            return True

    return False


@dataclass
class RecoveryAttempt:
    """Record of a single recovery attempt."""

    timestamp: float
    category: ErrorCategory
    error_message: str


@dataclass
class RecoveryState:
    """Per-issue recovery state."""

    issue_key: str
    attempts: list[RecoveryAttempt] = field(default_factory=list)
    no_pr_count: int = 0
    last_output: str | None = None
    no_pr_cost: float = 0.0

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def last_category(self) -> ErrorCategory | None:
        if self.attempts:
            return self.attempts[-1].category
        return None

    @property
    def should_retry_no_pr(self) -> bool:
        """Whether this issue should be retried after no-PR completion."""
        if self.no_pr_count == 0:
            return True
        if self.last_output and is_provider_rate_limit(self.last_output):
            return False
        return self.no_pr_count < MAX_NO_PR_ATTEMPTS

    @property
    def should_retry(self) -> bool:
        """Whether this issue should be retried."""
        if not self.attempts and self.no_pr_count == 0:
            return True
        if self.last_category in NON_RETRYABLE:
            return False
        if self.attempt_count >= MAX_ATTEMPTS:
            return False
        # No-PR retry logic
        if self.no_pr_count > 0:
            if self.last_output and is_provider_rate_limit(self.last_output):
                return False
            if self.no_pr_count >= MAX_NO_PR_ATTEMPTS:
                return False
        return True

    @property
    def backoff_seconds(self) -> float:
        """Exponential backoff: 30s, 120s, 480s."""
        if self.attempt_count == 0:
            return 0.0
        return BASE_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (self.attempt_count - 1))


class RecoveryManager(BackgroundPersistenceMixin):
    """Manages recovery state across all tasks.

    Uses in-memory cache for hot-path reads and persists to SQLite via Storage.
    Sync public API is preserved; DB writes are scheduled via _persist().
    """

    def __init__(self, storage: Storage | None = None) -> None:
        self._storage = storage
        self._states: dict[str, RecoveryState] = {}
        self._init_persistence()

    async def load(self) -> None:
        """Load recovery state from SQLite. Call after Storage.open()."""
        if not self._storage:
            return

        try:
            records = await self._storage.load_recovery_states()
            for rec in records:
                # Restore attempts
                state = RecoveryState(issue_key=rec.issue_key)
                attempts = await self._storage.load_recovery_attempts(rec.issue_key)
                for ts, cat, msg in attempts:
                    state.attempts.append(RecoveryAttempt(timestamp=ts, category=ErrorCategory(cat), error_message=msg))
                # Restore no-PR state
                state.no_pr_count = rec.no_pr_count
                state.last_output = rec.last_output
                state.no_pr_cost = rec.no_pr_cost
                self._states[rec.issue_key] = state

            if records:
                logger.info("Loaded recovery state for %d issues from SQLite", len(records))
        except Exception:
            logger.warning("Failed to load recovery state from SQLite", exc_info=True)

    def get_state(self, issue_key: str) -> RecoveryState:
        """Get or create recovery state for an issue."""
        if issue_key not in self._states:
            self._states[issue_key] = RecoveryState(issue_key=issue_key)
        return self._states[issue_key]

    def record_failure(
        self, issue_key: str, error: Exception | str, *, category: ErrorCategory | None = None
    ) -> RecoveryState:
        """Record a failed attempt for an issue.

        Args:
            category: Explicit error category. When provided, skips classify_error().
        """
        category = category if category is not None else classify_error(error)
        state = self.get_state(issue_key)
        ts = time.time()
        state.attempts.append(
            RecoveryAttempt(
                timestamp=ts,
                category=category,
                error_message=str(error),
            )
        )
        logger.warning(
            "Recorded failure for %s: %s (attempt %d/%d, retry=%s)",
            issue_key,
            category.value,
            state.attempt_count,
            MAX_ATTEMPTS,
            state.should_retry,
        )
        self._persist(issue_key, attempt=(ts, category.value, str(error)))
        return state

    def record_no_pr(self, issue_key: str, output: str, cost_usd: float = 0.0) -> RecoveryState:
        """Record a successful completion without PR.

        Args:
            issue_key: Task identifier
            output: Agent output (truncated to 2000 chars)
            cost_usd: Cost of this attempt
        """
        state = self.get_state(issue_key)
        state.no_pr_count += 1
        state.last_output = output[-2000:] if len(output) > 2000 else output
        state.no_pr_cost += cost_usd
        logger.warning(
            "No PR for %s (attempt %d/%d, retry=%s, cost=$%.2f)",
            issue_key,
            state.no_pr_count,
            MAX_NO_PR_ATTEMPTS,
            state.should_retry_no_pr,
            state.no_pr_cost,
        )
        self._persist(issue_key)
        return state

    def clear(self, issue_key: str) -> None:
        """Clear all recovery state after success."""
        self._states.pop(issue_key, None)
        self._persist_delete(issue_key)

    async def wait_for_retry(self, issue_key: str) -> None:
        """Wait the appropriate backoff time before retrying."""
        state = self.get_state(issue_key)
        wait = state.backoff_seconds
        if wait > 0:
            logger.info("Waiting %.0fs before retrying %s", wait, issue_key)
            await asyncio.sleep(wait)

    def _persist(self, issue_key: str, attempt: tuple[float, str, str] | None = None) -> None:
        """Schedule async persistence of recovery state to SQLite."""
        if not self._storage:
            return

        from orchestrator.stats_models import RecoveryRecord

        state = self._states.get(issue_key)
        record = RecoveryRecord(
            issue_key=issue_key,
            attempt_count=state.attempt_count if state else 0,
            no_pr_count=state.no_pr_count if state else 0,
            last_output=state.last_output if state else None,
            updated_at=time.time(),
            no_pr_cost=state.no_pr_cost if state else 0.0,
        )

        async def _write() -> None:
            async with self._key_locks[issue_key]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.upsert_recovery_state(record)
                    if attempt:
                        await self._storage.record_recovery_attempt(issue_key, *attempt)
                except Exception:
                    logger.warning("Failed to persist recovery state for %s", issue_key, exc_info=True)

        self._schedule_task(_write())

    def _persist_delete(self, issue_key: str) -> None:
        """Schedule async deletion of recovery state from SQLite."""
        if not self._storage:
            return

        async def _delete() -> None:
            async with self._key_locks[issue_key]:
                try:
                    if self._storage is None:
                        raise RuntimeError("storage is not set")
                    await self._storage.delete_recovery(issue_key)
                except Exception:
                    logger.warning("Failed to delete recovery state for %s", issue_key, exc_info=True)

        self._schedule_task(_delete())
