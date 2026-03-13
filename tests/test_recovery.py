"""Tests for recovery module."""

import asyncio
import time

import pytest

from orchestrator.recovery import (
    BACKOFF_MULTIPLIER,
    BASE_BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    MAX_NO_PR_ATTEMPTS,
    NON_RETRYABLE,
    ErrorCategory,
    RecoveryAttempt,
    RecoveryManager,
    RecoveryState,
    classify_error,
    is_provider_rate_limit,
)


class TestClassifyError:
    @pytest.mark.parametrize(
        ("error_input", "expected"),
        [
            ("rate limit exceeded", ErrorCategory.RATE_LIMIT),
            ("HTTP 429 Too Many Requests", ErrorCategory.RATE_LIMIT),
            ("401 Unauthorized", ErrorCategory.AUTH),
            ("403 Forbidden", ErrorCategory.AUTH),
            ("Request timed out", ErrorCategory.TIMEOUT),
            ("Agent timeout after 1800s", ErrorCategory.TIMEOUT),
            ("budget exceeded max_budget", ErrorCategory.BUDGET),
            ("cost limit reached", ErrorCategory.BUDGET),
            ("CLI not found", ErrorCategory.CLI),
            ("command not found: claude", ErrorCategory.CLI),
            ("connection refused", ErrorCategory.TRANSIENT),
            ("HTTP 502 Bad Gateway", ErrorCategory.TRANSIENT),
            ("network error", ErrorCategory.TRANSIENT),
            ("some unknown error", ErrorCategory.PERMANENT),
            # Exception input — should extract message via str()
            (RuntimeError("rate limit"), ErrorCategory.RATE_LIMIT),
        ],
        ids=lambda v: str(v) if isinstance(v, str) else type(v).__name__,
    )
    def test_classify(self, error_input, expected) -> None:
        assert classify_error(error_input) == expected


class TestRecordFailureCategoryOverride:
    """record_failure with explicit category overrides classify_error."""

    @pytest.mark.parametrize(
        ("error_msg", "override", "expect_retry"),
        [
            ("Cancelled: user request", ErrorCategory.CANCELLED, False),
            # "rate limit" would classify as RATE_LIMIT, but override to PERMANENT
            ("rate limit exceeded", ErrorCategory.PERMANENT, True),
        ],
    )
    def test_category_override(self, error_msg, override, expect_retry) -> None:
        manager = RecoveryManager()
        state = manager.record_failure("QR-1", error_msg, category=override)
        assert state.last_category == override
        # PERMANENT is retryable (until MAX_ATTEMPTS), CANCELLED is not
        assert state.should_retry is expect_retry


class TestRecoveryState:
    def test_initial_state(self) -> None:
        state = RecoveryState(issue_key="QR-1")
        assert state.attempt_count == 0
        assert state.last_category is None
        assert state.should_retry is True
        assert state.backoff_seconds == 0.0

    def test_backoff_increases(self) -> None:
        state = RecoveryState(issue_key="QR-1")
        state.attempts.append(RecoveryAttempt(time.time(), ErrorCategory.TRANSIENT, "err"))
        assert state.backoff_seconds == BASE_BACKOFF_SECONDS  # 30s

        state.attempts.append(RecoveryAttempt(time.time(), ErrorCategory.TRANSIENT, "err"))
        expected = BASE_BACKOFF_SECONDS * BACKOFF_MULTIPLIER  # 120s
        assert state.backoff_seconds == expected

    @pytest.mark.parametrize(
        "category",
        [
            ErrorCategory.AUTH,
            ErrorCategory.BUDGET,
            ErrorCategory.CLI,
            ErrorCategory.CANCELLED,
        ],
    )
    def test_non_retryable_categories(self, category) -> None:
        assert category in NON_RETRYABLE
        state = RecoveryState(issue_key="QR-1")
        state.attempts.append(RecoveryAttempt(time.time(), category, "error"))
        assert state.should_retry is False

    def test_max_attempts_reached(self) -> None:
        state = RecoveryState(issue_key="QR-1")
        for _ in range(MAX_ATTEMPTS):
            state.attempts.append(RecoveryAttempt(time.time(), ErrorCategory.TRANSIENT, "err"))
        assert state.should_retry is False


class TestRecoveryManager:
    def test_get_state_creates_new(self) -> None:
        manager = RecoveryManager()
        state = manager.get_state("QR-1")
        assert state.issue_key == "QR-1"
        assert state.attempt_count == 0

    def test_record_failure(self) -> None:
        manager = RecoveryManager()
        state = manager.record_failure("QR-1", "rate limit exceeded")
        assert state.attempt_count == 1
        assert state.last_category == ErrorCategory.RATE_LIMIT

    def test_clear(self) -> None:
        manager = RecoveryManager()
        manager.record_failure("QR-1", "error")
        manager.clear("QR-1")
        state = manager.get_state("QR-1")
        assert state.attempt_count == 0

    @pytest.mark.asyncio
    async def test_wait_for_retry(self) -> None:
        from unittest.mock import patch

        manager = RecoveryManager()
        manager.record_failure("QR-1", "connection error")

        with patch("orchestrator.recovery.asyncio.sleep") as mock_sleep:
            mock_sleep.return_value = None
            await manager.wait_for_retry("QR-1")
            mock_sleep.assert_called_once_with(BASE_BACKOFF_SECONDS)


