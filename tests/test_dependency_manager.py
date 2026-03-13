"""Tests for DependencyManager — task dependency detection and deferral."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import requests

from orchestrator.constants import EventType
from orchestrator.stats_models import DeferredTaskRecord

# ---------------------------------------------------------------------------
# extract_blocker_keys
# ---------------------------------------------------------------------------


class TestExtractBlockerKeys:
    def test_extracts_depends_on_link(self) -> None:
        from orchestrator.dependency_manager import extract_blocker_keys

        links = [{"relationship": "depends on", "issue": {"key": "QR-203"}}]
        result = extract_blocker_keys(links, "QR-204")
        assert result == {"QR-203"}

    def test_extracts_is_blocked_by_link(self) -> None:
        from orchestrator.dependency_manager import extract_blocker_keys

        links = [{"relationship": "is blocked by", "issue": {"key": "QR-100"}}]
        result = extract_blocker_keys(links, "QR-101")
        assert result == {"QR-100"}

    def test_ignores_subtask_links(self) -> None:
        from orchestrator.dependency_manager import extract_blocker_keys

        links = [{"relationship": "is subtask of", "issue": {"key": "QR-50"}}]
        result = extract_blocker_keys(links, "QR-51")
        assert result == set()

    def test_ignores_relates_links(self) -> None:
        from orchestrator.dependency_manager import extract_blocker_keys

        links = [{"relationship": "relates to", "issue": {"key": "QR-200"}}]
        result = extract_blocker_keys(links, "QR-201")
        assert result == set()

    def test_filters_self_reference(self) -> None:
        from orchestrator.dependency_manager import extract_blocker_keys

        links = [{"relationship": "depends on", "issue": {"key": "QR-204"}}]
        result = extract_blocker_keys(links, "QR-204")
        assert result == set()

    def test_empty_links(self) -> None:
        from orchestrator.dependency_manager import extract_blocker_keys

        result = extract_blocker_keys([], "QR-1")
        assert result == set()

    def test_handles_missing_fields(self) -> None:
        from orchestrator.dependency_manager import extract_blocker_keys

        links = [{"relationship": "depends on"}]  # no "issue" key
        result = extract_blocker_keys(links, "QR-1")
        assert result == set()

    def test_multiple_blockers(self) -> None:
        from orchestrator.dependency_manager import extract_blocker_keys

        links = [
            {"relationship": "depends on", "issue": {"key": "QR-201"}},
            {"relationship": "is blocked by", "issue": {"key": "QR-202"}},
            {"relationship": "relates to", "issue": {"key": "QR-300"}},
        ]
        result = extract_blocker_keys(links, "QR-204")
        assert result == {"QR-201", "QR-202"}


# ---------------------------------------------------------------------------
# extract_blocker_keys_from_text
# ---------------------------------------------------------------------------


class TestExtractBlockerKeysFromText:
    async def test_returns_empty_for_none_description(self) -> None:
        """None description returns empty set without LLM call."""
        from unittest.mock import MagicMock

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}
        result = await extract_blocker_keys_from_text(None, "QR-100", config, cache)
        assert result == set()

    async def test_returns_empty_for_empty_description(self) -> None:
        """Empty string description returns empty set without LLM call."""
        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}
        result = await extract_blocker_keys_from_text("", "QR-100", config, cache)
        assert result == set()

    async def test_extracts_single_blocker(self) -> None:
        """Extracts a single blocker key from text via LLM."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch("orchestrator.dependency_manager.call_llm_for_text", new=AsyncMock(return_value='["QR-232"]')):
            result = await extract_blocker_keys_from_text("Выполнять после мержа QR-232", "QR-100", config, cache)

        assert result == {"QR-232"}

    async def test_extracts_multiple_blockers(self) -> None:
        """Extracts multiple blocker keys from text."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch(
            "orchestrator.dependency_manager.call_llm_for_text", new=AsyncMock(return_value='["QR-230", "QR-231"]')
        ):
            result = await extract_blocker_keys_from_text("Depends on QR-230 and QR-231", "QR-100", config, cache)

        assert result == {"QR-230", "QR-231"}

    async def test_filters_self_reference(self) -> None:
        """Self-referencing key is filtered out."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch("orchestrator.dependency_manager.call_llm_for_text", new=AsyncMock(return_value='["QR-100"]')):
            result = await extract_blocker_keys_from_text("Related to QR-100", "QR-100", config, cache)

        assert result == set()

    async def test_filters_invalid_keys(self) -> None:
        """Non-issue keys are filtered out."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch(
            "orchestrator.dependency_manager.call_llm_for_text",
            new=AsyncMock(return_value='["QR-123", "not-a-key", "123"]'),
        ):
            result = await extract_blocker_keys_from_text("Some text", "QR-100", config, cache)

        assert result == {"QR-123"}

    async def test_fail_open_on_llm_error(self) -> None:
        """LLM call error returns empty set (fail-open)."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch(
            "orchestrator.dependency_manager.call_llm_for_text",
            new=AsyncMock(side_effect=RuntimeError("LLM error")),
        ):
            result = await extract_blocker_keys_from_text("Some text", "QR-100", config, cache)

        assert result == set()

    async def test_fail_open_on_invalid_json(self) -> None:
        """Invalid JSON response returns empty set (fail-open)."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch("orchestrator.dependency_manager.call_llm_for_text", new=AsyncMock(return_value="I don't know")):
            result = await extract_blocker_keys_from_text("Some text", "QR-100", config, cache)

        assert result == set()

    async def test_description_with_curly_braces_in_prompt(self) -> None:
        """Prompt building should handle curly braces in description."""
        from orchestrator.dependency_manager import _BLOCKER_EXTRACTION_PROMPT_PREFIX

        # Description with various types of curly braces
        description = """
        Implement after QR-123.
        Code: if (x) { return true; }
        JSON: {"key": "value"}
        Template: {variable}
        """

        # Build prompt using the same method as the code
        prompt = _BLOCKER_EXTRACTION_PROMPT_PREFIX + description

        # Should contain the full description including all braces
        assert "Code: if (x) { return true; }" in prompt
        assert '{"key": "value"}' in prompt
        assert "{variable}" in prompt
        assert "QR-123" in prompt

    async def test_description_with_curly_braces_end_to_end(self) -> None:
        """Description containing curly braces should work end-to-end."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        # Description with curly braces
        description = 'Depends on QR-123. Code: if (x) { return true; } JSON: {"key": "value"}'

        with patch("orchestrator.dependency_manager.call_llm_for_text", new=AsyncMock(return_value='["QR-123"]')):
            result = await extract_blocker_keys_from_text(description, "QR-200", config, cache)

        # Should successfully extract QR-123 despite curly braces
        assert result == {"QR-123"}

    async def test_caches_result_for_same_key(self) -> None:
        """Result is cached — same key calls LLM only once."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch(
            "orchestrator.dependency_manager.call_llm_for_text", new=AsyncMock(return_value='["QR-232"]')
        ) as mock_llm:
            # First call
            result1 = await extract_blocker_keys_from_text("Some text", "QR-100", config, cache)
            # Second call with same key
            result2 = await extract_blocker_keys_from_text("Some text", "QR-100", config, cache)

        assert result1 == {"QR-232"}
        assert result2 == {"QR-232"}
        mock_llm.assert_awaited_once()

    async def test_different_keys_not_cached(self) -> None:
        """Different keys each call LLM."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch(
            "orchestrator.dependency_manager.call_llm_for_text", new=AsyncMock(return_value='["QR-232"]')
        ) as mock_llm:
            await extract_blocker_keys_from_text("Some text", "QR-100", config, cache)
            await extract_blocker_keys_from_text("Some text", "QR-101", config, cache)

        assert mock_llm.await_count == 2

    async def test_description_change_invalidates_cache(self) -> None:
        """Changing description for same key should re-call LLM (not use stale cache)."""
        from unittest.mock import patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch("orchestrator.dependency_manager.call_llm_for_text") as mock_llm:
            # First call with one blocker
            mock_llm.return_value = '["QR-100"]'
            result1 = await extract_blocker_keys_from_text("Depends on QR-100", "QR-200", config, cache)
            assert result1 == {"QR-100"}

            # Second call with DIFFERENT description but SAME key should call LLM again
            mock_llm.return_value = '["QR-101"]'
            result2 = await extract_blocker_keys_from_text("Depends on QR-101", "QR-200", config, cache)
            assert result2 == {"QR-101"}  # Should get new blocker, not cached QR-100

        assert mock_llm.await_count == 2  # Should have called LLM twice

    async def test_cache_is_lru_not_fifo(self) -> None:
        """Cache should evict least recently used entries, not oldest inserted."""
        from unittest.mock import patch

        from orchestrator.dependency_manager import extract_blocker_keys_from_text

        config = MagicMock()
        cache: dict[str, set[str]] = {}

        with patch("orchestrator.dependency_manager.call_llm_for_text", return_value='["QR-999"]'):
            # Add 3 entries to cache
            await extract_blocker_keys_from_text("Text A", "QR-1", config, cache)
            await extract_blocker_keys_from_text("Text B", "QR-2", config, cache)
            await extract_blocker_keys_from_text("Text C", "QR-3", config, cache)

            assert len(cache) == 3

            # Access the OLDEST entry (QR-1) - this should move it to the end (most recent)
            await extract_blocker_keys_from_text("Text A", "QR-1", config, cache)

            # Verify that accessing moved it to the end in LRU order
            # In Python 3.7+, dict maintains insertion order
            # After accessing QR-1, order should be: QR-2, QR-3, QR-1 (QR-1 is most recent)
            cache_keys = list(cache.keys())
            # The last key should contain "QR-1" (moved to end by LRU access)
            assert "QR-1" in cache_keys[-1], f"Expected QR-1 at end (LRU), but order is: {cache_keys}"


