"""Clone/pull git repositories as needed."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import typing
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from orchestrator.config import RepoInfo

logger = logging.getLogger(__name__)


class RepoResolver:
    """Clones/pulls git repos."""

    _repo_locks: typing.ClassVar[dict[str, threading.Lock]] = {}
    _locks_lock: typing.ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _get_repo_lock(cls, repo_path: Path) -> threading.Lock:
        """Get or create a lock for the given repo path."""
        key = str(repo_path.resolve())
        with cls._locks_lock:
            if key not in cls._repo_locks:
                cls._repo_locks[key] = threading.Lock()
            return cls._repo_locks[key]

    @staticmethod
    def _auth_url(url: str) -> str:
        """Inject GITHUB_TOKEN into https GitHub URLs for auth."""
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            return url
        parsed = urlparse(url)
        if parsed.hostname in ("github.com", "www.github.com") and parsed.scheme == "https":
            authed = parsed._replace(netloc=f"{token}@{parsed.hostname}")
            return urlunparse(authed)
        return url

    @classmethod
    def ensure_repos(cls, repos: list[RepoInfo]) -> list[Path]:
        """Clone or pull repos, return local paths."""
        paths: list[Path] = []
        for repo in repos:
            local_path = Path(repo.path)
            lock = cls._get_repo_lock(local_path)
            with lock:
                if local_path.exists() and (local_path / ".git").exists():
                    logger.info("Pulling %s", repo.url)
                    try:
                        subprocess.run(
                            ["git", "pull", "--ff-only"],
                            cwd=local_path,
                            check=True,
                            capture_output=True,
                        )
                    except subprocess.CalledProcessError:
                        logger.warning(
                            "Pull failed for %s, skipping (worktrees may depend on this repo)",
                            repo.url,
                        )
                else:
                    if local_path.exists():
                        logger.warning("Removing corrupted repo dir %s (no .git found)", local_path)
                        shutil.rmtree(local_path)
                    cls._clone_repo(repo.url, local_path)
            paths.append(local_path)
        return paths

    @staticmethod
    def _clone_repo(url: str, local_path: Path) -> None:
        """Clone a repository."""
        clone_url = RepoResolver._auth_url(url)
        logger.info("Cloning %s → %s", url, local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", clone_url, str(local_path)],
            check=True,
            capture_output=True,
        )