@pytest.fixture
async def storage(tmp_path):
    """Create a SQLiteStorage with a temporary database."""
    from orchestrator.sqlite_storage import SQLiteStorage

    db = SQLiteStorage(str(tmp_path / "test_recovery.db"))
    await db.open()
    yield db
    await db.close()


class TestSQLitePersistence:
    async def test_record_failure_persists_to_sqlite(self, storage) -> None:
        """Failure state persisted to SQLite and recoverable via load()."""
        manager = RecoveryManager(storage=storage)
        manager.record_failure("QR-1", "connection error")
        manager.record_failure("QR-1", "timeout")

        await asyncio.sleep(0.05)

        manager2 = RecoveryManager(storage=storage)
        await manager2.load()
        state = manager2.get_state("QR-1")
        assert state.attempt_count == 2
        assert state.last_category == ErrorCategory.TIMEOUT

    async def test_clear_deletes_from_sqlite(self, storage) -> None:
        """clear() removes state from SQLite."""
        manager = RecoveryManager(storage=storage)
        manager.record_failure("QR-1", "error")
        await asyncio.sleep(0.05)

        manager.clear("QR-1")
        await asyncio.sleep(0.05)

        manager2 = RecoveryManager(storage=storage)
        await manager2.load()
        assert manager2.get_state("QR-1").attempt_count == 0

    async def test_works_without_storage(self) -> None:
        """RecoveryManager works in memory-only mode."""
        manager = RecoveryManager()
        manager.record_failure("QR-1", "error")
        assert manager.get_state("QR-1").attempt_count == 1

    async def test_incremental_updates(self, storage) -> None:
        """Multiple operations persist correctly."""
        manager = RecoveryManager(storage=storage)
        manager.record_failure("QR-1", "first error")
        manager.record_failure("QR-2", "second error")
        await asyncio.sleep(0.05)

        manager2 = RecoveryManager(storage=storage)
        await manager2.load()
        assert manager2.get_state("QR-1").attempt_count == 1
        assert manager2.get_state("QR-2").attempt_count == 1


class TestPersistOrdering:
    """DB writes for the same issue_key must execute in order."""

    async def test_record_then_clear_no_zombie(self, storage) -> None:
        """record_failure -> clear -> no recovery state in DB."""
        manager = RecoveryManager(storage=storage)
        manager.record_failure("QR-1", "error")
        manager.clear("QR-1")
        await manager.drain_background_tasks()

        records = await storage.load_recovery_states()
        assert not any(r.issue_key == "QR-1" for r in records)

    async def test_clear_then_record_keeps_state(self, storage) -> None:
        """clear -> record_failure -> state persists in DB."""
        manager = RecoveryManager(storage=storage)
        manager.record_failure("QR-1", "first error")
        manager.clear("QR-1")
        manager.record_failure("QR-1", "second error")
        await manager.drain_background_tasks()

        records = await storage.load_recovery_states()
        assert any(r.issue_key == "QR-1" for r in records)

    async def test_disable_storage_stops_persistence(self, storage) -> None:
        """disable_storage() prevents any further DB writes."""
        manager = RecoveryManager(storage=storage)
        manager.disable_storage()
        manager.record_failure("QR-1", "error")
        await asyncio.sleep(0.05)
        records = await storage.load_recovery_states()
        assert len(records) == 0


class TestBackgroundTaskRefs:
    """Fire-and-forget tasks must be stored to avoid GC."""

    async def test_persist_tasks_stored_in_set(self, storage) -> None:
        """RecoveryManager keeps references to background tasks."""
        manager = RecoveryManager(storage=storage)
        assert hasattr(manager, "_background_tasks")
        manager.record_failure("QR-1", "test error")
        assert len(manager._background_tasks) > 0
        await asyncio.sleep(0.05)
        assert len(manager._background_tasks) == 0