# ---------------------------------------------------------------------------
# DependencyManager.check_dependencies
# ---------------------------------------------------------------------------


def _make_manager(
    links_by_key: dict[str, list[dict]] | None = None,
    issues_by_key: dict[str, MagicMock] | None = None,
    link_fetch_error: bool = False,
    storage: MagicMock | None = None,
    with_config: bool = True,
) -> tuple:
    """Build a DependencyManager with mocked tracker and event bus.

    Args:
        with_config: If True (default), creates manager with a mock config.
                     If False, creates manager with config=None.
    """
    from orchestrator.dependency_manager import DependencyManager

    tracker = MagicMock()
    event_bus = AsyncMock()
    config = MagicMock() if with_config else None

    def get_links(key: str) -> list[dict]:
        if link_fetch_error:
            raise requests.ConnectionError("Network error")
        if links_by_key is None:
            return []
        return links_by_key.get(key, [])

    def get_issue(key: str) -> MagicMock:
        if issues_by_key is None:
            raise requests.ConnectionError("Issue not found")
        if key not in issues_by_key:
            raise requests.ConnectionError(f"Issue {key} not found")
        return issues_by_key[key]

    tracker.get_links = get_links
    tracker.get_issue = get_issue

    manager = DependencyManager(tracker=tracker, event_bus=event_bus, storage=storage, config=config)
    return manager, tracker, event_bus


