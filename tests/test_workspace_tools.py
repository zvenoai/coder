"""Tests for workspace_tools module."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.config import RepoInfo
from orchestrator.workspace import WorktreeInfo


def _make_repos() -> list[RepoInfo]:
    return [
        RepoInfo(url="https://github.com/org/backend.git", path="/workspace/backend", description="Go backend"),
        RepoInfo(url="https://github.com/org/frontend.git", path="/workspace/frontend", description="React frontend"),
    ]


class TestWorkspaceState:
    def test_empty_state(self) -> None:
        from orchestrator.workspace_tools import WorkspaceState

        state = WorkspaceState(issue_key="QR-1")
        assert state.repo_paths == []
        assert state.created_worktrees == []

    def test_repo_paths_single(self) -> None:
        from orchestrator.workspace_tools import WorkspaceState

        wt = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        state = WorkspaceState(issue_key="QR-1", created_worktrees=[wt])
        assert state.repo_paths == [Path("/ws/backend")]

    def test_repo_paths_multiple(self) -> None:
        from orchestrator.workspace_tools import WorkspaceState

        wt1 = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        wt2 = WorktreeInfo(path=Path("/wt/QR-1/frontend"), branch="ai/QR-1", repo_path=Path("/ws/frontend"))
        state = WorkspaceState(issue_key="QR-1", created_worktrees=[wt1, wt2])
        assert state.repo_paths == [Path("/ws/backend"), Path("/ws/frontend")]


class TestListAvailableRepos:
    async def test_returns_catalog(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        state = WorkspaceState(issue_key="QR-1")
        repos = _make_repos()
        server = build_workspace_server(MagicMock(), MagicMock(), repos, state)

        # Find the list_repos tool function
        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        list_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "list_available_repos")

        result = await list_fn({})
        text = result["content"][0]["text"]
        assert "backend" in text
        assert "frontend" in text
        assert "Go backend" in text
        assert "React frontend" in text

    async def test_shows_active_worktrees(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        wt = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        state = WorkspaceState(issue_key="QR-1", created_worktrees=[wt])
        repos = _make_repos()
        build_workspace_server(MagicMock(), MagicMock(), repos, state)

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        list_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "list_available_repos")

        result = await list_fn({})
        text = result["content"][0]["text"]
        assert "[ACTIVE]" in text


class TestRequestWorktree:
    async def test_successful_request(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        state = WorkspaceState(issue_key="QR-1")
        repos = _make_repos()
        mock_resolver = MagicMock()
        mock_resolver.ensure_repos.return_value = [Path("/ws/backend")]
        mock_workspace = MagicMock()
        wt = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        mock_workspace.create_worktree.return_value = wt

        build_workspace_server(mock_resolver, mock_workspace, repos, state)

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        result = await req_fn({"repo_name": "backend"})
        text = result["content"][0]["text"]
        assert "Worktree created" in text
        assert "/wt/QR-1/backend" in text
        assert "ai/QR-1" in text
        assert len(state.created_worktrees) == 1

    async def test_idempotent_request(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        wt = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        state = WorkspaceState(issue_key="QR-1", created_worktrees=[wt])
        repos = _make_repos()
        mock_resolver = MagicMock()
        mock_workspace = MagicMock()

        build_workspace_server(mock_resolver, mock_workspace, repos, state)

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        result = await req_fn({"repo_name": "backend"})
        text = result["content"][0]["text"]
        assert "already active" in text
        # Should NOT have called ensure_repos or create_worktree
        mock_resolver.ensure_repos.assert_not_called()
        mock_workspace.create_worktree.assert_not_called()
        # State should still have only one worktree
        assert len(state.created_worktrees) == 1

    async def test_unknown_repo(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        state = WorkspaceState(issue_key="QR-1")
        repos = _make_repos()

        build_workspace_server(MagicMock(), MagicMock(), repos, state)

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        result = await req_fn({"repo_name": "nonexistent"})
        assert result.get("isError") is True
        text = result["content"][0]["text"]
        assert "Unknown repository" in text

    async def test_blocking_calls_run_in_thread(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        state = WorkspaceState(issue_key="QR-1")
        repos = _make_repos()
        mock_resolver = MagicMock()
        mock_resolver.ensure_repos.return_value = [Path("/ws/backend")]
        mock_workspace = MagicMock()
        wt = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        mock_workspace.create_worktree.return_value = wt

        build_workspace_server(mock_resolver, mock_workspace, repos, state)

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.side_effect = [
                [Path("/ws/backend")],  # ensure_repos result
                wt,  # create_worktree result
                None,  # _setup_npmrc result (returns None)
            ]
            await req_fn({"repo_name": "backend"})

        assert mock_to_thread.call_count == 3
        # First call: ensure_repos
        assert mock_to_thread.call_args_list[0][0][0] == mock_resolver.ensure_repos
        # Second call: create_worktree
        assert mock_to_thread.call_args_list[1][0][0] == mock_workspace.create_worktree
        # Third call: _setup_npmrc
        from orchestrator.workspace_tools import _setup_npmrc

        assert mock_to_thread.call_args_list[2][0][0] == _setup_npmrc

    async def test_response_includes_cd_instruction(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        state = WorkspaceState(issue_key="QR-1")
        repos = _make_repos()
        mock_resolver = MagicMock()
        mock_resolver.ensure_repos.return_value = [Path("/ws/backend")]
        mock_workspace = MagicMock()
        wt = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        mock_workspace.create_worktree.return_value = wt

        build_workspace_server(mock_resolver, mock_workspace, repos, state)

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        result = await req_fn({"repo_name": "backend"})
        text = result["content"][0]["text"]
        assert "cd /wt/QR-1/backend" in text

    async def test_idempotent_response_includes_cd_instruction(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        wt = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        state = WorkspaceState(issue_key="QR-1", created_worktrees=[wt])
        repos = _make_repos()

        build_workspace_server(MagicMock(), MagicMock(), repos, state)

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        result = await req_fn({"repo_name": "backend"})
        text = result["content"][0]["text"]
        assert "cd /wt/QR-1/backend" in text

    async def test_ensure_repos_called(self, mock_sdk) -> None:
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        state = WorkspaceState(issue_key="QR-1")
        repos = _make_repos()
        mock_resolver = MagicMock()
        mock_resolver.ensure_repos.return_value = [Path("/ws/backend")]
        mock_workspace = MagicMock()
        wt = WorktreeInfo(path=Path("/wt/QR-1/backend"), branch="ai/QR-1", repo_path=Path("/ws/backend"))
        mock_workspace.create_worktree.return_value = wt

        build_workspace_server(mock_resolver, mock_workspace, repos, state)

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        await req_fn({"repo_name": "backend"})

        # Verify ensure_repos was called with just the one repo
        mock_resolver.ensure_repos.assert_called_once()
        called_repos = mock_resolver.ensure_repos.call_args[0][0]
        assert len(called_repos) == 1
        assert called_repos[0].url == "https://github.com/org/backend.git"

        # Verify create_worktree was called
        mock_workspace.create_worktree.assert_called_once_with(Path("/ws/backend"), "QR-1")


class TestSetupNpmrc:
    """Tests for the _setup_npmrc helper function."""

    def test_creates_npmrc_with_correct_content(self, tmp_path: Path) -> None:
        """When github_token provided and @zvenoai dependency exists, .npmrc should be created."""
        from orchestrator.workspace_tools import _setup_npmrc

        # Create a package.json with GitHub Package Registry dependency
        package_json = tmp_path / "package.json"
        package_json.write_text(
            json.dumps({"name": "test-app", "dependencies": {"@zvenoai/api-contract-frontend": "^1.0.0"}})
        )

        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert npmrc.exists(), ".npmrc should be created"

        content = npmrc.read_text()
        assert "@zvenoai:registry=https://npm.pkg.github.com" in content
        assert "//npm.pkg.github.com/:_authToken=test-github-token" in content

    def test_skips_when_no_token(self, tmp_path: Path) -> None:
        """When github_token is empty, .npmrc should not be created."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text(json.dumps({"dependencies": {"@zvenoai/api-contract-frontend": "^1.0.0"}}))

        _setup_npmrc(tmp_path, "")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created without token"

    def test_skips_when_no_package_json(self, tmp_path: Path) -> None:
        """When package.json doesn't exist, .npmrc should not be created."""
        from orchestrator.workspace_tools import _setup_npmrc

        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created without package.json"

    def test_skips_when_no_github_packages_dependency(self, tmp_path: Path) -> None:
        """When no @zvenoai dependencies, .npmrc should not be created."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))

        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created without @zvenoai dependencies"

    def test_checks_dev_dependencies_too(self, tmp_path: Path) -> None:
        """Should create .npmrc if @zvenoai package is in devDependencies."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text(
            json.dumps(
                {"dependencies": {"react": "^18.0.0"}, "devDependencies": {"@zvenoai/api-contract-frontend": "^1.0.0"}}
            )
        )

        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert npmrc.exists(), ".npmrc should be created for devDependencies too"

    def test_handles_malformed_package_json(self, tmp_path: Path) -> None:
        """Should not crash on malformed package.json."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text("not valid json {")

        # Should not raise
        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists()

    def test_handles_package_json_with_array(self, tmp_path: Path) -> None:
        """Should not crash when package.json is a valid JSON array instead of object."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text("[]")

        # Should not raise AttributeError
        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created for invalid package.json structure"

    def test_handles_package_json_with_null(self, tmp_path: Path) -> None:
        """Should not crash when package.json contains null."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text("null")

        # Should not raise AttributeError
        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created for null package.json"

    def test_handles_package_json_with_number(self, tmp_path: Path) -> None:
        """Should not crash when package.json is a number."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text("123")

        # Should not raise AttributeError
        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created for non-object package.json"

    def test_handles_dependencies_as_string(self, tmp_path: Path) -> None:
        """Should not crash when dependencies is a string instead of an object."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text('{"dependencies": "invalid"}')

        # Should not raise TypeError from unpacking
        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created when dependencies is not a dict"

    def test_handles_dependencies_as_list(self, tmp_path: Path) -> None:
        """Should not crash when dependencies is a list instead of an object."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text('{"dependencies": ["@zvenoai/api-contract-frontend"]}')

        # Should not raise TypeError from unpacking
        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created when dependencies is not a dict"

    def test_handles_dev_dependencies_as_number(self, tmp_path: Path) -> None:
        """Should not crash when devDependencies is a number instead of an object."""
        from orchestrator.workspace_tools import _setup_npmrc

        package_json = tmp_path / "package.json"
        package_json.write_text('{"devDependencies": 42}')

        # Should not raise TypeError from unpacking
        _setup_npmrc(tmp_path, "test-github-token")

        npmrc = tmp_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created when devDependencies is not a dict"


