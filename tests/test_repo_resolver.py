"""Tests for repo_resolver module."""

import subprocess
from unittest.mock import MagicMock, patch

from orchestrator.config import RepoInfo
from orchestrator.repo_resolver import RepoResolver


class TestEnsureRepos:
    @patch("orchestrator.repo_resolver.subprocess.run")
    def test_clone_when_not_exists(self, mock_run, tmp_path) -> None:
        repo = RepoInfo(url="https://github.com/test/repo.git", path=str(tmp_path / "new-repo"))

        RepoResolver.ensure_repos([repo])

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert "clone" in args[0][0]

    @patch("orchestrator.repo_resolver.subprocess.run")
    def test_pull_when_exists(self, mock_run, tmp_path) -> None:
        repo_path = tmp_path / "existing-repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        repo = RepoInfo(url="https://github.com/test/repo.git", path=str(repo_path))

        RepoResolver.ensure_repos([repo])

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert "pull" in args[0][0]

    @patch("orchestrator.repo_resolver.subprocess.run")
    def test_pull_failure_does_not_reclone(self, mock_run, tmp_path) -> None:
        """Pull failure should NOT destructively re-clone — other worktrees depend on the repo."""
        repo_path = tmp_path / "existing-repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        mock_run.side_effect = subprocess.CalledProcessError(1, "git pull")

        repo = RepoInfo(url="https://github.com/test/repo.git", path=str(repo_path))

        # Should not raise — pull failure is logged and skipped
        paths = RepoResolver.ensure_repos([repo])

        # Repo path should still be returned (not deleted)
        assert paths == [repo_path]
        assert repo_path.exists()

    @patch("orchestrator.repo_resolver.subprocess.run")
    def test_clone_cleans_corrupted_dir_without_git(self, mock_run, tmp_path) -> None:
        """Dir exists but has no .git — should be removed before clone."""
        repo_path = tmp_path / "corrupted-repo"
        repo_path.mkdir()
        (repo_path / "stale-file.txt").write_text("leftover")

        repo = RepoInfo(url="https://github.com/test/repo.git", path=str(repo_path))
        RepoResolver.ensure_repos([repo])

        # Should have called clone (not pull)
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert "clone" in args[0][0]

    @patch("orchestrator.repo_resolver.subprocess.run")
    def test_clone_removes_corrupted_dir_before_clone(self, mock_run, tmp_path) -> None:
        """Corrupted dir (no .git, non-empty) must be removed before git clone."""
        repo_path = tmp_path / "corrupted"
        repo_path.mkdir()
        (repo_path / "junk").write_text("x")

        dir_existed_during_clone: list[bool] = []

        def capture_run(cmd, *args, **kwargs):
            if "clone" in cmd:
                dir_existed_during_clone.append(repo_path.exists())
            return MagicMock(returncode=0)

        mock_run.side_effect = capture_run

        repo = RepoInfo(url="https://github.com/test/repo.git", path=str(repo_path))
        RepoResolver.ensure_repos([repo])

        assert len(dir_existed_during_clone) == 1
        # Dir must NOT exist when git clone runs (it was removed)
        assert dir_existed_during_clone[0] is False


class TestRepoLocking:
    """Test per-repo locking in RepoResolver."""

    def test_same_repo_gets_same_lock(self, tmp_path) -> None:
        lock1 = RepoResolver._get_repo_lock(tmp_path / "backend")
        lock2 = RepoResolver._get_repo_lock(tmp_path / "backend")
        assert lock1 is lock2

    def test_different_repos_get_different_locks(self, tmp_path) -> None:
        lock1 = RepoResolver._get_repo_lock(tmp_path / "backend")
        lock2 = RepoResolver._get_repo_lock(tmp_path / "frontend")
        assert lock1 is not lock2
