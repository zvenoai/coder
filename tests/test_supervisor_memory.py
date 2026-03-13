"""Tests for supervisor memory module (SQLite + FTS5 + hybrid search)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from orchestrator.supervisor_memory import (
    EmbeddingClient,
    MemoryIndex,
    chunk_file,
    create_memory_system,
    list_memory_files,
    read_memory_file,
    recall_memories,
)

# ---------------------------------------------------------------------------
# EmbeddingClient tests (kept from old implementation)
# ---------------------------------------------------------------------------


class TestEmbeddingClient:
    """Tests for EmbeddingClient."""

    def test_embed_calls_openai_api(self):
        """Test that embed() calls embeddings API correctly."""
        import orchestrator.supervisor_memory as mem_module

        mock_openai = Mock()
        mock_client = Mock()
        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 768)]
        mock_client.embeddings.create.return_value = mock_response
        mock_openai.OpenAI.return_value = mock_client

        original = mem_module.openai
        try:
            mem_module.openai = mock_openai
            client = EmbeddingClient(api_key="test-key")
            result = client.embed("test text")

            assert len(result) == 768
            mock_openai.OpenAI.assert_called_once_with(api_key="test-key", base_url="https://api.zveno.ai/v1")
            mock_client.embeddings.create.assert_called_once_with(
                input="test text", model="google/gemini-embedding-001"
            )
        finally:
            mem_module.openai = original

    def test_embed_raises_import_error_without_openai(self):
        """Test that EmbeddingClient raises ImportError if openai is not installed."""
        import orchestrator.supervisor_memory as mem_module

        original = mem_module.openai
        try:
            mem_module.openai = None
            with pytest.raises(ImportError, match="openai package is required"):
                EmbeddingClient(api_key="test-key")
        finally:
            mem_module.openai = original


# ---------------------------------------------------------------------------
# chunk_file tests
# ---------------------------------------------------------------------------


class TestChunkFile:
    """Tests for chunk_file() — line-based markdown splitting."""

    def test_empty_file(self, tmp_path: Path):
        """Empty file returns no chunks."""
        f = tmp_path / "empty.md"
        f.write_text("")
        assert chunk_file(f) == []

    def test_small_file_single_chunk(self, tmp_path: Path):
        """File smaller than target size produces one chunk."""
        f = tmp_path / "small.md"
        f.write_text("Hello world\nSecond line\n")
        chunks = chunk_file(f)
        assert len(chunks) == 1
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 2
        assert "Hello world" in chunks[0]["text"]
        assert "Second line" in chunks[0]["text"]

    def test_large_file_multiple_chunks(self, tmp_path: Path):
        """File larger than target size is split into overlapping chunks."""
        # ~200 chars per line * 20 lines = ~4000 chars > 1600 target
        lines = [f"Line {i}: " + "x" * 180 for i in range(1, 21)]
        f = tmp_path / "large.md"
        f.write_text("\n".join(lines) + "\n")
        chunks = chunk_file(f)
        assert len(chunks) > 1
        # All chunks should have valid line numbers (1-indexed)
        for c in chunks:
            assert c["start_line"] >= 1
            assert c["end_line"] >= c["start_line"]
            assert len(c["text"]) > 0

    def test_chunks_have_overlap(self, tmp_path: Path):
        """Adjacent chunks should overlap by ~320 chars."""
        lines = [f"Line {i}: " + "x" * 180 for i in range(1, 31)]
        f = tmp_path / "overlap.md"
        f.write_text("\n".join(lines) + "\n")
        chunks = chunk_file(f)
        if len(chunks) >= 2:
            # Second chunk should start before first chunk ends
            assert chunks[1]["start_line"] <= chunks[0]["end_line"]

    def test_1_indexed_lines(self, tmp_path: Path):
        """Line numbers are 1-indexed (not 0-indexed)."""
        f = tmp_path / "lines.md"
        f.write_text("first\nsecond\nthird\n")
        chunks = chunk_file(f)
        assert chunks[0]["start_line"] == 1

    def test_chunk_has_required_fields(self, tmp_path: Path):
        """Each chunk dict has start_line, end_line, text."""
        f = tmp_path / "fields.md"
        f.write_text("content here\n")
        chunks = chunk_file(f)
        assert len(chunks) == 1
        c = chunks[0]
        assert "start_line" in c
        assert "end_line" in c
        assert "text" in c


# ---------------------------------------------------------------------------
# MemoryIndex tests
# ---------------------------------------------------------------------------


class TestMemoryIndex:
    """Tests for MemoryIndex (SQLite + FTS5)."""

    @pytest.fixture
    def memory_dir(self, tmp_path: Path) -> Path:
        """Create a memory directory with a sample markdown file."""
        d = tmp_path / "memory"
        d.mkdir()
        (d / "MEMORY.md").write_text("# Project Knowledge\n\nWe use FastAPI for web.\n")
        return d

    @pytest.fixture
    def index_path(self, tmp_path: Path) -> Path:
        return tmp_path / ".index.sqlite"

    async def test_init_creates_tables(self, memory_dir: Path, index_path: Path):
        """MemoryIndex.__init__ should create SQLite tables."""
        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            assert index_path.exists()  # noqa: ASYNC240
        finally:
            await idx.close()

    async def test_initialize_retries_after_partial_failure(self, memory_dir: Path, index_path: Path):
        """If initialize() fails partway (after connect but before FTS), re-calling should retry."""
        import aiosqlite as _aiosqlite

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))

        # First initialize: connect succeeds, then we simulate FTS failure
        real_connect = _aiosqlite.connect

        call_count = 0

        async def connect_then_break(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            conn = await real_connect(*args, **kwargs)
            if call_count == 1:
                original_executescript = conn.executescript

                script_call = 0

                async def failing_executescript(sql: str) -> object:
                    nonlocal script_call
                    script_call += 1
                    if script_call == 2:  # Second executescript = _FTS_SQL
                        raise RuntimeError("FTS5 not available")
                    return await original_executescript(sql)

                conn.executescript = failing_executescript  # type: ignore[assignment]
            return conn

        with patch("orchestrator.supervisor_memory.aiosqlite.connect", side_effect=connect_then_break):
            with pytest.raises(RuntimeError, match="FTS5 not available"):
                await idx.initialize()

        # _db should have been reset to None so re-init is possible
        assert idx._db is None

        # Second call with real aiosqlite should succeed
        await idx.initialize()
        try:
            assert idx._db is not None
        finally:
            await idx.close()

    async def test_initialize_concurrent_calls_single_connection(self, memory_dir: Path, index_path: Path):
        """Concurrent initialize() calls must open exactly one DB connection.

        Without a lock, three concurrent coroutines all pass the
        ``if self._db is not None: return`` check before any of them sets
        ``self._db``, causing multiple connections to be opened (leak).
        """
        import asyncio
        from unittest.mock import patch

        import aiosqlite as _aiosqlite

        connect_calls = 0
        real_connect = _aiosqlite.connect

        async def counting_connect(*args: object, **kwargs: object) -> object:
            nonlocal connect_calls
            connect_calls += 1
            # Yield to the event loop here — this is the gap between the
            # ``if self._db is not None`` guard and ``self._db = db`` that
            # allows concurrent coroutines to race.
            await asyncio.sleep(0)
            return await real_connect(*args, **kwargs)

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        try:
            with patch(
                "orchestrator.supervisor_memory.aiosqlite.connect",
                side_effect=counting_connect,
            ):
                await asyncio.gather(
                    idx.initialize(),
                    idx.initialize(),
                    idx.initialize(),
                )
            # Without a lock, connect_calls > 1 (leaked connections).
            assert connect_calls == 1, f"Expected 1 DB connection, got {connect_calls} (race condition)"
            assert idx._db is not None
        finally:
            await idx.close()

    async def test_sync_concurrent_calls_do_not_conflict(self, memory_dir: Path, index_path: Path):
        """Concurrent sync() calls must not conflict on BEGIN IMMEDIATE.

        Three SupervisorChatManager instances share one MemoryIndex. When
        multiple managers call create_session() → memory.sync() concurrently,
        each issues BEGIN IMMEDIATE on the shared _db connection. Without a
        write lock, the second BEGIN IMMEDIATE raises OperationalError (SQLite
        cannot start a transaction within a transaction), and its ROLLBACK
        handler tears down the first manager's in-progress write.
        """
        import asyncio
        from unittest.mock import patch

        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            # Yield inside _index_file before BEGIN IMMEDIATE so that
            # the second concurrent sync() can reach _index_file too,
            # exposing the transaction conflict without the write lock.
            real_index_file = MemoryIndex._index_file

            async def slow_index_file(self_inner: MemoryIndex, *args: object, **kwargs: object) -> None:
                await asyncio.sleep(0)  # Force interleaving at the critical window
                await real_index_file(self_inner, *args, **kwargs)

            with patch.object(MemoryIndex, "_index_file", slow_index_file):
                # Without _write_lock on sync(), these two concurrent calls
                # race on BEGIN IMMEDIATE and raise OperationalError.
                await asyncio.gather(
                    idx.sync(mock_embedder),
                    idx.sync(mock_embedder),
                )
        finally:
            await idx.close()

    async def test_sync_indexes_files(self, memory_dir: Path, index_path: Path):
        """sync() should index all .md files in memory_dir."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)
            # Should have indexed MEMORY.md
            count = await idx.chunk_count()
            assert count > 0
        finally:
            await idx.close()

    async def test_sync_skips_unchanged_files(self, memory_dir: Path, index_path: Path):
        """sync() should skip files that haven't changed (same hash)."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)
            call_count_1 = mock_embedder.embed.call_count

            # Sync again without changes — should skip
            await idx.sync(mock_embedder)
            call_count_2 = mock_embedder.embed.call_count

            assert call_count_2 == call_count_1
        finally:
            await idx.close()

    async def test_sync_reindexes_changed_files(self, memory_dir: Path, index_path: Path):
        """sync() should reindex files whose content has changed."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)
            call_count_1 = mock_embedder.embed.call_count

            # Modify file
            (memory_dir / "MEMORY.md").write_text("# Updated\n\nNew content here.\n")

            await idx.sync(mock_embedder)
            call_count_2 = mock_embedder.embed.call_count

            # Should have made new embed calls
            assert call_count_2 > call_count_1
        finally:
            await idx.close()

    async def test_sync_indexes_new_files(self, memory_dir: Path, index_path: Path):
        """sync() should index newly created .md files."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)
            count_1 = await idx.chunk_count()

            # Add new file
            (memory_dir / "2026-02-16.md").write_text("## Today\n\nNew daily note.\n")

            await idx.sync(mock_embedder)
            count_2 = await idx.chunk_count()

            assert count_2 > count_1
        finally:
            await idx.close()

    async def test_sync_removes_deleted_files(self, memory_dir: Path, index_path: Path):
        """sync() should remove chunks from deleted .md files."""
        # Create a second file
        (memory_dir / "extra.md").write_text("Extra content\n")

        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)
            count_1 = await idx.chunk_count()

            # Delete extra file
            (memory_dir / "extra.md").unlink()

            await idx.sync(mock_embedder)
            count_2 = await idx.chunk_count()

            assert count_2 < count_1
        finally:
            await idx.close()

    async def test_hybrid_search_returns_results(self, memory_dir: Path, index_path: Path):
        """hybrid_search should return matching chunks."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)

            results = await idx.hybrid_search(
                query_embedding=[0.1] * 768,
                query_text="FastAPI",
                max_results=5,
                min_score=0.0,
            )
            assert len(results) > 0
            # Results should have required fields
            r = results[0]
            assert "path" in r
            assert "start_line" in r
            assert "end_line" in r
            assert "score" in r
            assert "snippet" in r
        finally:
            await idx.close()

    async def test_hybrid_search_empty_index(self, memory_dir: Path, index_path: Path):
        """hybrid_search on empty index returns empty list."""
        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            results = await idx.hybrid_search(
                query_embedding=[0.1] * 768,
                query_text="anything",
                max_results=5,
                min_score=0.0,
            )
            assert results == []
        finally:
            await idx.close()

    async def test_hybrid_search_min_score_filter(self, memory_dir: Path, index_path: Path):
        """hybrid_search respects min_score parameter."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)

            # With very high min_score, should return nothing
            results = await idx.hybrid_search(
                query_embedding=[0.0] * 768,  # zero vector — low similarity
                query_text="nonexistent_xyz_query",
                max_results=5,
                min_score=0.99,
            )
            assert len(results) == 0
        finally:
            await idx.close()

    async def test_hybrid_search_max_results(self, memory_dir: Path, index_path: Path):
        """hybrid_search respects max_results parameter."""
        # Create a file with lots of content
        lines = [f"## Section {i}\n\nContent about topic {i}.\n" for i in range(20)]
        (memory_dir / "MEMORY.md").write_text("\n".join(lines))

        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)

            results = await idx.hybrid_search(
                query_embedding=[0.1] * 768,
                query_text="topic",
                max_results=2,
                min_score=0.0,
            )
            assert len(results) <= 2
        finally:
            await idx.close()

    async def test_reindex_single_file(self, memory_dir: Path, index_path: Path):
        """reindex_file should re-chunk and re-embed a single file."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)

            # Modify file and reindex just that file
            (memory_dir / "MEMORY.md").write_text("# Updated Knowledge\n\nCompletely new content.\n")
            await idx.reindex_file(str(memory_dir / "MEMORY.md"), mock_embedder)

            results = await idx.hybrid_search(
                query_embedding=[0.1] * 768,
                query_text="Completely new content",
                max_results=5,
                min_score=0.0,
            )
            assert len(results) > 0
            assert "Completely new content" in results[0]["snippet"]
        finally:
            await idx.close()

    async def test_embedding_cache_avoids_re_embed(self, memory_dir: Path, index_path: Path):
        """Identical text chunks should use cached embeddings."""
        # Create two files with identical content
        (memory_dir / "a.md").write_text("Identical content here\n")
        (memory_dir / "b.md").write_text("Identical content here\n")

        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)
            # Embedding should be called only once for identical text
            # (second call uses cache)
            assert mock_embedder.embed.call_count == 2  # MEMORY.md + one of a/b
            # Actually, 3 files: MEMORY.md, a.md, b.md — but a.md and b.md have
            # identical text so the second should use cache
            # MEMORY.md text != a.md text, so we expect 2 unique embed calls
            # (MEMORY.md content, and "Identical content here")
        finally:
            await idx.close()