class TestRequestWorktreeWithNpmrc:
    """Integration tests for .npmrc creation during worktree request."""

    async def test_creates_npmrc_for_web_repo(self, mock_sdk, tmp_path: Path) -> None:
        """When requesting web repo worktree, .npmrc should be created if it has @zvenoai deps."""
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        # Create a realistic worktree directory with package.json
        worktree_path = tmp_path / "web"
        worktree_path.mkdir()
        package_json = worktree_path / "package.json"
        package_json.write_text(
            json.dumps({"name": "web", "dependencies": {"@zvenoai/api-contract-frontend": "^1.0.0"}})
        )

        state = WorkspaceState(issue_key="QR-145")
        repos = [
            RepoInfo(
                url="https://github.com/zvenoai/web.git", path="/workspace/web", description="Frontend (React/Next.js)"
            )
        ]

        mock_resolver = MagicMock()
        mock_resolver.ensure_repos.return_value = [Path("/workspace/web")]
        mock_workspace = MagicMock()
        wt = WorktreeInfo(path=worktree_path, branch="ai/QR-145", repo_path=Path("/workspace/web"))
        mock_workspace.create_worktree.return_value = wt

        build_workspace_server(mock_resolver, mock_workspace, repos, state, github_token="test-token-123")

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        await req_fn({"repo_name": "web"})

        # Verify .npmrc was created
        npmrc = worktree_path / ".npmrc"
        assert npmrc.exists(), ".npmrc should be created for web repo"

        content = npmrc.read_text()
        assert "@zvenoai:registry=https://npm.pkg.github.com" in content
        assert "//npm.pkg.github.com/:_authToken=test-token-123" in content

    async def test_no_npmrc_without_github_token(self, mock_sdk, tmp_path: Path) -> None:
        """When github_token is empty, .npmrc should not be created."""
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        worktree_path = tmp_path / "web"
        worktree_path.mkdir()
        package_json = worktree_path / "package.json"
        package_json.write_text(json.dumps({"dependencies": {"@zvenoai/api-contract-frontend": "^1.0.0"}}))

        state = WorkspaceState(issue_key="QR-145")
        repos = [RepoInfo(url="https://github.com/zvenoai/web.git", path="/workspace/web", description="Frontend")]

        mock_resolver = MagicMock()
        mock_resolver.ensure_repos.return_value = [Path("/workspace/web")]
        mock_workspace = MagicMock()
        wt = WorktreeInfo(path=worktree_path, branch="ai/QR-145", repo_path=Path("/workspace/web"))
        mock_workspace.create_worktree.return_value = wt

        # No github_token provided (empty string by default)
        build_workspace_server(mock_resolver, mock_workspace, repos, state, github_token="")

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        await req_fn({"repo_name": "web"})

        npmrc = worktree_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created without token"

    async def test_no_npmrc_for_backend_repo(self, mock_sdk, tmp_path: Path) -> None:
        """Backend repo (no package.json with @zvenoai) should not get .npmrc."""
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        worktree_path = tmp_path / "backend"
        worktree_path.mkdir()
        # Backend has no package.json

        state = WorkspaceState(issue_key="QR-100")
        repos = [
            RepoInfo(url="https://github.com/zvenoai/backend.git", path="/workspace/backend", description="Backend")
        ]

        mock_resolver = MagicMock()
        mock_resolver.ensure_repos.return_value = [Path("/workspace/backend")]
        mock_workspace = MagicMock()
        wt = WorktreeInfo(path=worktree_path, branch="ai/QR-100", repo_path=Path("/workspace/backend"))
        mock_workspace.create_worktree.return_value = wt

        build_workspace_server(mock_resolver, mock_workspace, repos, state, github_token="test-token")

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        await req_fn({"repo_name": "backend"})

        npmrc = worktree_path / ".npmrc"
        assert not npmrc.exists(), ".npmrc should not be created for non-npm repos"

    async def test_npmrc_error_does_not_fail_worktree_creation(self, mock_sdk, tmp_path: Path) -> None:
        """If .npmrc creation fails (e.g., disk full), the worktree should still be created successfully."""
        from orchestrator.workspace_tools import WorkspaceState, build_workspace_server

        worktree_path = tmp_path / "web"
        worktree_path.mkdir()
        package_json = worktree_path / "package.json"
        package_json.write_text(json.dumps({"dependencies": {"@zvenoai/api-contract-frontend": "^1.0.0"}}))

        state = WorkspaceState(issue_key="QR-145")
        repos = [RepoInfo(url="https://github.com/zvenoai/web.git", path="/workspace/web", description="Frontend")]

        mock_resolver = MagicMock()
        mock_resolver.ensure_repos.return_value = [Path("/workspace/web")]
        mock_workspace = MagicMock()
        wt = WorktreeInfo(path=worktree_path, branch="ai/QR-145", repo_path=Path("/workspace/web"))
        mock_workspace.create_worktree.return_value = wt

        build_workspace_server(mock_resolver, mock_workspace, repos, state, github_token="test-token")

        tools = mock_sdk.create_sdk_mcp_server.call_args.kwargs["tools"]
        req_fn = next(f for f in tools if getattr(f, "_tool_name", None) == "request_worktree")

        # Make .npmrc write fail (simulate disk full or permission denied)
        with patch("orchestrator.workspace_tools._setup_npmrc", side_effect=OSError("Disk full")):
            result = await req_fn({"repo_name": "web"})

        # Worktree should still be created successfully (no isError)
        assert result.get("isError") is None, "Worktree creation should succeed even if .npmrc fails"
        text = result["content"][0]["text"]
        assert "Worktree created" in text

        # Worktree should be in state (not lost due to exception)
        assert len(state.created_worktrees) == 1
        assert state.created_worktrees[0].path == worktree_path