def _make_storage() -> MagicMock:
    """Build a mock storage with async methods for deferred tasks."""
    storage = MagicMock()
    storage.upsert_deferred_task = AsyncMock()
    storage.delete_deferred_task = AsyncMock()
    storage.load_deferred_tasks = AsyncMock(return_value=[])
    return storage


def _make_issue(status: str = "open") -> MagicMock:
    issue = MagicMock()
    issue.status = status
    return issue


class TestCheckDependencies:
    async def test_no_links_returns_false(self) -> None:
        """No links → dispatch allowed."""
        manager, _, _ = _make_manager(links_by_key={"QR-204": []})
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is False

    async def test_all_blockers_resolved_returns_false(self) -> None:
        """All blockers resolved → dispatch allowed."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="Done")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is False

    async def test_unresolved_blocker_returns_true_and_defers(self) -> None:
        """Unresolved blocker → deferred."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is True
        assert manager.is_deferred("QR-204")

    async def test_publishes_task_deferred_event(self) -> None:
        """TASK_DEFERRED event published when task is deferred."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, event_bus = _make_manager(links_by_key=links, issues_by_key=issues)
        await manager.check_dependencies("QR-204", "Test task")

        event_bus.publish.assert_called_once()
        event = event_bus.publish.call_args[0][0]
        assert event.type == EventType.TASK_DEFERRED
        assert event.task_key == "QR-204"
        assert "QR-203" in event.data["blockers"]

    async def test_fail_open_on_link_fetch_error(self) -> None:
        """Error fetching links → allow dispatch (fail-open)."""
        manager, _, _ = _make_manager(link_fetch_error=True)
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is False

    async def test_treats_blocker_fetch_error_as_unresolved(self) -> None:
        """Error fetching blocker issue → treat as unresolved (conservative)."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        # No issues_by_key → get_issue raises requests.ConnectionError
        manager, _, _ = _make_manager(links_by_key=links)
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is True
        assert manager.is_deferred("QR-204")

    async def test_cancelled_blocker_is_resolved(self) -> None:
        """Cancelled blocker counts as resolved → dispatch allowed."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="Cancelled")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is False

    async def test_already_deferred_returns_true_without_recheck(self) -> None:
        """Already deferred task returns True immediately."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, event_bus = _make_manager(links_by_key=links, issues_by_key=issues)

        # First call defers
        await manager.check_dependencies("QR-204", "Test task")
        event_bus.reset_mock()

        # Second call returns True without re-publishing
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is True
        event_bus.publish.assert_not_called()

    async def test_text_dependency_defers_task(self) -> None:
        """Task with text-based dependency is deferred if blocker is unresolved."""
        from unittest.mock import AsyncMock, patch

        links: dict[str, list[dict[str, Any]]] = {"QR-204": []}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        # Mock extract_blocker_keys_from_text to return QR-203
        with patch(
            "orchestrator.dependency_manager.extract_blocker_keys_from_text",
            new=AsyncMock(return_value={"QR-203"}),
        ):
            result = await manager.check_dependencies("QR-204", "Test task", description="Выполнять после QR-203")

        assert result is True
        assert manager.is_deferred("QR-204")

    async def test_text_dependency_combined_with_links(self) -> None:
        """Text blockers and link blockers are merged."""
        from unittest.mock import AsyncMock, patch

        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-202"}}]}
        issues = {"QR-202": _make_issue(status="open"), "QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        with patch(
            "orchestrator.dependency_manager.extract_blocker_keys_from_text",
            new=AsyncMock(return_value={"QR-203"}),
        ):
            result = await manager.check_dependencies("QR-204", "Test task", description="Also depends on QR-203")

        assert result is True
        deferred = manager.get_deferred()
        assert set(deferred["QR-204"].blockers) == {"QR-202", "QR-203"}

    async def test_text_dependency_fail_open(self) -> None:
        """Text extraction error does not block dispatch (fail-open)."""
        from unittest.mock import AsyncMock, patch

        links: dict[str, list[dict[str, Any]]] = {"QR-204": []}
        manager, _, _ = _make_manager(links_by_key=links)

        with patch(
            "orchestrator.dependency_manager.extract_blocker_keys_from_text",
            new=AsyncMock(side_effect=RuntimeError("LLM error")),
        ):
            result = await manager.check_dependencies("QR-204", "Test task", description="Some text")

        assert result is False
        assert not manager.is_deferred("QR-204")

    async def test_description_none_skips_text_extraction(self) -> None:
        """When description=None, text extraction is not called."""
        from unittest.mock import AsyncMock, patch

        links: dict[str, list[dict[str, Any]]] = {"QR-204": []}
        manager, _, _ = _make_manager(links_by_key=links)

        with patch(
            "orchestrator.dependency_manager.extract_blocker_keys_from_text",
            new=AsyncMock(side_effect=AssertionError("Should not be called")),
        ):
            result = await manager.check_dependencies("QR-204", "Test task", description=None)

        assert result is False

    async def test_no_config_skips_text_extraction(self) -> None:
        """When manager has no config, text extraction is not called."""
        from unittest.mock import AsyncMock, patch

        # Create manager without config
        links: dict[str, list[dict[str, Any]]] = {"QR-204": []}
        manager, _, _ = _make_manager(links_by_key=links, with_config=False)

        with patch(
            "orchestrator.dependency_manager.extract_blocker_keys_from_text",
            new=AsyncMock(side_effect=AssertionError("Should not be called")),
        ):
            result = await manager.check_dependencies("QR-204", "Test task", description="Some text")

        # Text extraction not called because manager._config is None
        assert result is False

    async def test_link_fetch_error_still_checks_text_dependencies(self) -> None:
        """Link fetch failure should not skip text-based dependency checks."""
        from unittest.mock import AsyncMock, patch

        # Link fetch will fail, but we have a text blocker
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(link_fetch_error=True, issues_by_key=issues)

        with patch(
            "orchestrator.dependency_manager.extract_blocker_keys_from_text",
            new=AsyncMock(return_value={"QR-203"}),
        ):
            result = await manager.check_dependencies("QR-204", "Test task", description="Depends on QR-203")

        # Should defer due to text blocker even though link fetch failed
        assert result is True
        assert manager.is_deferred("QR-204")