# ---------------------------------------------------------------------------
# recall_memories tests
# ---------------------------------------------------------------------------


class TestRecallMemories:
    """Tests for recall_memories function."""

    @pytest.fixture
    def memory_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "memory"
        d.mkdir()
        (d / "MEMORY.md").write_text("# Knowledge\n\nWe always use pytest for testing.\n")
        return d

    @pytest.fixture
    def index_path(self, tmp_path: Path) -> Path:
        return tmp_path / ".index.sqlite"

    async def test_empty_results(self, memory_dir: Path, index_path: Path):
        """Should return empty string when no memories match."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.0] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            result = await recall_memories(idx, mock_embedder, "completely unrelated xyz", limit=3, min_score=0.99)
            assert result == ""
        finally:
            await idx.close()

    async def test_xml_block_format(self, memory_dir: Path, index_path: Path):
        """Should format memories as XML block."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)

            result = await recall_memories(idx, mock_embedder, "pytest testing", limit=3, min_score=0.0)

            assert "<relevant-memories>" in result
            assert "</relevant-memories>" in result
            assert "<memory" in result
            assert "pytest" in result.lower() or "testing" in result.lower() or "Knowledge" in result
        finally:
            await idx.close()

    async def test_embed_error_returns_empty(self, memory_dir: Path, index_path: Path):
        """Should return empty string if embedding fails."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.side_effect = Exception("API error")

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            result = await recall_memories(idx, mock_embedder, "test prompt")
            assert result == ""
        finally:
            await idx.close()


# ---------------------------------------------------------------------------
# create_memory_system tests
# ---------------------------------------------------------------------------


class TestCreateMemorySystem:
    """Tests for create_memory_system function."""

    def test_returns_none_without_api_key(self, tmp_path: Path):
        """Should return None if no API key provided."""
        result = create_memory_system(
            embedding_api_key="",
            memory_dir=str(tmp_path / "memory"),
            index_path=str(tmp_path / ".index.sqlite"),
        )
        assert result is None

    def test_returns_none_without_openai(self, tmp_path: Path):
        """Should return None if openai package is not available."""
        import orchestrator.supervisor_memory as mem_module

        original = mem_module.openai
        try:
            mem_module.openai = None
            result = create_memory_system(
                embedding_api_key="test-key",
                memory_dir=str(tmp_path / "memory"),
                index_path=str(tmp_path / ".index.sqlite"),
            )
            assert result is None
        finally:
            mem_module.openai = original

    def test_returns_tuple_when_available(self, tmp_path: Path):
        """Should return (MemoryIndex, EmbeddingClient) when everything is available."""
        import orchestrator.supervisor_memory as mem_module

        mock_openai = Mock()
        mock_client = Mock()
        mock_openai.OpenAI.return_value = mock_client

        original = mem_module.openai
        try:
            mem_module.openai = mock_openai
            result = create_memory_system(
                embedding_api_key="test-key",
                memory_dir=str(tmp_path / "memory"),
                index_path=str(tmp_path / ".index.sqlite"),
            )

            assert result is not None
            assert isinstance(result, tuple)
            assert len(result) == 2
            assert isinstance(result[0], MemoryIndex)
            assert isinstance(result[1], EmbeddingClient)
        finally:
            mem_module.openai = original

    def test_creates_memory_dir_if_missing(self, tmp_path: Path):
        """Should create memory_dir if it doesn't exist."""
        import orchestrator.supervisor_memory as mem_module

        mock_openai = Mock()
        mock_openai.OpenAI.return_value = Mock()

        original = mem_module.openai
        try:
            mem_module.openai = mock_openai
            memory_dir = tmp_path / "nonexistent" / "memory"
            result = create_memory_system(
                embedding_api_key="test-key",
                memory_dir=str(memory_dir),
                index_path=str(tmp_path / ".index.sqlite"),
            )
            assert result is not None
            assert memory_dir.exists()
        finally:
            mem_module.openai = original