class TestProviderRateLimit:
    """Tests for is_provider_rate_limit() detection."""

    def test_detects_anthropic_limit_message(self) -> None:
        assert is_provider_rate_limit("You've hit your limit · resets 2pm (UTC)")
        assert is_provider_rate_limit("Error: You've hit your limit")

    def test_detects_generic_rate_limit(self) -> None:
        assert is_provider_rate_limit("rate limit exceeded")
        assert is_provider_rate_limit("Rate Limit")
        assert is_provider_rate_limit("RATE_LIMIT error")

    def test_detects_429(self) -> None:
        assert is_provider_rate_limit("HTTP 429 Too Many Requests")
        assert is_provider_rate_limit("Error 429: rate limit exceeded")
        assert is_provider_rate_limit("Status code 429")

    def test_detects_too_many_requests(self) -> None:
        assert is_provider_rate_limit("too many requests")
        assert is_provider_rate_limit("Too Many Requests")

    def test_detects_resets_keyword(self) -> None:
        assert is_provider_rate_limit("limit exceeded · resets tomorrow")

    def test_false_for_normal_errors(self) -> None:
        assert not is_provider_rate_limit("connection timeout")
        assert not is_provider_rate_limit("unknown error")
        assert not is_provider_rate_limit("file not found")

    def test_false_for_pr_numbers_with_429(self) -> None:
        """429 in PR numbers should not trigger rate limit detection."""
        assert not is_provider_rate_limit("Fixed bug in PR #1429")
        assert not is_provider_rate_limit("See issue QR-4291 for details")
        assert not is_provider_rate_limit("Merged PR #4290 and #1429")

    def test_false_for_line_numbers_with_429(self) -> None:
        """429 in line numbers should not trigger rate limit detection."""
        assert not is_provider_rate_limit("Error on line 429 of file.py")
        assert not is_provider_rate_limit("Traceback: file.py:429")
        assert not is_provider_rate_limit("Check lines 428-4291 for the bug")

    def test_false_for_technical_resets(self) -> None:
        """'resets' in technical context should not trigger rate limit detection."""
        assert not is_provider_rate_limit("The function resets the counter to zero")
        assert not is_provider_rate_limit("Password resets are handled by auth service")
        assert not is_provider_rate_limit("System resets after 5 failed attempts")
        assert not is_provider_rate_limit("This resets all state variables")


class TestRecordNoPR:
    """Tests for record_no_pr() functionality."""

    def test_record_no_pr_increments_count(self) -> None:
        manager = RecoveryManager()
        state = manager.record_no_pr("QR-1", "agent output", cost_usd=1.5)
        assert state.no_pr_count == 1
        assert state.last_output == "agent output"
        assert state.no_pr_cost == 1.5

    def test_record_no_pr_multiple_times(self) -> None:
        manager = RecoveryManager()
        manager.record_no_pr("QR-1", "first output", cost_usd=1.0)
        state = manager.record_no_pr("QR-1", "second output", cost_usd=2.0)
        assert state.no_pr_count == 2
        assert state.last_output == "second output"
        assert state.no_pr_cost == 3.0

    def test_record_no_pr_truncates_output(self) -> None:
        manager = RecoveryManager()
        long_output = "x" * 5000
        state = manager.record_no_pr("QR-1", long_output)
        assert len(state.last_output) == 2000
        assert state.last_output == "x" * 2000

    def test_should_retry_no_pr_when_under_limit(self) -> None:
        manager = RecoveryManager()
        manager.record_no_pr("QR-1", "output")
        state = manager.get_state("QR-1")
        assert state.should_retry_no_pr is True

        manager.record_no_pr("QR-1", "output2")
        state = manager.get_state("QR-1")
        assert state.should_retry_no_pr is True

    def test_should_retry_no_pr_false_after_max(self) -> None:
        manager = RecoveryManager()
        for i in range(MAX_NO_PR_ATTEMPTS):
            manager.record_no_pr("QR-1", f"output {i}")

        state = manager.get_state("QR-1")
        assert state.no_pr_count == MAX_NO_PR_ATTEMPTS
        assert state.should_retry_no_pr is False

    def test_should_retry_no_pr_false_on_rate_limit(self) -> None:
        manager = RecoveryManager()
        state = manager.record_no_pr("QR-1", "You've hit your limit · resets 2pm (UTC)")
        assert state.no_pr_count == 1
        assert state.should_retry_no_pr is False

    def test_clear_resets_no_pr_state(self) -> None:
        manager = RecoveryManager()
        manager.record_no_pr("QR-1", "output", cost_usd=5.0)
        manager.clear("QR-1")
        state = manager.get_state("QR-1")
        assert state.no_pr_count == 0
        assert state.last_output is None
        assert state.no_pr_cost == 0.0

    @pytest.mark.asyncio
    async def test_record_no_pr_persists_to_sqlite(self, storage) -> None:
        """record_no_pr() should persist to SQLite and be recoverable."""
        manager = RecoveryManager(storage=storage)
        manager.record_no_pr("QR-1", "test output", cost_usd=2.5)
        manager.record_no_pr("QR-1", "second output", cost_usd=1.0)

        await asyncio.sleep(0.05)

        manager2 = RecoveryManager(storage=storage)
        await manager2.load()
        state = manager2.get_state("QR-1")
        assert state.no_pr_count == 2
        assert state.last_output == "second output"
        assert state.no_pr_cost == 3.5

    def test_should_retry_integrates_no_pr_logic(self) -> None:
        """should_retry should return False when no-PR limit is reached."""
        manager = RecoveryManager()
        # No failures, only no-PR attempts
        for i in range(MAX_NO_PR_ATTEMPTS):
            manager.record_no_pr("QR-1", f"output {i}")

        state = manager.get_state("QR-1")
        assert state.should_retry is False