# ---------------------------------------------------------------------------
# DependencyManager.recheck_deferred
# ---------------------------------------------------------------------------


class TestRecheckDeferred:
    async def test_removes_unblocked_task(self) -> None:
        """When blockers resolve, task should be removed from deferred."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _tracker, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        # Defer the task
        await manager.check_dependencies("QR-204", "Test task")
        assert manager.is_deferred("QR-204")

        # Now resolve the blocker
        issues["QR-203"].status = "Done"
        unblocked = await manager.recheck_deferred()

        assert "QR-204" in unblocked
        assert not manager.is_deferred("QR-204")

    async def test_publishes_task_unblocked_event(self) -> None:
        """TASK_UNBLOCKED event published when task is unblocked."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, event_bus = _make_manager(links_by_key=links, issues_by_key=issues)

        await manager.check_dependencies("QR-204", "Test task")
        event_bus.reset_mock()

        # Resolve blocker
        issues["QR-203"].status = "Done"
        await manager.recheck_deferred()

        # Find TASK_UNBLOCKED event
        calls = event_bus.publish.call_args_list
        unblocked_events = [c for c in calls if c[0][0].type == EventType.TASK_UNBLOCKED]
        assert len(unblocked_events) == 1
        assert unblocked_events[0][0][0].task_key == "QR-204"

    async def test_keeps_still_blocked_task(self) -> None:
        """Task with unresolved blockers stays deferred."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        await manager.check_dependencies("QR-204", "Test task")
        unblocked = await manager.recheck_deferred()

        assert unblocked == []
        assert manager.is_deferred("QR-204")

    async def test_empty_deferred_set(self) -> None:
        """Empty deferred set returns empty list."""
        manager, _, _ = _make_manager()
        unblocked = await manager.recheck_deferred()
        assert unblocked == []

    async def test_returns_unblocked_keys(self) -> None:
        """Returns list of all unblocked keys."""
        links = {
            "QR-204": [{"relationship": "depends on", "issue": {"key": "QR-200"}}],
            "QR-205": [{"relationship": "depends on", "issue": {"key": "QR-200"}}],
        }
        issues = {"QR-200": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        await manager.check_dependencies("QR-204", "Task A")
        await manager.check_dependencies("QR-205", "Task B")

        # Resolve shared blocker
        issues["QR-200"].status = "Done"
        unblocked = await manager.recheck_deferred()

        assert set(unblocked) == {"QR-204", "QR-205"}

    async def test_text_blocker_rechecked(self) -> None:
        """Task deferred via text blocker is unblocked when blocker resolves."""
        from unittest.mock import AsyncMock, patch

        links: dict[str, list[dict[str, Any]]] = {"QR-204": []}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        # Defer via text blocker
        with patch(
            "orchestrator.dependency_manager.extract_blocker_keys_from_text",
            new=AsyncMock(return_value={"QR-203"}),
        ):
            await manager.check_dependencies("QR-204", "Test task", description="Выполнять после QR-203")

        assert manager.is_deferred("QR-204")

        # Resolve blocker
        issues["QR-203"].status = "Done"
        unblocked = await manager.recheck_deferred()

        assert "QR-204" in unblocked
        assert not manager.is_deferred("QR-204")


# ---------------------------------------------------------------------------
# Non-issue blocker skip (preflight reviews)
# ---------------------------------------------------------------------------


class TestManualDeferralSkipsRecheck:
    async def test_manual_deferral_not_rechecked(self) -> None:
        """Manual deferrals (preflight, supervisor) must not trigger Tracker API.

        Bug: recheck_deferred() called tracker.get_issue() on every
        blocker string, including free-text reasons from manual deferrals
        like "preflight_review: Git commits found: ...". This caused
        400 Bad Request errors every poll cycle.

        Fix: _recheck_single() skips manual tasks entirely.
        """
        manager, tracker, _ = _make_manager()
        tracker.get_issue = MagicMock(
            side_effect=AssertionError("get_issue should not be called"),
        )
        await manager.defer_task(
            "QR-192",
            "SEO task",
            "preflight_review: Git commits found: abc123",
        )

        unblocked = await manager.recheck_deferred()

        tracker.get_issue.assert_not_called()
        assert unblocked == []
        assert manager.is_deferred("QR-192")

    async def test_non_manual_deferral_still_rechecked(self) -> None:
        """Regular (non-manual) deferrals must still be rechecked."""
        links = {
            "QR-204": [
                {"relationship": "depends on", "issue": {"key": "QR-203"}},
            ],
        }
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(
            links_by_key=links,
            issues_by_key=issues,
        )

        await manager.check_dependencies("QR-204", "Test task")
        assert manager.is_deferred("QR-204")

        # Resolve blocker
        issues["QR-203"].status = "Done"
        unblocked = await manager.recheck_deferred()
        assert "QR-204" in unblocked

    async def test_is_issue_key_defense_in_depth(self) -> None:
        """_is_issue_key guards against non-issue keys as defense-in-depth."""
        from orchestrator.dependency_manager import _is_issue_key

        assert _is_issue_key("QR-123") is True
        assert _is_issue_key("PROJ-1") is True
        assert _is_issue_key("preflight_review: evidence") is False
        assert _is_issue_key("some random text") is False
        assert _is_issue_key("") is False
        assert _is_issue_key("qr-123") is False


# ---------------------------------------------------------------------------
# DependencyManager.approve_dispatch
# ---------------------------------------------------------------------------


class TestApproveDispatch:
    async def test_removes_from_deferred(self) -> None:
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        await manager.check_dependencies("QR-204", "Test task")
        assert manager.is_deferred("QR-204")

        result = await manager.approve_dispatch("QR-204")
        assert result is True
        assert not manager.is_deferred("QR-204")

    async def test_returns_false_when_not_deferred(self) -> None:
        manager, _, _ = _make_manager()
        result = await manager.approve_dispatch("QR-999")
        assert result is False

    async def test_publishes_task_unblocked_event(self) -> None:
        """approve_dispatch must publish TASK_UNBLOCKED event."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, event_bus = _make_manager(links_by_key=links, issues_by_key=issues)

        await manager.check_dependencies("QR-204", "Test task")
        event_bus.reset_mock()

        await manager.approve_dispatch("QR-204")

        event_bus.publish.assert_called_once()
        event = event_bus.publish.call_args[0][0]
        assert event.type == EventType.TASK_UNBLOCKED
        assert event.task_key == "QR-204"
        assert event.data["source"] == "supervisor_approval"


