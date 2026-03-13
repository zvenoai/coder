"""Task dependency manager — defers tasks with unresolved blockers.

Supports two types of dependency detection:
1. Structured Tracker links (depends on, is blocked by)
2. Text-based dependencies extracted via LLM from task description
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests

from orchestrator._persistence import BackgroundPersistenceMixin
from orchestrator.constants import DEPENDENCY_LINK_HINTS, EventType, is_cancelled_status, is_resolved_status
from orchestrator.epic_coordinator import EpicCoordinator
from orchestrator.event_bus import Event
from orchestrator.llm_utils import call_llm_for_text
from orchestrator.stats_models import DeferredTaskRecord

if TYPE_CHECKING:
    from orchestrator.config import Config
    from orchestrator.event_bus import EventBus
    from orchestrator.storage import Storage
    from orchestrator.tracker_client import TrackerClient

logger = logging.getLogger(__name__)


@dataclass
class DeferredTask:
    """A task deferred due to unresolved dependencies."""

    issue_key: str
    issue_summary: str
    blockers: list[str]
    deferred_at: float
    manual: bool = False


def extract_blocker_keys(links: Sequence[Mapping[str, Any]], issue_key: str) -> set[str]:
    """Extract blocker issue keys from Tracker link dicts.

    Looks for dependency relationships (depends on, is blocked by, blocked by)
    and extracts the linked issue key. Filters out self-references.
    """
    blocker_keys: set[str] = set()
    for link in links:
        relationship = str(link.get("relationship", "")).lower()
        if not any(hint in relationship for hint in DEPENDENCY_LINK_HINTS):
            continue
        linked_key = EpicCoordinator.extract_linked_issue_key(link)
        if linked_key and linked_key != issue_key:
            blocker_keys.add(linked_key)
    return blocker_keys


_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def _is_issue_key(value: str) -> bool:
    """Return True if value looks like a Tracker issue key (e.g. QR-123)."""
    return bool(_ISSUE_KEY_RE.match(value))


_BLOCKER_EXTRACTION_PROMPT_PREFIX = """\
You are a dependency extractor. Given a task description, \
extract issue keys that this task depends on or is blocked by.

Only extract keys where the text explicitly states a blocking \
dependency (e.g. "after merge of", "depends on", "blocked by", \
"выполнять после", "зависит от", "после мержа", "после закрытия").
Do NOT extract keys that are merely mentioned or referenced.

Return ONLY a JSON array of issue keys. Examples:
- ["QR-123", "QR-456"]
- []

