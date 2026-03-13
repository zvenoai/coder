"""Supervisor memory: markdown files + SQLite FTS5 hybrid search.

Source of truth: markdown files in data/memory/
Index: SQLite with FTS5 for keyword search + JSON embeddings for vector search.
Approach adapted from OpenClaw memory system.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Any

import aiosqlite

# Optional import — graceful degradation if not installed
try:
    import openai
except ImportError:
    openai = None  # type: ignore

logger = logging.getLogger(__name__)

# Chunking parameters (from OpenClaw)
_CHUNK_TARGET_CHARS = 1600  # ~400 tokens
_CHUNK_OVERLAP_CHARS = 320  # ~80 tokens

# Hybrid search weights
_VECTOR_WEIGHT = 0.7
_BM25_WEIGHT = 0.3

# Embedding model identifier (used in chunk ID generation)
_EMBEDDING_MODEL = "gemini-embedding-001"


# ---------------------------------------------------------------------------
# Cosine similarity (pure Python — corpus is small, ~hundreds of chunks)
# ---------------------------------------------------------------------------


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a: First vector
        b: Second vector

    Returns:
        Cosine similarity in [-1, 1], or 0.0 if either vector is zero.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# EmbeddingClient (kept from previous implementation)
# ---------------------------------------------------------------------------


class EmbeddingClient:
    """Wrapper for OpenAI-compatible embeddings API."""

    def __init__(self, api_key: str, base_url: str = "https://api.zveno.ai/v1") -> None:
        """Initialize embeddings client with API key.

        Args:
            api_key: API key for embeddings provider.
            base_url: Base URL for embeddings API (any OpenAI-compatible endpoint).

        Raises:
            ImportError: If openai package is not installed
        """
        mem_module = sys.modules[__name__]
        if mem_module.openai is None:  # type: ignore[attr-defined]
            raise ImportError("openai package is required for EmbeddingClient")
        self._client = mem_module.openai.OpenAI(api_key=api_key, base_url=base_url)  # type: ignore[attr-defined]

    def embed(self, text: str) -> list[float]:
        """Generate embedding vector for text using gemini-embedding-001 (768 dims).

        Args:
            text: Text to embed

        Returns:
            768-dimensional embedding vector
        """
        response = self._client.embeddings.create(input=text, model="google/gemini-embedding-001")
        return response.data[0].embedding


# ---------------------------------------------------------------------------
# Chunking (line-based, from OpenClaw)
# ---------------------------------------------------------------------------


def chunk_file(path: str | Path) -> list[dict[str, Any]]:
    """Split a markdown file into overlapping chunks.

    Args:
        path: Path to .md file

    Returns:
        List of dicts with keys: start_line, end_line, text
        Line numbers are 1-indexed.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    lines = text.splitlines()
    chunks: list[dict[str, Any]] = []

    # Accumulate lines until we hit the target size
    start_idx = 0  # 0-based index into lines
    while start_idx < len(lines):
        # Accumulate lines until target chars
        char_count = 0
        end_idx = start_idx
        while end_idx < len(lines) and char_count < _CHUNK_TARGET_CHARS:
            char_count += len(lines[end_idx]) + 1  # +1 for newline
            end_idx += 1

        # Build chunk text
        chunk_lines = lines[start_idx:end_idx]
        chunk_text = "\n".join(chunk_lines)

        chunks.append(
            {
                "start_line": start_idx + 1,  # 1-indexed
                "end_line": end_idx,  # inclusive, 1-indexed
                "text": chunk_text,
            }
        )

        if end_idx >= len(lines):
            break

        # Step forward by (target - overlap) chars
        step_chars = _CHUNK_TARGET_CHARS - _CHUNK_OVERLAP_CHARS
        step_count = 0
        next_start = start_idx
        while next_start < end_idx and step_count < step_chars:
            step_count += len(lines[next_start]) + 1
            next_start += 1

        # Ensure progress (at least one line forward)
        if next_start <= start_idx:
            next_start = start_idx + 1

        start_idx = next_start

    return chunks


def _list_md_files(directory: Path) -> list[Path]:
    """List .md files in directory, sorted by name."""
    return sorted(directory.glob("*.md"))


def _file_hash(path: Path) -> str:
    """Compute SHA-256 hash of file contents."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _text_hash(text: str) -> str:
    """Compute SHA-256 hash of text (for embedding cache)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk_id(path: str, start: int, end: int, file_hash: str) -> str:
    """Generate deterministic chunk ID."""
    raw = f"{path}:{start}:{end}:{file_hash}:{_EMBEDDING_MODEL}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# MemoryIndex — SQLite + FTS5 + vector embeddings
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- File tracking (incremental reindex)
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL
);