# ---------------------------------------------------------------------------
# DependencyManager.defer_task
# ---------------------------------------------------------------------------


class TestDeferTask:
    async def test_adds_manual_deferral(self) -> None:
        manager, _, _ = _make_manager()
        result = await manager.defer_task("QR-204", "Test task", "Waiting for API design")
        assert result is True
        assert manager.is_deferred("QR-204")

    async def test_returns_false_if_already_deferred(self) -> None:
        manager, _, _ = _make_manager()
        await manager.defer_task("QR-204", "Test task", "reason1")
        result = await manager.defer_task("QR-204", "Test task", "reason2")
        assert result is False

    async def test_sets_manual_flag(self) -> None:
        manager, _, _ = _make_manager()
        await manager.defer_task("QR-204", "Test task", "reason")
        deferred = manager.get_deferred()
        assert deferred["QR-204"].manual is True

    async def test_manual_deferral_has_reason_as_blocker(self) -> None:
        manager, _, _ = _make_manager()
        await manager.defer_task("QR-204", "Test task", "Waiting for API design")
        deferred = manager.get_deferred()
        assert "Waiting for API design" in deferred["QR-204"].blockers

    async def test_publishes_task_deferred_event(self) -> None:
        """defer_task must publish TASK_DEFERRED event."""
        manager, _, event_bus = _make_manager()
        await manager.defer_task("QR-204", "Test task", "Waiting for design")

        event_bus.publish.assert_called_once()
        event = event_bus.publish.call_args[0][0]
        assert event.type == EventType.TASK_DEFERRED
        assert event.task_key == "QR-204"
        assert event.data["source"] == "supervisor_manual"


