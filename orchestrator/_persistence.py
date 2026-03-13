"""Shared background-persistence utilities.

Provides a mixin class that manages background asyncio tasks for
non-blocking database writes. Used by RecoveryManager, EpicCoordinator,
PRMonitor, NeedsInfoMonitor, and ProposalManager.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Coroutine
from typing import Any


class BackgroundPersistenceMixin:
    """Mixin for components that persist state via background asyncio tasks.

    Subclasses must NOT define ``_background_tasks`` or ``_key_locks`` themselves;
    this mixin initialises them.  Call ``_init_persistence()`` from ``__init__``.
    """

    _storage: Any  # Set by subclasses; typed here so disable_storage() works.

    def _init_persistence(self) -> None:
        self._background_tasks: set[asyncio.Task] = set()
        self._key_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _schedule_task(self, coro: Coroutine[Any, Any, None]) -> None:
        """Schedule a coroutine as a background task, keeping a reference to prevent GC."""
        try:
            loop = asyncio.get_running_loop()
            task: asyncio.Task[None] = loop.create_task(coro)
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError:
            # No event loop — skip DB persistence (e.g. in sync tests)
            pass

    def disable_storage(self) -> None:
        """Disable persistence (e.g. after DB init failure). In-memory state is unaffected."""
        self._storage = None

    async def drain_background_tasks(self) -> None:
        """Wait for all pending background tasks to complete."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
