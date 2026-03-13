"""Git worktree management for task isolation."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorktreeInfo:
    """Info about a created git worktree."""

    path: Path
    branch: str
    repo_path: Path


class WorkspaceManager:
    """Manages git worktrees for per-task isolation."""

    def __init__(self, worktree_base_dir: str = "/workspace/worktrees") -> None:
        self._base = Path(worktree_base_dir)
        self._repo_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _get_repo_lock(self, repo_path: Path) -> threading.Lock:
        """Get or create a lock for the given repo path."""
        key = str(repo_path.resolve())
        with self._locks_lock:
            if key not in self._repo_locks:
                self._repo_locks[key] = threading.Lock()
            return self._repo_locks[key]

    def create_worktree(
        self,
        repo_path: Path,
        issue_key: str,
        base_branch: str = "main",
    ) -> WorktreeInfo:
        """Create a git worktree for the given issue.

        Creates branch ai/{issue_key} based on origin/{base_branch}.

        If a worktree already exists with unpushed commits or uncommitted changes,
        it is preserved and reused to avoid losing agent progress across retries.
        """
        branch = f"ai/{issue_key}"
        worktree_path = self._base / issue_key / repo_path.name
        lock = self._get_repo_lock(repo_path)

        with lock:
            # Fetch latest
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            # Check if worktree already exists with local work
            if worktree_path.exists():
                has_local_work = self._has_local_work(worktree_path, branch, base_branch)

                if has_local_work:
                    logger.info(
                        "Reusing existing worktree with local work for %s at %s",
                        issue_key,
                        worktree_path,
                    )
                    return WorktreeInfo(path=worktree_path, branch=branch, repo_path=repo_path)

                # No local work — safe to recreate for fresh base
                logger.info(
                    "Worktree for %s exists but has no local work, recreating",
                    issue_key,
                )
                self._remove_single_worktree(repo_path, worktree_path)
                # Delete the branch too so we start fresh from base_branch
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    check=False,
                    cwd=repo_path,
                    capture_output=True,
                )

            # Check if local branch exists (may have commits not in a worktree)
            local_branch_result = subprocess.run(
                ["git", "branch", "--list", branch],
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            local_branch_exists = bool(local_branch_result.stdout.strip())

            if not local_branch_exists:
                # Safe to delete any stale ref (belt-and-suspenders)
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    check=False,
                    cwd=repo_path,
                    capture_output=True,
                )

            worktree_path.parent.mkdir(parents=True, exist_ok=True)

            if local_branch_exists:
                # Reuse existing local branch
                subprocess.run(
                    ["git", "worktree", "add", str(worktree_path), branch],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                )
            else:
                # Check if branch exists on remote
                result = subprocess.run(
                    ["git", "ls-remote", "--heads", "origin", branch],
                    check=False,
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                )
                remote_exists = f"refs/heads/{branch}" in (result.stdout or "")

                if remote_exists:
                    # Track existing remote branch
                    subprocess.run(
                        [
                            "git",
                            "worktree",
                            "add",
                            "--track",
                            "-b",
                            branch,
                            str(worktree_path),
                            f"origin/{branch}",
                        ],
                        cwd=repo_path,
                        check=True,
                        capture_output=True,
                    )
                else:
                    # Create new branch from base
                    subprocess.run(
                        [
                            "git",
                            "worktree",
                            "add",
                            "-b",
                            branch,
                            str(worktree_path),
                            f"origin/{base_branch}",
                        ],
                        cwd=repo_path,
                        check=True,
                        capture_output=True,
                    )

            logger.info(
                "Created worktree for %s: %s (branch %s)",
                issue_key,
                worktree_path,
                branch,
            )
            return WorktreeInfo(path=worktree_path, branch=branch, repo_path=repo_path)

    def remove_worktree(self, repo_path: Path, worktree_path: Path) -> None:
        """Remove a git worktree, with fallback to manual cleanup."""
        self._remove_single_worktree(repo_path, worktree_path)

    def cleanup_stale(
        self,
        stale_keys: set[str],
        all_repo_paths: list[Path],
    ) -> int:
        """Remove stale worktree directories and prune git refs.

        Args:
            stale_keys: Issue keys whose worktrees should be removed.
            all_repo_paths: Repo paths to run ``git worktree prune`` on.

        Returns:
            Number of worktree directories successfully removed.

        Uses ``shutil.rmtree`` + a single ``git worktree prune`` per
        repo instead of individual ``git worktree remove`` calls.
        This is a batch optimization for startup cleanup where many
        stale worktrees may exist simultaneously.
        """
        removed = 0
        for key in sorted(stale_keys):
            issue_dir = self._base / key
            if not issue_dir.exists():
                continue
            try:
                shutil.rmtree(issue_dir)
                removed += 1
                logger.info("Removed stale worktree dir: %s", issue_dir)
            except OSError:
                logger.warning(
                    "Failed to remove stale worktree dir: %s",
                    issue_dir,
                    exc_info=True,
                )

        for repo_path in all_repo_paths:
            if not repo_path.exists():
                continue
            lock = self._get_repo_lock(repo_path)
            with lock:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=repo_path,
                    check=False,
                    capture_output=True,
                )

        return removed

    def cleanup_issue(self, issue_key: str, repo_paths: list[Path]) -> None:
        """Remove all worktrees for an issue across all repos."""
        issue_dir = self._base / issue_key

        for repo_path in repo_paths:
            worktree_path = issue_dir / repo_path.name
            if worktree_path.exists():
                self._remove_single_worktree(repo_path, worktree_path)

        # Remove the issue directory if empty
        if issue_dir.exists():
            try:
                issue_dir.rmdir()
            except OSError:
                pass

    @staticmethod
    def _has_local_work(worktree_path: Path, branch: str, base_branch: str = "main") -> bool:
        """Check if a worktree has unpushed commits or uncommitted changes.

        Returns True if the worktree contains any local work that hasn't been
        pushed to the remote, including:
        - Unpushed commits (ahead of origin/{branch})
        - Local-only commits (when remote branch doesn't exist yet)
        - Uncommitted changes (staged or unstaged files)
        """
        # Check for unpushed commits
        log_result = subprocess.run(
            ["git", "log", "--oneline", f"origin/{branch}..HEAD"],
            cwd=worktree_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if log_result.returncode == 0 and log_result.stdout.strip():
            return True

        # If remote branch doesn't exist, git log above fails.
        # Check if there are ANY commits beyond the base branch
        if log_result.returncode != 0:
            fallback_log_result = subprocess.run(
                ["git", "log", "--oneline", f"origin/{base_branch}..HEAD"],
                cwd=worktree_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if fallback_log_result.returncode == 0 and fallback_log_result.stdout.strip():
                return True

        # Check for uncommitted changes (staged or unstaged)
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            check=False,
            capture_output=True,
            text=True,
        )
        return status_result.returncode == 0 and bool(status_result.stdout.strip())

    @staticmethod
    def _remove_single_worktree(repo_path: Path, worktree_path: Path) -> None:
        """Remove a single worktree with fallback."""
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            logger.warning(
                "git worktree remove failed for %s, falling back to manual cleanup",
                worktree_path,
            )
            if worktree_path.exists():
                shutil.rmtree(worktree_path)
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=repo_path,
                check=False,
                capture_output=True,
            )