# ---------------------------------------------------------------------------
# DependencyManager.remove_deferred
# ---------------------------------------------------------------------------


class TestRemoveDeferred:
    async def test_removes_deferred_task(self) -> None:
        """remove_deferred removes from deferred set and returns True."""
        manager, _, _ = _make_manager()
        await manager.defer_task("QR-204", "Test task", "reason")
        assert manager.is_deferred("QR-204")

        result = await manager.remove_deferred("QR-204")
        assert result is True
        assert not manager.is_deferred("QR-204")

    async def test_returns_false_when_not_deferred(self) -> None:
        """remove_deferred returns False when task is not deferred."""
        manager, _, _ = _make_manager()
        result = await manager.remove_deferred("QR-999")
        assert result is False

    async def test_does_not_add_to_approved_set(self) -> None:
        """Unlike approve_dispatch, remove_deferred must NOT add to approved set.

        After remove_deferred, check_dependencies should re-evaluate the task
        (not bypass checks like approve_dispatch does).
        """
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        await manager.check_dependencies("QR-204", "Test task")
        assert manager.is_deferred("QR-204")

        await manager.remove_deferred("QR-204")
        assert not manager.is_deferred("QR-204")

        # check_dependencies should re-evaluate (not bypass like approve)
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is True  # Still blocked — deferred again

    async def test_deletes_from_storage(self) -> None:
        """remove_deferred persists deletion to storage."""
        storage = _make_storage()
        manager, _, _ = _make_manager(storage=storage)
        await manager.defer_task("QR-204", "Test task", "reason")
        await manager.drain_background_tasks()
        storage.reset_mock()

        await manager.remove_deferred("QR-204")
        await manager.drain_background_tasks()

        storage.delete_deferred_task.assert_called_once_with("QR-204")


