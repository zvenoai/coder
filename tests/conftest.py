"""Shared pytest fixtures for all tests."""

from __future__ import annotations

import sys
import typing
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestrator.config import Config, ReposConfig
from orchestrator.tracker_client import TrackerClient


@pytest.fixture(autouse=True)
def mock_sdk(monkeypatch):
    """Mock the claude_agent_sdk module for all tests.

    This fixture is automatically applied to all tests (autouse=True).
    It mocks the SDK to avoid actual API calls and simplify testing.
    """
    mock_module = MagicMock()

    class _MockBase:
        """Base for mock SDK types that accept any kwargs."""

        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _AssistantMessage(_MockBase):
        pass

    class _ResultMessage(_MockBase):
        pass

    class _TextBlock(_MockBase):
        text: str = ""

    class _ThinkingBlock(_MockBase):
        thinking: str = ""

    class _ToolUseBlock(_MockBase):
        name: str = ""
        input: typing.ClassVar[dict] = {}

    mock_module.AssistantMessage = _AssistantMessage
    mock_module.ResultMessage = _ResultMessage
    mock_module.TextBlock = _TextBlock
    mock_module.ThinkingBlock = _ThinkingBlock
    mock_module.ToolUseBlock = _ToolUseBlock
    mock_module.ClaudeAgentOptions = MagicMock()
    mock_module.ClaudeSDKClient = MagicMock()
    mock_module.HookMatcher = MagicMock()

    def mock_tool(name, desc, schema):
        def wrapper(fn):
            fn._tool_name = name
            fn._tool_desc = desc
            fn._tool_schema = schema
            return fn

        return wrapper

    mock_module.tool = mock_tool
    mock_module.create_sdk_mcp_server = MagicMock(return_value="mock_server")

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mock_module)

    # Clear cached orchestrator modules that import SDK
    sdk_modules = {
        "orchestrator.agent_runner",
        "orchestrator.compaction",
        "orchestrator.llm_utils",
        "orchestrator.tracker_tools",
        "orchestrator.workspace_tools",
        "orchestrator.supervisor_tools",
        "orchestrator.orchestrator_tools",
        "orchestrator.supervisor",
        "orchestrator.supervisor_chat",
        "orchestrator.supervisor_memory",
        "orchestrator.comm_tools",
        "orchestrator.main",
    }
    for mod_name in list(sys.modules):
        if mod_name in sdk_modules or any(mod_name.startswith(m) for m in sdk_modules):
            sys.modules.pop(mod_name, None)

    yield mock_module


@pytest.fixture
def tracker_client() -> TrackerClient:
    """Create a TrackerClient instance for testing."""
    return TrackerClient(token="test-token", org_id="test-org")


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Create a test configuration with temporary directories."""
    return Config(
        tracker_token="test-token",
        tracker_org_id="test-org",
        tracker_queue="TEST",
        tracker_tag="test-tag",
        workspace_dir=str(tmp_path / "workspace"),
        worktree_base_dir=str(tmp_path / "worktrees"),
        repos_config=ReposConfig(),
        github_token="test-github-token",
        poll_interval_seconds=60,
        max_concurrent_agents=2,
        review_check_delay_seconds=30,
        needs_info_check_delay_seconds=30,
        agent_permission_mode="default",
        agent_max_budget_usd="10.0",
    )


@pytest.fixture
def mock_tracker():
    """Create a mock TrackerClient for testing."""
    tracker = MagicMock(spec=TrackerClient)
    tracker.search.return_value = []
    tracker.get_comments.return_value = []
    tracker.get_transitions.return_value = []
    return tracker