Task description:
"""


_BLOCKER_EXTRACTION_SYSTEM_PROMPT = (
    "You are a dependency extraction assistant. Output only the JSON array, nothing else."
)


async def extract_blocker_keys_from_text(
    text: str | None,
    issue_key: str,
    config: Config,
    cache: dict[str, set[str]],
) -> set[str]:
    """Extract blocker issue keys from description text via LLM.

    Args:
        text: Task description text (may be None or empty)
        issue_key: Current task key (for filtering self-references)
        config: Orchestrator configuration
        cache: Cache dict keyed by (issue_key, text_hash)

    Returns:
        Set of blocker issue keys (filtered: valid keys only, no self-refs)
        Empty set on error (fail-open)
    """
    if not text:
        return set()

    # Cache key includes both issue_key and text hash to handle description edits
    import hashlib

    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    cache_key = f"{issue_key}:{text_hash}"

    # Check cache (LRU: move accessed entry to end)
    if cache_key in cache:
        # Move to end for LRU semantics (delete and re-add)
        value = cache.pop(cache_key)
        cache[cache_key] = value
        return value

    try:
        # Call LLM — build prompt without .format() to avoid issues with braces
        prompt = _BLOCKER_EXTRACTION_PROMPT_PREFIX + text
        response = await call_llm_for_text(
            prompt,
            config,
            system_prompt=_BLOCKER_EXTRACTION_SYSTEM_PROMPT,
            timeout_seconds=30,
            separator="",
        )

        # Parse JSON array
        try:
            keys = json.loads(response.strip())
            if not isinstance(keys, list):
                logger.warning("LLM response is not a JSON array for %s: %s", issue_key, response[:100])
                return set()
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM response as JSON for %s: %s", issue_key, response[:100])
            return set()

        # Filter: only valid issue keys, no self-reference
        blocker_keys = {k for k in keys if isinstance(k, str) and _is_issue_key(k) and k != issue_key}

        # Cache result (caller should handle size limits if needed)
        cache[cache_key] = blocker_keys
        return blocker_keys

    except Exception:
        logger.warning(
            "Text blocker extraction failed for %s, returning empty set (fail-open)", issue_key, exc_info=True
        )
        return set()


class DependencyManager(BackgroundPersistenceMixin):
    """Manages task dependency deferral and unblocking.

    Thread-safe via asyncio.Lock: protects _deferred from concurrent
    modification between poll_and_dispatch (recheck + check) and
    supervisor tool calls (approve_dispatch, defer_task).
    """

    def __init__(
        self,
        tracker: TrackerClient,
        event_bus: EventBus,
        storage: Storage | None = None,
        config: Config | None = None,
        max_concurrent_api_calls: int = 10,
        max_concurrent_llm_calls: int = 5,
    ) -> None:
        self._tracker = tracker
        self._event_bus = event_bus
        self._storage = storage
        self._config = config
        self._deferred: dict[str, DeferredTask] = {}
        self._approved: set[str] = set()
        self._lock = asyncio.Lock()
        self._api_semaphore = asyncio.Semaphore(max_concurrent_api_calls)
        self._llm_semaphore = asyncio.Semaphore(max_concurrent_llm_calls)
        self._text_blocker_cache: dict[str, set[str]] = {}
        self._text_blocker_cache_max_size = 1000  # Limit cache growth
        self._init_persistence()

    def is_deferred(self, key: str) -> bool:
        """Check if a task is currently deferred."""
        return key in self._deferred

    def get_deferred(self) -> dict[str, DeferredTask]:
        """Return a copy of the deferred tasks dict."""
        return dict(self._deferred)

    async def load(self) -> None:
        """Restore deferred tasks from storage. Does NOT publish events."""
        if self._storage is None:
            return
        records = await self._storage.load_deferred_tasks()
        for rec in records:
            self._deferred[rec.issue_key] = DeferredTask(
                issue_key=rec.issue_key,
                issue_summary=rec.issue_summary,
                blockers=rec.blockers,
                deferred_at=rec.deferred_at,
                manual=rec.manual,
            )
        if records:
            logger.info("Restored %d deferred task(s) from storage", len(records))

    def _persist_upsert(self, task: DeferredTask) -> None:
        """Schedule background upsert of a deferred task to storage."""
        if self._storage is None:
            return
        key = task.issue_key

        async def _do() -> None:
            async with self._key_locks[key]:
                if self._storage is None:
                    raise RuntimeError("storage is not set")
                await self._storage.upsert_deferred_task(
                    DeferredTaskRecord(
                        issue_key=task.issue_key,
                        issue_summary=task.issue_summary,
                        blockers=task.blockers,
                        deferred_at=task.deferred_at,
                        manual=task.manual,
                    )
                )

        self._schedule_task(_do())

    def _persist_delete(self, key: str) -> None:
        """Schedule background deletion of a deferred task from storage."""
        if self._storage is None:
            return

        async def _do() -> None:
            async with self._key_locks[key]:
                if self._storage is None:
                    raise RuntimeError("storage is not set")
                await self._storage.delete_deferred_task(key)

        self._schedule_task(_do())

    async def _check_blocker(self, blocker_key: str, task_key: str) -> str | None:
        """Check a single blocker. Returns key if unresolved, None if resolved.

        Non-issue blockers (preflight reviews, manual reasons) are
        always unresolved — only supervisor tools can remove them.
        Conservative: fetch errors → treat as unresolved.
        Rate-limited via semaphore to avoid overwhelming Tracker API.
        """
        # Non-issue blockers (e.g. "preflight_review: ...") cannot
        # be resolved via Tracker API — skip the API call entirely.
        if not _is_issue_key(blocker_key):
            return blocker_key

        async with self._api_semaphore:
            try:
                blocker_issue = await asyncio.to_thread(self._tracker.get_issue, blocker_key)
                if is_resolved_status(blocker_issue.status) or is_cancelled_status(blocker_issue.status):
                    return None
                return blocker_key
            except requests.RequestException:
                logger.warning(
                    "Failed to check blocker %s for %s, treating as unresolved",
                    blocker_key,
                    task_key,
                    exc_info=True,
                )
                return blocker_key

    async def check_dependencies(self, key: str, summary: str, description: str | None = None) -> bool:
        """Check if a task has unresolved blockers. Returns True if deferred.

        Fail-open: errors fetching links → allow dispatch.
        Conservative: errors fetching a blocker → treat as unresolved.
        Blocker status checks run in parallel via asyncio.gather.
        """
        async with self._lock:
            # If approved by supervisor, consume the approval and allow dispatch
            if key in self._approved:
                self._approved.discard(key)
                return False
            if key in self._deferred:
                return True

        # Fetch links (fail-open on error)
        blocker_keys: set[str] = set()
        try:
            async with self._api_semaphore:
                links = await asyncio.to_thread(self._tracker.get_links, key)
            blocker_keys = extract_blocker_keys(links, key)
        except requests.RequestException:
            logger.warning(
                "Failed to fetch links for %s, continuing with text-only blockers (fail-open)", key, exc_info=True
            )

        # Extract text-based blockers via LLM (fail-open on error)
        if description and self._config is not None:
            try:
                async with self._llm_semaphore:
                    text_blockers = await extract_blocker_keys_from_text(
                        description, key, self._config, self._text_blocker_cache
                    )
                blocker_keys |= text_blockers

                # Evict least recently used entries if cache exceeds size limit
                # LRU semantics: accessed entries moved to end in extract_blocker_keys_from_text
                # So first entry is least recently used (oldest access time)
                while len(self._text_blocker_cache) > self._text_blocker_cache_max_size:
                    # Pop first (least recently used) entry
                    self._text_blocker_cache.pop(next(iter(self._text_blocker_cache)))
            except Exception:
                logger.warning(
                    "Text blocker extraction failed for %s, continuing with link-only blockers (fail-open)",
                    key,
                    exc_info=True,
                )

        if not blocker_keys:
            return False

        # Check all blockers in parallel
        results = await asyncio.gather(*(self._check_blocker(bk, key) for bk in blocker_keys))
        unresolved = [r for r in results if r is not None]

        if not unresolved:
            return False

        # Defer the task
        async with self._lock:
            # Double-check: another coroutine may have approved/deferred it
            if key in self._approved:
                self._approved.discard(key)
                return False
            if key in self._deferred:
                return True
            task = DeferredTask(
                issue_key=key,
                issue_summary=summary,
                blockers=unresolved,
                deferred_at=time.time(),
            )
            self._deferred[key] = task

        self._persist_upsert(task)
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_DEFERRED,
                task_key=key,
                data={"summary": summary, "blockers": unresolved},
            )
        )
        logger.info("Deferred %s — blocked by: %s", key, ", ".join(unresolved))
        return True

    async def _recheck_single(self, key: str, deferred: DeferredTask) -> str | None:
        """Re-check one deferred task. Returns key if unblocked, None if still blocked.

        Manual deferrals (supervisor/preflight) are never auto-resolved —
        only supervisor tools can remove them.
        Checks all blockers in parallel. Conservative: any error → still blocked.
        """
        if deferred.manual:
            return None

        results = await asyncio.gather(*(self._check_blocker(bk, key) for bk in deferred.blockers))
        still_blocked = any(r is not None for r in results)

        if still_blocked:
            return None

        async with self._lock:
            self._deferred.pop(key, None)
        self._persist_delete(key)
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_UNBLOCKED,
                task_key=key,
                data={
                    "summary": deferred.issue_summary,
                    "previous_blockers": deferred.blockers,
                },
            )
        )
        logger.info("Unblocked %s — blockers resolved", key)
        return key

    async def recheck_deferred(self) -> list[str]:
        """Re-check all deferred tasks in parallel. Returns keys that are now unblocked.

        Each deferred task's blockers are checked concurrently, and all deferred
        tasks are rechecked concurrently via asyncio.gather.
        """
        async with self._lock:
            snapshot = dict(self._deferred)

        if not snapshot:
            return []

        results = await asyncio.gather(*(self._recheck_single(key, deferred) for key, deferred in snapshot.items()))
        return [r for r in results if r is not None]

    async def approve_dispatch(self, key: str) -> bool:
        """Force-approve a deferred task for immediate dispatch.

        Returns True if the task was deferred and is now approved,
        False if not found in deferred set.
        """
        async with self._lock:
            if key not in self._deferred:
                return False
            deferred = self._deferred.pop(key)
            self._approved.add(key)
        self._persist_delete(key)
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_UNBLOCKED,
                task_key=key,
                data={
                    "summary": deferred.issue_summary,
                    "previous_blockers": deferred.blockers,
                    "source": "supervisor_approval",
                },
            )
        )
        logger.info("Supervisor approved dispatch for deferred task %s", key)
        return True

    async def remove_deferred(self, key: str) -> bool:
        """Remove a task from deferred set without approving dispatch.

        Used for supervisor-confirmed skips: the task is removed from
        deferred but NOT added to the approved set.

        Returns True if found and removed, False if not found.
        """
        async with self._lock:
            if key not in self._deferred:
                return False
            self._deferred.pop(key)
        self._persist_delete(key)
        return True

    async def defer_task(self, key: str, summary: str, reason: str) -> bool:
        """Manually defer a task (semantic dependency from supervisor).

        Returns True if newly deferred, False if already deferred.
        """
        async with self._lock:
            if key in self._deferred:
                return False
            task = DeferredTask(
                issue_key=key,
                issue_summary=summary,
                blockers=[reason],
                deferred_at=time.time(),
                manual=True,
            )
            self._deferred[key] = task
        self._persist_upsert(task)
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_DEFERRED,
                task_key=key,
                data={"summary": summary, "blockers": [reason], "source": "supervisor_manual"},
            )
        )
        logger.info("Supervisor manually deferred %s: %s", key, reason)
        return True