# ---------------------------------------------------------------------------
# Persistence via SQLite
# ---------------------------------------------------------------------------


class TestPersistence:
    async def test_load_restores_deferred_from_storage(self) -> None:
        """load() restores _deferred dict from storage."""
        storage = _make_storage()
        storage.load_deferred_tasks.return_value = [
            DeferredTaskRecord(
                issue_key="QR-10",
                issue_summary="Saved task",
                blockers=["QR-9"],
                deferred_at=1000.0,
                manual=False,
            ),
        ]
        manager, _, _ = _make_manager(storage=storage)
        await manager.load()

        assert manager.is_deferred("QR-10")
        deferred = manager.get_deferred()
        assert deferred["QR-10"].issue_summary == "Saved task"
        assert deferred["QR-10"].blockers == ["QR-9"]

    async def test_load_does_not_publish_events(self) -> None:
        """load() must NOT publish TASK_DEFERRED events — they were already emitted before restart."""
        storage = _make_storage()
        storage.load_deferred_tasks.return_value = [
            DeferredTaskRecord(
                issue_key="QR-10",
                issue_summary="Saved",
                blockers=["QR-9"],
                deferred_at=1000.0,
            ),
        ]
        manager, _, event_bus = _make_manager(storage=storage)
        await manager.load()

        event_bus.publish.assert_not_called()

    async def test_check_dependencies_persists_deferred_task(self) -> None:
        """check_dependencies() persists newly deferred task to storage."""
        storage = _make_storage()
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues, storage=storage)

        await manager.check_dependencies("QR-204", "Test task")
        await manager.drain_background_tasks()

        storage.upsert_deferred_task.assert_called_once()
        record = storage.upsert_deferred_task.call_args[0][0]
        assert record.issue_key == "QR-204"
        assert record.manual is False

    async def test_recheck_unblock_deletes_from_storage(self) -> None:
        """recheck_deferred() deletes unblocked task from storage."""
        storage = _make_storage()
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues, storage=storage)

        await manager.check_dependencies("QR-204", "Test task")
        await manager.drain_background_tasks()
        storage.reset_mock()

        # Resolve blocker
        issues["QR-203"].status = "Done"
        await manager.recheck_deferred()
        await manager.drain_background_tasks()

        storage.delete_deferred_task.assert_called_once_with("QR-204")

    async def test_approve_dispatch_deletes_from_storage(self) -> None:
        """approve_dispatch() deletes task from storage."""
        storage = _make_storage()
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues, storage=storage)

        await manager.check_dependencies("QR-204", "Test task")
        await manager.drain_background_tasks()
        storage.reset_mock()

        await manager.approve_dispatch("QR-204")
        await manager.drain_background_tasks()

        storage.delete_deferred_task.assert_called_once_with("QR-204")

    async def test_defer_task_persists_to_storage(self) -> None:
        """defer_task() persists manual deferral to storage."""
        storage = _make_storage()
        manager, _, _ = _make_manager(storage=storage)

        await manager.defer_task("QR-204", "Test task", "Waiting for design")
        await manager.drain_background_tasks()

        storage.upsert_deferred_task.assert_called_once()
        record = storage.upsert_deferred_task.call_args[0][0]
        assert record.issue_key == "QR-204"
        assert record.manual is True

    async def test_works_without_storage(self) -> None:
        """All operations work when storage=None (backward compat)."""
        manager, _, _ = _make_manager(storage=None)

        # Manual defer (no persistence)
        result = await manager.defer_task("QR-204", "Test task", "reason")
        assert result is True
        assert manager.is_deferred("QR-204")

        # Approve (no persistence)
        result = await manager.approve_dispatch("QR-204")
        assert result is True
        assert not manager.is_deferred("QR-204")


# ---------------------------------------------------------------------------
# Approved set (re-defer protection)
# ---------------------------------------------------------------------------


