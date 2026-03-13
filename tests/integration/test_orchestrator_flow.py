"""Integration tests for full orchestrator flows.

These tests use real workspace management and repo resolution,
mocking only external APIs (Tracker, GitHub, SDK).
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.integration
class TestWorkspaceIntegration:
    """Integration tests for workspace and repo resolution."""

    def test_workspace_manager_initialization(self, tmp_path):
        """Test that workspace manager initializes correctly."""
        from orchestrator.workspace import WorkspaceManager

        worktree_base = str(tmp_path / "worktrees")
        workspace = WorkspaceManager(worktree_base)

        # Verify worktree base dir is set
        assert workspace._base == Path(worktree_base)

    @patch("orchestrator.workspace.subprocess.run")
    def test_workspace_manager_creates_worktree(self, mock_subprocess, tmp_path):
        """Test that workspace manager can create worktrees."""
        from orchestrator.workspace import WorkspaceManager

        worktree_base = str(tmp_path / "worktrees")
        workspace = WorkspaceManager(worktree_base)

        # Mock successful git worktree add
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Create repo directory
        repo_path = tmp_path / "repos" / "test-repo"
        repo_path.mkdir(parents=True)

        # Create worktree for issue TEST-1 (creates branch ai/TEST-1)
        worktree = workspace.create_worktree(repo_path, "TEST-1")

        # Verify worktree was created with ai/ prefix
        assert worktree is not None
        assert worktree.branch == "ai/TEST-1"
        assert worktree.repo_path == repo_path


@pytest.mark.integration
class TestAgentRunnerIntegration:
    """Integration tests for agent runner with mocked SDK."""

    def test_agent_runner_initialization(self, test_config, mock_sdk):
        """Test that agent runner initializes with correct config."""
        from orchestrator.agent_runner import AgentRunner
        from orchestrator.tracker_client import TrackerClient

        tracker = MagicMock(spec=TrackerClient)
        runner = AgentRunner(test_config, tracker)

        # Verify runner is initialized
        assert runner is not None


@pytest.mark.integration
class TestEventBusIntegration:
    """Integration tests for event bus pub/sub."""

    async def test_event_bus_publish_subscribe(self):
        """Test that event bus correctly delivers events to subscribers."""
        from orchestrator.event_bus import Event, EventBus

        bus = EventBus()
        received_events = []

        # Subscribe to task events
        queue = bus.subscribe_task("TEST-1")

        # Publish event
        event = Event(type="agent_output", task_key="TEST-1", data={"text": "hello"})
        await bus.publish(event)

        # Check event was received
        try:
            received = queue.get_nowait()
            received_events.append(received)
        except asyncio.QueueEmpty:
            pass

        assert len(received_events) == 1
        assert received_events[0].task_key == "TEST-1"
        assert received_events[0].data["text"] == "hello"

        # Unsubscribe
        bus.unsubscribe_task("TEST-1", queue)

    async def test_event_bus_global_subscription(self):
        """Test that global subscribers receive all events."""
        from orchestrator.event_bus import Event, EventBus

        bus = EventBus()
        received_events = []

        # Subscribe globally
        queue = bus.subscribe_global()

        # Publish events for different tasks
        await bus.publish(Event(type="task_started", task_key="TEST-1", data={}))
        await bus.publish(Event(type="task_started", task_key="TEST-2", data={}))

        # Check all events were received
        while not queue.empty():
            received_events.append(queue.get_nowait())

        assert len(received_events) == 2
        assert received_events[0].task_key == "TEST-1"
        assert received_events[1].task_key == "TEST-2"

        # Unsubscribe
        bus.unsubscribe_global(queue)


@pytest.mark.integration
class TestRecoveryManagerIntegration:
    """Integration tests for recovery manager error classification."""

    def test_recovery_manager_tracks_state(self):
        """Test that recovery manager correctly tracks recovery state per issue."""
        from orchestrator.recovery import RecoveryManager

        recovery = RecoveryManager()

        # Record failures for an issue
        state1 = recovery.record_failure("TEST-1", "Connection timeout")
        assert state1.issue_key == "TEST-1"
        assert len(state1.attempts) == 1

        state2 = recovery.record_failure("TEST-1", "Another timeout")
        assert len(state2.attempts) == 2
        assert state1 is state2  # Same state object

    def test_error_classification(self):
        """Test error classification function."""
        from orchestrator.recovery import ErrorCategory, classify_error

        # Test specific error categories
        assert classify_error("Connection timeout") == ErrorCategory.TIMEOUT
        assert classify_error("Rate limit exceeded") == ErrorCategory.RATE_LIMIT
        assert classify_error("HTTP 500") == ErrorCategory.TRANSIENT
        assert classify_error("Too many concurrent") == ErrorCategory.CONCURRENCY
        assert classify_error("Unauthorized access") == ErrorCategory.AUTH
        assert classify_error("Budget exceeded") == ErrorCategory.BUDGET

        # Test permanent errors (anything that doesn't match specific patterns)
        assert classify_error("Invalid API key") == ErrorCategory.PERMANENT
        assert classify_error("Permission denied") == ErrorCategory.PERMANENT

    def test_should_retry_logic(self):
        """Test should_retry logic based on error categories and attempts."""
        from orchestrator.recovery import RecoveryManager

        recovery = RecoveryManager()

        # Record transient error - should retry
        recovery.record_failure("TEST-1", "Connection timeout")
        assert recovery.get_state("TEST-1").should_retry is True

        # Record many transient errors - eventually should not retry (max attempts)
        for _ in range(10):
            recovery.record_failure("TEST-2", "Timeout")

        # After many attempts, should stop retrying
        assert recovery.get_state("TEST-2").should_retry is False

        # Non-retryable categories: AUTH, BUDGET, CLI - should not retry
        recovery.record_failure("TEST-3", "Unauthorized access")
        assert recovery.get_state("TEST-3").should_retry is False

        recovery.record_failure("TEST-4", "Budget exceeded")
        assert recovery.get_state("TEST-4").should_retry is False

        recovery.record_failure("TEST-5", "Command not found")
        assert recovery.get_state("TEST-5").should_retry is False

        # PERMANENT errors ARE retryable (up to MAX_ATTEMPTS)
        recovery.record_failure("TEST-6", "Invalid API key")
        assert recovery.get_state("TEST-6").should_retry is True  # PERMANENT is retryable


@pytest.mark.integration
class TestConfigIntegration:
    """Integration tests for configuration loading."""

    def test_config_fixture_provides_valid_config(self, test_config):
        """Test that test_config fixture provides valid configuration."""
        # Verify all required fields are present
        assert test_config.tracker_token == "test-token"
        assert test_config.tracker_org_id == "test-org"
        assert test_config.tracker_queue == "TEST"
        assert test_config.workspace_dir is not None
        assert test_config.worktree_base_dir is not None

    def test_config_agent_env_property(self, test_config):
        """Test that config agent_env property works."""
        # agent_env is a @property that combines permission_mode, max_turns, max_budget
        env = test_config.agent_env
        assert env is not None
        assert isinstance(env, dict)