# ---------------------------------------------------------------------------
# Cosine similarity helper tests
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    """Tests for cosine similarity computation used in hybrid search."""

    def test_identical_vectors(self):
        """Cosine similarity of identical normalized vectors should be 1.0."""
        from orchestrator.supervisor_memory import _cosine_similarity

        v = [1.0, 0.0, 0.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        """Cosine similarity of orthogonal vectors should be 0.0."""
        from orchestrator.supervisor_memory import _cosine_similarity

        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_opposite_vectors(self):
        """Cosine similarity of opposite vectors should be -1.0."""
        from orchestrator.supervisor_memory import _cosine_similarity

        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6

    def test_zero_vector(self):
        """Cosine similarity with zero vector should be 0.0."""
        from orchestrator.supervisor_memory import _cosine_similarity

        a = [1.0, 2.0]
        b = [0.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0


# ---------------------------------------------------------------------------
# list_memory_files tests
# ---------------------------------------------------------------------------


class TestListMemoryFiles:
    """Tests for list_memory_files function."""

    def test_lists_md_files(self, tmp_path: Path):
        """Should list all .md files with metadata."""
        d = tmp_path / "memory"
        d.mkdir()
        (d / "MEMORY.md").write_text("# Long-term\n")
        (d / "2026-02-16.md").write_text("## Daily\n")
        (d / "not-md.txt").write_text("ignored\n")

        files = list_memory_files(str(d))
        assert len(files) == 2
        names = {f["name"] for f in files}
        assert names == {"MEMORY.md", "2026-02-16.md"}
        # Each entry has name, size_bytes, lines
        for f in files:
            assert "name" in f
            assert "size_bytes" in f
            assert "lines" in f

    def test_empty_directory(self, tmp_path: Path):
        """Should return empty list for directory with no .md files."""
        d = tmp_path / "memory"
        d.mkdir()
        assert list_memory_files(str(d)) == []

    def test_nonexistent_directory(self, tmp_path: Path):
        """Should return empty list for non-existent directory."""
        assert list_memory_files(str(tmp_path / "nope")) == []

    def test_daily_files_sorted_newest_first(self, tmp_path: Path):
        """Daily files should be sorted newest first (descending), with MEMORY.md always first."""
        d = tmp_path / "memory"
        d.mkdir()
        (d / "MEMORY.md").write_text("curated\n")
        (d / "2026-02-14.md").write_text("old\n")
        (d / "2026-02-16.md").write_text("new\n")
        (d / "2026-02-15.md").write_text("mid\n")

        files = list_memory_files(str(d))
        names = [f["name"] for f in files]
        assert names == ["MEMORY.md", "2026-02-16.md", "2026-02-15.md", "2026-02-14.md"]


# ---------------------------------------------------------------------------
# read_memory_file tests
# ---------------------------------------------------------------------------


class TestReadMemoryFile:
    """Tests for read_memory_file function."""

    def test_reads_existing_file(self, tmp_path: Path):
        """Should return file content."""
        d = tmp_path / "memory"
        d.mkdir()
        (d / "MEMORY.md").write_text("# Knowledge\nLine 2\n")
        content = read_memory_file(str(d), "MEMORY.md")
        assert content is not None
        assert "# Knowledge" in content

    def test_returns_none_for_missing_file(self, tmp_path: Path):
        """Should return None if file doesn't exist."""
        d = tmp_path / "memory"
        d.mkdir()
        assert read_memory_file(str(d), "nope.md") is None

    def test_rejects_path_traversal(self, tmp_path: Path):
        """Should return None for path traversal attempts."""
        d = tmp_path / "memory"
        d.mkdir()
        assert read_memory_file(str(d), "../etc/passwd") is None
        assert read_memory_file(str(d), "sub/file.md") is None

    def test_truncates_large_file(self, tmp_path: Path):
        """Should truncate content beyond max_chars."""
        d = tmp_path / "memory"
        d.mkdir()
        (d / "big.md").write_text("x" * 5000)
        content = read_memory_file(str(d), "big.md", max_chars=100)
        assert content is not None
        assert len(content) <= 120  # 100 + truncation marker


# ---------------------------------------------------------------------------
# FTS cleanup verification tests
# ---------------------------------------------------------------------------


class TestFTSCleanup:
    """Tests that FTS entries are properly cleaned up on file removal."""

    @pytest.fixture
    def memory_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "memory"
        d.mkdir()
        (d / "a.md").write_text("Alpha content about search\n")
        (d / "b.md").write_text("Beta content about search\n")
        return d

    @pytest.fixture
    def index_path(self, tmp_path: Path) -> Path:
        return tmp_path / ".index.sqlite"

    async def test_fts_entries_removed_on_file_delete(self, memory_dir: Path, index_path: Path):
        """FTS entries should be removed when a file is deleted and re-synced."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)

            # Verify FTS has entries for both files
            assert idx._db is not None
            async with idx._db.execute("SELECT COUNT(*) FROM chunks_fts") as cur:
                row = await cur.fetchone()
                fts_count_before = row[0] if row else 0
            assert fts_count_before >= 2

            # Delete one file
            (memory_dir / "a.md").unlink()
            await idx.sync(mock_embedder)

            # FTS should have fewer entries
            async with idx._db.execute("SELECT COUNT(*) FROM chunks_fts") as cur:
                row = await cur.fetchone()
                fts_count_after = row[0] if row else 0
            assert fts_count_after < fts_count_before

            # Only b.md chunks remain in FTS
            async with idx._db.execute("SELECT DISTINCT path FROM chunks_fts") as cur:
                paths = [r[0] async for r in cur]
            assert len(paths) == 1
            assert paths[0].endswith("b.md")
        finally:
            await idx.close()

    async def test_sync_deletion_is_atomic(self, memory_dir: Path, index_path: Path):
        """File deletion during sync should be atomic (all-or-nothing per file)."""
        mock_embedder = Mock(spec=EmbeddingClient)
        mock_embedder.embed.return_value = [0.1] * 768

        idx = MemoryIndex(memory_dir=str(memory_dir), index_path=str(index_path))
        await idx.initialize()
        try:
            await idx.sync(mock_embedder)

            # Delete both files
            (memory_dir / "a.md").unlink()
            (memory_dir / "b.md").unlink()

            await idx.sync(mock_embedder)

            # All data should be gone
            assert await idx.chunk_count() == 0
            assert idx._db is not None
            async with idx._db.execute("SELECT COUNT(*) FROM chunks_fts") as cur:
                row = await cur.fetchone()
                assert row is not None and row[0] == 0
            async with idx._db.execute("SELECT COUNT(*) FROM files") as cur:
                row = await cur.fetchone()
                assert row is not None and row[0] == 0
        finally:
            await idx.close()
