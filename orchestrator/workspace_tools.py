"""MCP workspace tools — lazy worktree creation for agent tasks."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from orchestrator.config import RepoInfo
from orchestrator.repo_resolver import RepoResolver
from orchestrator.workspace import WorkspaceManager, WorktreeInfo

logger = logging.getLogger(__name__)

_GITHUB_PACKAGES_SCOPE = "@zvenoai"
_GITHUB_PACKAGES_REGISTRY = "https://npm.pkg.github.com"


def _needs_github_packages_auth(worktree_path: Path) -> bool:
    """Check if the worktree has a package.json with @zvenoai scoped dependencies."""
    pkg_json = worktree_path / "package.json"
    if not pkg_json.exists():
        return False
    try:
        data = json.loads(pkg_json.read_text())
        all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        return any(name.startswith(_GITHUB_PACKAGES_SCOPE) for name in all_deps)
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return False


def _setup_npmrc(worktree_path: Path, github_token: str) -> None:
    """Create .npmrc for GitHub Package Registry auth if needed.

    Writes .npmrc only when:
    1. github_token is non-empty
    2. worktree contains package.json with @zvenoai scoped dependencies
    """
    if not github_token:
        return
    if not _needs_github_packages_auth(worktree_path):
        return

    npmrc_path = worktree_path / ".npmrc"
    npmrc_content = (
        f"{_GITHUB_PACKAGES_SCOPE}:registry={_GITHUB_PACKAGES_REGISTRY}\n"
        f"//npm.pkg.github.com/:_authToken={github_token}\n"
    )
    npmrc_path.write_text(npmrc_content)
    logger.info("Created .npmrc for GitHub Packages auth in %s", worktree_path)


@dataclass
class WorkspaceState:
    """Tracks lazily-created worktrees for a task."""

    issue_key: str
    created_worktrees: list[WorktreeInfo] = field(default_factory=list)

    @property
    def repo_paths(self) -> list[Path]:
        """Return repo paths for all created worktrees (for cleanup)."""
        return [wt.repo_path for wt in self.created_worktrees]


def build_workspace_server(
    resolver: RepoResolver,
    workspace: WorkspaceManager,
    all_repos: list[RepoInfo],
    state: WorkspaceState,
    github_token: str = "",
) -> Any:
    """Build an MCP server with workspace tools, scoped to a single task."""

    @tool(
        "list_available_repos",
        "List all project repositories with descriptions. Shows which repos already have active worktrees.",
        {},
    )
    async def list_repos(args: dict[str, Any]) -> dict[str, Any]:
        active_names = {wt.repo_path.name for wt in state.created_worktrees}
        lines: list[str] = []
        for repo in all_repos:
            name = Path(repo.path).name
            status = " [ACTIVE]" if name in active_names else ""
            desc = f" — {repo.description}" if repo.description else ""
            lines.append(f"- **{name}**{desc}{status}")
        text = "## Available repositories\n\n" + "\n".join(lines) if lines else "No repositories configured."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "request_worktree",
        "Clone/pull a repository and create a git worktree for it. Pass the repo name (e.g. 'backend'). Returns the worktree path and branch. Idempotent — requesting the same repo again returns the existing worktree.",
        {"repo_name": str},
    )
    async def request_worktree(args: dict[str, Any]) -> dict[str, Any]:
        repo_name = args["repo_name"]

        # Check if already created (idempotent)
        for wt in state.created_worktrees:
            if wt.repo_path.name == repo_name:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Worktree already active for **{repo_name}**:\n"
                                f"- Path: `{wt.path}`\n"
                                f"- Branch: `{wt.branch}`\n\n"
                                f"**Important:** Run `cd {wt.path}` before executing any commands."
                            ),
                        }
                    ]
                }

        # Find repo by name
        repo = None
        for r in all_repos:
            if Path(r.path).name == repo_name:
                repo = r
                break

        if repo is None:
            available = ", ".join(Path(r.path).name for r in all_repos)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Unknown repository: '{repo_name}'. Available: {available}",
                    }
                ],
                "isError": True,
            }

        # Ensure repo cloned/pulled
        try:
            paths = await asyncio.to_thread(resolver.ensure_repos, [repo])
        except Exception as e:
            logger.error("Failed to ensure repo %s: %s", repo_name, e)
            return {
                "content": [{"type": "text", "text": f"Failed to clone/pull {repo_name}: {e}"}],
                "isError": True,
            }

        # Create worktree
        try:
            wt = await asyncio.to_thread(workspace.create_worktree, paths[0], state.issue_key)
        except Exception as e:
            logger.error("Failed to create worktree for %s: %s", repo_name, e)
            return {
                "content": [{"type": "text", "text": f"Failed to create worktree for {repo_name}: {e}"}],
                "isError": True,
            }

        state.created_worktrees.append(wt)

        # Setup repo-specific auth (e.g., .npmrc for GitHub Package Registry)
        try:
            await asyncio.to_thread(_setup_npmrc, wt.path, github_token)
        except Exception as e:
            logger.warning("Failed to create .npmrc for %s: %s (worktree still usable)", repo_name, e)

        logger.info("Lazy worktree created for %s/%s: %s", state.issue_key, repo_name, wt.path)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Worktree created for **{repo_name}**:\n"
                        f"- Path: `{wt.path}`\n"
                        f"- Branch: `{wt.branch}`\n\n"
                        f"**Important:** Run `cd {wt.path}` before executing any commands."
                    ),
                }
            ]
        }

    return create_sdk_mcp_server(
        name="workspace",
        version="1.0.0",
        tools=[list_repos, request_worktree],
    )
