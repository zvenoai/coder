"""Supervisor — memory system for orchestrator and chat.

Retains only the memory initialization from the former SupervisorRunner.
Watch/run/trigger queue removed — orchestrator agent handles decisions.
"""

from __future__ import annotations

import logging

from orchestrator.config import Config
from orchestrator.storage import Storage
from orchestrator.supervisor_memory import EmbeddingClient, MemoryIndex, create_memory_system

logger = logging.getLogger(__name__)


class SupervisorRunner:
    """Provides memory system for supervisor chat and orchestrator agent.

    The watch/run/trigger queue has been removed. Only memory initialization
    and storage management remain.
    """

    def __init__(
        self,
        config: Config,
        storage: Storage | None = None,
    ) -> None:
        self._config = config
        self._storage = storage

        # Initialize memory system (SQLite + markdown files)
        self._memory_index: MemoryIndex | None
        self._embedder: EmbeddingClient | None

        result = create_memory_system(
            embedding_api_key=config.embedding_api_key,
            memory_dir=config.supervisor_memory_dir,
            index_path=config.supervisor_memory_index_path,
            embedding_base_url=config.embedding_base_url,
        )
        if result:
            self._memory_index, self._embedder = result
            logger.info("Supervisor memory system initialized")
        else:
            self._memory_index, self._embedder = None, None
            logger.info("Supervisor memory system disabled")

    def disable_storage(self) -> None:
        """Disable persistence after DB init failure."""
        self._storage = None

    @property
    def memory_index(self) -> MemoryIndex | None:
        """Get the memory index instance."""
        return self._memory_index

    @property
    def embedder(self) -> EmbeddingClient | None:
        """Get the embedder client instance."""
        return self._embedder

    # Kept for API backward compatibility with web.py status endpoint
    @property
    def is_running(self) -> bool:
        return False

    @property
    def last_run_at(self) -> float | None:
        return None

    @property
    def queue_size(self) -> int:
        return 0
