"""Tests for SupervisorRunner."""

from unittest.mock import MagicMock, patch

from orchestrator.config import Config, ReposConfig


def make_config(**overrides) -> Config:
    """Create a test Config with supervisor defaults."""
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(),
        supervisor_enabled=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestSupervisorInit:
    @patch("orchestrator.supervisor.create_memory_system")
    def test_memory_system_initialized_when_available(self, mock_create_memory: MagicMock) -> None:
        """When create_memory_system returns a tuple, memory_index and embedder are set."""
        from orchestrator.supervisor import SupervisorRunner
        from orchestrator.supervisor_memory import EmbeddingClient, MemoryIndex

        mock_memory_index = MagicMock(spec=MemoryIndex)
        mock_embedder = MagicMock(spec=EmbeddingClient)
        mock_create_memory.return_value = (mock_memory_index, mock_embedder)

        runner = SupervisorRunner(config=make_config())

        assert runner.memory_index is mock_memory_index
        assert runner.embedder is mock_embedder

    @patch("orchestrator.supervisor.create_memory_system")
    def test_memory_system_disabled_when_returns_none(self, mock_create_memory: MagicMock) -> None:
        """When create_memory_system returns None, memory_index and embedder are None."""
        from orchestrator.supervisor import SupervisorRunner

        mock_create_memory.return_value = None

        runner = SupervisorRunner(config=make_config())

        assert runner.memory_index is None
        assert runner.embedder is None

    @patch("orchestrator.supervisor.create_memory_system")
    def test_create_memory_system_called_with_config_values(self, mock_create_memory: MagicMock) -> None:
        """create_memory_system should be called with config values."""
        from orchestrator.supervisor import SupervisorRunner

        mock_create_memory.return_value = None

        config = make_config(
            embedding_api_key="test-key",
            embedding_base_url="https://api.example.com/v1",
            supervisor_memory_dir="/test/memory",
            supervisor_memory_index_path="/test/memory/.index.sqlite",
        )

        SupervisorRunner(config=config)

        mock_create_memory.assert_called_once_with(
            embedding_api_key="test-key",
            memory_dir="/test/memory",
            index_path="/test/memory/.index.sqlite",
            embedding_base_url="https://api.example.com/v1",
        )


class TestSupervisorProperties:
    @patch("orchestrator.supervisor.create_memory_system", return_value=None)
    def test_is_running_always_false(self, _mock: MagicMock) -> None:
        from orchestrator.supervisor import SupervisorRunner

        runner = SupervisorRunner(config=make_config())
        assert runner.is_running is False

    @patch("orchestrator.supervisor.create_memory_system", return_value=None)
    def test_last_run_at_always_none(self, _mock: MagicMock) -> None:
        from orchestrator.supervisor import SupervisorRunner

        runner = SupervisorRunner(config=make_config())
        assert runner.last_run_at is None

    @patch("orchestrator.supervisor.create_memory_system", return_value=None)
    def test_queue_size_always_zero(self, _mock: MagicMock) -> None:
        from orchestrator.supervisor import SupervisorRunner

        runner = SupervisorRunner(config=make_config())
        assert runner.queue_size == 0


class TestDisableStorage:
    @patch("orchestrator.supervisor.create_memory_system", return_value=None)
    def test_disable_storage_sets_storage_to_none(self, _mock: MagicMock) -> None:
        from orchestrator.supervisor import SupervisorRunner

        runner = SupervisorRunner(config=make_config(), storage=MagicMock())

        assert runner._storage is not None
        runner.disable_storage()
        assert runner._storage is None