-- Chunks with embeddings
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    hash TEXT NOT NULL,
    model TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Embedding cache (avoid re-embedding identical text)
CREATE TABLE IF NOT EXISTS embedding_cache (
    hash TEXT PRIMARY KEY,
    embedding TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

# FTS5 table must be created separately (IF NOT EXISTS not supported for virtual tables in all versions)
_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, id UNINDEXED, path UNINDEXED, start_line UNINDEXED, end_line UNINDEXED
);
"""


class MemoryIndex:
    """SQLite-backed memory index with FTS5 for keyword search and JSON embeddings for vector search."""

    def __init__(self, memory_dir: str, index_path: str) -> None:
        """Initialize MemoryIndex.

        Args:
            memory_dir: Directory containing .md memory files
            index_path: Path to SQLite index database
        """
        self._memory_dir = memory_dir
        self._index_path = index_path
        self._db: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        # Serializes write operations (sync, reindex_file) on the shared _db
        # connection.  Multiple SupervisorChatManager instances share one
        # MemoryIndex; without this lock concurrent syncs race on
        # BEGIN IMMEDIATE, causing OperationalError and inadvertent ROLLBACKs.
        self._write_lock = asyncio.Lock()

    @property
    def memory_dir(self) -> str:
        """Public accessor for memory directory path."""
        return self._memory_dir

    async def initialize(self) -> None:
        """Create database and tables. Safe to call multiple times (idempotent)."""
        if self._db is not None:
            return  # Fast path: already initialized, no lock needed
        async with self._init_lock:
            # mypy narrows _db to None after the outer check, but another
            # coroutine may have set it while we awaited the lock.
            if self._db is not None:  # type: ignore[unreachable]  # double-checked locking
                return  # type: ignore[unreachable]

            # Ensure parent directory exists
            Path(self._index_path).parent.mkdir(parents=True, exist_ok=True)

            db = await aiosqlite.connect(self._index_path, isolation_level=None)
            try:
                await db.executescript(_SCHEMA_SQL)
                await db.executescript(_FTS_SQL)
            except Exception:
                await db.close()
                raise
            self._db = db

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def chunk_count(self) -> int:
        """Return total number of indexed chunks."""
        if self._db is None:
            raise RuntimeError("MemoryIndex not initialized. Call initialize() first.")
        async with self._db.execute("SELECT COUNT(*) FROM chunks") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def sync(self, embedder: EmbeddingClient) -> None:
        """Synchronize index with .md files in memory_dir.

        - Indexes new files
        - Reindexes changed files (by content hash)
        - Removes chunks from deleted files

        Args:
            embedder: EmbeddingClient for generating embeddings
        """
        async with self._write_lock:
            await self._sync_locked(embedder)

    async def _sync_locked(self, embedder: EmbeddingClient) -> None:
        """Inner sync() body; must be called under _write_lock."""
        if self._db is None:
            raise RuntimeError("MemoryIndex not initialized. Call initialize() first.")
        memory_path = Path(self._memory_dir)
        if not await asyncio.to_thread(memory_path.exists):
            return

        # Discover current .md files
        current_files: dict[str, Path] = {}
        for md_file in await asyncio.to_thread(_list_md_files, memory_path):
            current_files[str(md_file)] = md_file

        # Get tracked files from DB
        tracked: dict[str, str] = {}  # path -> hash
        async with self._db.execute("SELECT path, hash FROM files") as cursor:
            async for row in cursor:
                tracked[row[0]] = row[1]

        # Remove chunks for deleted files (atomic per batch)
        deleted = [p for p in tracked if p not in current_files]
        if deleted:
            await self._db.execute("BEGIN")
            try:
                for tracked_path in deleted:
                    await self._remove_file(tracked_path)
                await self._db.execute("COMMIT")
            except Exception:
                await self._db.execute("ROLLBACK")
                raise

        # Index new or changed files
        for file_path, md_file in current_files.items():
            current_hash = await asyncio.to_thread(_file_hash, md_file)
            if file_path in tracked and tracked[file_path] == current_hash:
                continue  # Unchanged
            await self._index_file(file_path, md_file, current_hash, embedder)

    async def reindex_file(self, file_path: str, embedder: EmbeddingClient) -> None:
        """Reindex a single file (after write).

        Args:
            file_path: Absolute path to the .md file
            embedder: EmbeddingClient for generating embeddings
        """
        async with self._write_lock:
            if self._db is None:
                raise RuntimeError("MemoryIndex not initialized. Call initialize() first.")
            md_file = Path(file_path)
            if not await asyncio.to_thread(md_file.exists):
                await self._remove_file(file_path)
                return

            current_hash = await asyncio.to_thread(_file_hash, md_file)
            await self._index_file(file_path, md_file, current_hash, embedder)

    async def _index_file(self, file_path: str, md_file: Path, file_hash: str, embedder: EmbeddingClient) -> None:
        """Index or reindex a single file (within a transaction)."""
        if self._db is None:
            raise RuntimeError("MemoryIndex not initialized. Call initialize() first.")

        # Chunk the file first (before starting transaction)
        chunks = await asyncio.to_thread(chunk_file, md_file)
        if not chunks:
            await self._remove_file(file_path)
            return

        # Pre-compute all embeddings before transaction
        embeddings: list[list[float]] = []
        for chunk in chunks:
            text_h = _text_hash(chunk["text"])
            embedding = await self._get_cached_embedding(text_h)
            if embedding is None:
                embedding = await asyncio.to_thread(embedder.embed, chunk["text"])
                await self._cache_embedding(text_h, embedding)
            embeddings.append(embedding)

        # All-or-nothing: transaction for index writes
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            # Remove old chunks for this file
            await self._remove_file(file_path)

            stat = await asyncio.to_thread(md_file.stat)
            now = time.time()

            # Upsert file record
            await self._db.execute(
                "INSERT OR REPLACE INTO files (path, hash, mtime, size) VALUES (?, ?, ?, ?)",
                (file_path, file_hash, stat.st_mtime, stat.st_size),
            )

            # Store chunks with pre-computed embeddings
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                chunk_id = _chunk_id(file_path, chunk["start_line"], chunk["end_line"], file_hash)
                embedding_json = json.dumps(embedding)

                await self._db.execute(
                    "INSERT OR REPLACE INTO chunks (id, path, start_line, end_line, hash, model, text, embedding, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        file_path,
                        chunk["start_line"],
                        chunk["end_line"],
                        file_hash,
                        _EMBEDDING_MODEL,
                        chunk["text"],
                        embedding_json,
                        now,
                    ),
                )

                # Insert into FTS
                await self._db.execute(
                    "INSERT INTO chunks_fts (text, id, path, start_line, end_line) VALUES (?, ?, ?, ?, ?)",
                    (chunk["text"], chunk_id, file_path, chunk["start_line"], chunk["end_line"]),
                )

            await self._db.execute("COMMIT")
        except Exception:
            await self._db.execute("ROLLBACK")
            raise

    async def _remove_file(self, file_path: str) -> None:
        """Remove all chunks and FTS entries for a file.

        Caller is responsible for transaction management (this method
        does NOT open its own transaction so it can be used inside
        _index_file's BEGIN IMMEDIATE block).
        """
        if self._db is None:
            raise RuntimeError("MemoryIndex not initialized. Call initialize() first.")

        # Get chunk IDs and delete from FTS by rowid for correctness
        async with self._db.execute("SELECT id FROM chunks WHERE path = ?", (file_path,)) as cursor:
            chunk_ids = [row[0] async for row in cursor]

        for cid in chunk_ids:
            # Delete FTS row where the stored id column matches.
            # FTS5 UNINDEXED columns support equality in WHERE (scan-based).
            await self._db.execute("DELETE FROM chunks_fts WHERE id = ?", (cid,))

        await self._db.execute("DELETE FROM chunks WHERE path = ?", (file_path,))
        await self._db.execute("DELETE FROM files WHERE path = ?", (file_path,))

    async def _get_cached_embedding(self, text_hash: str) -> list[float] | None:
        """Look up embedding in cache."""
        if self._db is None:
            raise RuntimeError("MemoryIndex not initialized. Call initialize() first.")
        async with self._db.execute("SELECT embedding FROM embedding_cache WHERE hash = ?", (text_hash,)) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Corrupted embedding cache for hash %s, skipping", text_hash)
                    return None
        return None

    async def _cache_embedding(self, text_hash: str, embedding: list[float]) -> None:
        """Store embedding in cache."""
        if self._db is None:
            raise RuntimeError("MemoryIndex not initialized. Call initialize() first.")
        await self._db.execute(
            "INSERT OR REPLACE INTO embedding_cache (hash, embedding, updated_at) VALUES (?, ?, ?)",
            (text_hash, json.dumps(embedding), time.time()),
        )

    async def hybrid_search(
        self,
        query_embedding: list[float],
        query_text: str,
        max_results: int = 6,
        min_score: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Hybrid search: vector (cosine) + BM25 (FTS5).

        Algorithm:
        1. Vector search: cosine similarity across all chunks, top N*4
        2. Keyword search: FTS5 MATCH + BM25, top N*4
        3. Merge by chunk ID: final_score = 0.7 * vector_score + 0.3 * bm25_score
        4. Filter by min_score, return top N

        Args:
            query_embedding: Query vector
            query_text: Query text for keyword search
            max_results: Maximum results to return
            min_score: Minimum combined score threshold

        Returns:
            List of dicts with keys: path, start_line, end_line, score, snippet
        """
        if self._db is None:
            raise RuntimeError("MemoryIndex not initialized. Call initialize() first.")
        candidates_n = max_results * 4

        # 1. Vector search — cosine similarity over all chunks
        vector_scores: dict[str, float] = {}
        chunk_data: dict[str, dict[str, Any]] = {}

        async with self._db.execute("SELECT id, path, start_line, end_line, text, embedding FROM chunks") as cursor:
            async for row in cursor:
                cid, path, start_line, end_line, text, emb_json = row
                try:
                    emb = json.loads(emb_json)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Corrupted embedding for chunk %s, skipping", cid)
                    continue
                sim = _cosine_similarity(query_embedding, emb)
                vector_scores[cid] = sim
                chunk_data[cid] = {
                    "path": path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "snippet": text,
                }

        if not vector_scores:
            return []

        # Take top N*4 by vector score
        top_vector = sorted(vector_scores.items(), key=lambda x: x[1], reverse=True)[:candidates_n]
        top_vector_ids = {cid for cid, _ in top_vector}

        # 2. Keyword search — FTS5
        bm25_scores: dict[str, float] = {}
        # Sanitize query for FTS5: escape special chars, split into tokens
        fts_query = _sanitize_fts_query(query_text)
        if fts_query:
            try:
                async with self._db.execute(
                    "SELECT id, rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, candidates_n),
                ) as cursor:
                    rank_idx = 0
                    async for row in cursor:
                        cid = row[0]
                        # BM25 score: 1 / (1 + rank_position)
                        # FTS5 rank is negative (lower = better), so use position
                        bm25_scores[cid] = 1.0 / (1.0 + rank_idx)
                        rank_idx += 1
            except Exception:
                # FTS query might fail on unusual characters — fall back to vector only
                logger.debug("FTS5 query failed for: %s", fts_query, exc_info=True)

        # 3. Merge scores
        all_ids = top_vector_ids | set(bm25_scores.keys())
        merged: list[tuple[str, float]] = []
        for cid in all_ids:
            v_score = vector_scores.get(cid, 0.0)
            b_score = bm25_scores.get(cid, 0.0)
            final = _VECTOR_WEIGHT * v_score + _BM25_WEIGHT * b_score
            if final >= min_score:
                merged.append((cid, final))

        # 4. Sort and limit
        merged.sort(key=lambda x: x[1], reverse=True)
        results: list[dict[str, Any]] = []
        for cid, score in merged[:max_results]:
            data = chunk_data.get(cid)
            if not data:
                # Chunk from BM25 but not loaded in vector pass — load it
                async with self._db.execute(
                    "SELECT path, start_line, end_line, text FROM chunks WHERE id = ?", (cid,)
                ) as cursor:
                    bm25_row = await cursor.fetchone()
                    if bm25_row:
                        data = {
                            "path": bm25_row[0],
                            "start_line": bm25_row[1],
                            "end_line": bm25_row[2],
                            "snippet": bm25_row[3],
                        }
            if data:
                results.append(
                    {
                        "path": data["path"],
                        "start_line": data["start_line"],
                        "end_line": data["end_line"],
                        "score": round(score, 4),
                        "snippet": data["snippet"],
                    }
                )

        return results


def _sanitize_fts_query(text: str) -> str:
    """Sanitize text for FTS5 MATCH query.

    Keeps only alphanumeric chars and whitespace, splits into tokens,
    escapes internal quotes, joins with OR.
    """
    # Keep only alphanumeric and whitespace — removes all FTS5 operators
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    tokens = [t.strip() for t in cleaned.split() if t.strip() and len(t.strip()) >= 2]
    if not tokens:
        return ""
    # Escape quotes within tokens, quote each token, join with OR
    safe_tokens = [t.replace('"', '""') for t in tokens[:10]]
    return " OR ".join(f'"{t}"' for t in safe_tokens)


# ---------------------------------------------------------------------------
# File listing & reading (for tools and bootstrap injection)
# ---------------------------------------------------------------------------


def list_memory_files(memory_dir: str) -> list[dict[str, Any]]:
    """List all .md files in the memory directory with metadata.

    Args:
        memory_dir: Path to the memory directory.

    Returns:
        List of dicts with keys: name, size_bytes, lines.
        Sorted by name (MEMORY.md first, then daily files descending).
    """
    d = Path(memory_dir)
    if not d.is_dir():
        return []

    files: list[dict[str, Any]] = []
    for md in sorted(d.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
            files.append(
                {
                    "name": md.name,
                    "size_bytes": md.stat().st_size,
                    "lines": len(text.splitlines()),
                }
            )
        except OSError:
            continue

    # Sort: MEMORY.md first, then daily files by name descending (newest first)
    memory = [f for f in files if f["name"] == "MEMORY.md"]
    daily = sorted(
        [f for f in files if f["name"] != "MEMORY.md"],
        key=lambda f: f["name"],
        reverse=True,
    )
    return memory + daily


def read_memory_file(memory_dir: str, filename: str, *, max_chars: int = 20000) -> str | None:
    """Read a memory file safely (no path traversal).

    Args:
        memory_dir: Path to the memory directory.
        filename: Filename only (e.g. 'MEMORY.md').
        max_chars: Max characters to return (default 20000, per OpenClaw).

    Returns:
        File content (truncated if needed), or None if not found / invalid.
    """
    if "/" in filename or "\\" in filename:
        return None
    if not filename.endswith(".md"):
        return None

    path = Path(memory_dir) / filename
    if not path.is_file():
        return None

    try:
        text = path.read_text(encoding="utf-8")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[truncated]"
        return text
    except OSError:
        return None


# ---------------------------------------------------------------------------
# recall_memories — auto-recall at session start
# ---------------------------------------------------------------------------


async def recall_memories(
    index: MemoryIndex, embedder: EmbeddingClient, prompt: str, limit: int = 3, min_score: float = 0.3
) -> str:
    """Recall relevant memories for a prompt and format as XML block.

    Args:
        index: MemoryIndex instance
        embedder: EmbeddingClient instance
        prompt: Query prompt
        limit: Maximum memories to recall
        min_score: Minimum similarity score

    Returns:
        Formatted XML block with memories, or empty string if none found
    """
    try:
        vector = await asyncio.to_thread(embedder.embed, prompt)
        results = await index.hybrid_search(
            query_embedding=vector,
            query_text=prompt,
            max_results=limit,
            min_score=min_score,
        )

        if not results:
            return ""

        lines = ["<relevant-memories>"]
        for r in results:
            rel_path = Path(r["path"]).name
            lines.append(
                f'  <memory path="{rel_path}" lines="{r["start_line"]}-{r["end_line"]}" score="{r["score"]:.2f}">'
            )
            lines.append(f"    {r['snippet']}")
            lines.append("  </memory>")
        lines.append("</relevant-memories>")
        return "\n".join(lines)
    except Exception:
        logger.exception("Error recalling memories")
        return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_memory_system(
    embedding_api_key: str,
    memory_dir: str,
    index_path: str,
    embedding_base_url: str = "https://api.zveno.ai/v1",
) -> tuple[MemoryIndex, EmbeddingClient] | None:
    """Factory function to create memory system with graceful degradation.

    Args:
        embedding_api_key: API key for embeddings provider.
        memory_dir: Path to memory directory with .md files
        index_path: Path to SQLite index database
        embedding_base_url: Base URL for OpenAI-compatible embeddings API.

    Returns:
        Tuple of (MemoryIndex, EmbeddingClient), or None if dependencies missing or no API key
    """
    if not embedding_api_key:
        logger.info("No embedding API key provided, memory system disabled")
        return None

    mem_module = sys.modules[__name__]
    if mem_module.openai is None:  # type: ignore[attr-defined]
        logger.info("openai package not available, memory system disabled")
        return None

    try:
        # Ensure memory directory exists
        Path(memory_dir).mkdir(parents=True, exist_ok=True)

        embedder = EmbeddingClient(
            api_key=embedding_api_key,
            base_url=embedding_base_url,
        )
        index = MemoryIndex(memory_dir=memory_dir, index_path=index_path)
        return (index, embedder)
    except ImportError as e:
        logger.warning("Memory system dependencies not available: %s", e)
        return None