class TestApprovedSet:
    async def test_approve_prevents_redefer(self) -> None:
        """After approve_dispatch, check_dependencies must NOT re-defer the task."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        # Defer, then approve
        await manager.check_dependencies("QR-204", "Test task")
        assert manager.is_deferred("QR-204")
        await manager.approve_dispatch("QR-204")
        assert not manager.is_deferred("QR-204")

        # check_dependencies must NOT re-defer (blocker still open)
        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is False
        assert not manager.is_deferred("QR-204")

    async def test_approved_cleared_after_check(self) -> None:
        """The approved key is consumed (cleared) by check_dependencies."""
        links = {"QR-204": [{"relationship": "depends on", "issue": {"key": "QR-203"}}]}
        issues = {"QR-203": _make_issue(status="open")}
        manager, _, _ = _make_manager(links_by_key=links, issues_by_key=issues)

        # Defer → approve → check (consumes approved) → check again (should defer)
        await manager.check_dependencies("QR-204", "Test task")
        await manager.approve_dispatch("QR-204")

        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is False  # approved: bypassed

        result = await manager.check_dependencies("QR-204", "Test task")
        assert result is True  # approved consumed: deferred again


# ---------------------------------------------------------------------------
# API semaphore
# ---------------------------------------------------------------------------


class TestApiSemaphore:
    async def test_concurrent_checks_bounded_by_semaphore(self) -> None:
        """Semaphore limits concurrent API calls to max_concurrent_api_calls."""
        from orchestrator.dependency_manager import DependencyManager

        tracker = MagicMock()
        event_bus = AsyncMock()
        max_concurrent = 2

        # Track concurrency with thread-safe counter (runs in thread pool via to_thread)
        counter_lock = threading.Lock()
        current_concurrent = 0
        max_observed = 0

        def slow_get_issue(key: str) -> MagicMock:
            nonlocal current_concurrent, max_observed
            with counter_lock:
                current_concurrent += 1
                max_observed = max(max_observed, current_concurrent)
            time.sleep(0.02)  # runs in thread via asyncio.to_thread
            with counter_lock:
                current_concurrent -= 1
            issue = MagicMock()
            issue.status = "open"
            return issue

        tracker.get_issue = slow_get_issue
        # get_links returns one blocker per task
        tracker.get_links = lambda key: [{"relationship": "depends on", "issue": {"key": f"BLOCKER-{key}"}}]

        manager = DependencyManager(
            tracker=tracker,
            event_bus=event_bus,
            max_concurrent_api_calls=max_concurrent,
        )

        # Run 5 dependency checks concurrently — each triggers get_links + get_issue
        await asyncio.gather(*(manager.check_dependencies(f"QR-{i}", f"Task {i}") for i in range(5)))

        # The semaphore should have limited concurrency
        assert max_observed <= max_concurrent

    async def test_concurrent_llm_calls_bounded_by_semaphore(self) -> None:
        """Semaphore limits concurrent LLM text extraction calls."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.dependency_manager import DependencyManager

        tracker = MagicMock()
        event_bus = AsyncMock()
        config = MagicMock()
        max_concurrent_llm = 3

        # Track LLM call concurrency
        current_concurrent = 0
        max_observed = 0
        lock = asyncio.Lock()

        async def tracked_llm_call(*args, **kwargs):
            nonlocal current_concurrent, max_observed
            async with lock:
                current_concurrent += 1
                max_observed = max(max_observed, current_concurrent)
            await asyncio.sleep(0.02)  # Simulate LLM call
            async with lock:
                current_concurrent -= 1
            return '["QR-BLOCKER"]'

        tracker.get_links = lambda key: []  # No tracker links
        tracker.get_issue = lambda key: MagicMock(status="open")

        manager = DependencyManager(
            tracker=tracker,
            event_bus=event_bus,
            config=config,
            max_concurrent_llm_calls=max_concurrent_llm,
        )

        with patch("orchestrator.dependency_manager.call_llm_for_text", new=tracked_llm_call):
            # Check 10 tasks in parallel — semaphore should limit LLM concurrency to 3
            await asyncio.gather(
                *(
                    manager.check_dependencies(f"QR-{i}", "Test", description="Depends on QR-BLOCKER")
                    for i in range(10)
                ),
            )

        assert max_observed == max_concurrent_llm


# ---------------------------------------------------------------------------
# Locking (async methods)
# ---------------------------------------------------------------------------


class TestLocking:
    def test_approve_dispatch_is_async(self) -> None:
        """approve_dispatch must be an async method (uses lock)."""
        from orchestrator.dependency_manager import DependencyManager

        assert inspect.iscoroutinefunction(DependencyManager.approve_dispatch)

    def test_defer_task_is_async(self) -> None:
        """defer_task must be an async method (uses lock)."""
        from orchestrator.dependency_manager import DependencyManager

        assert inspect.iscoroutinefunction(DependencyManager.defer_task)
