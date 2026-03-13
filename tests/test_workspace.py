"""Tests for workspace module."""

import threading
from unittest.mock import MagicMock, patch

from orchestrator.workspace import WorkspaceManager


def _ls_remote_result(stdout: str = "") -> MagicMock:
    """Helper to create a CompletedProcess-like mock for ls-remote."""
    result = MagicMock()
    result.stdout = stdout
    return result


class TestCreateWorktree:
    @patch("orchestrator.workspace.subprocess.run")
    def test_creates_worktree_new_branch(self, mock_run, tmp_path) -> None:
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        # ls-remote returns empty (no remote branch)
        mock_run.return_value = _ls_remote_result("")

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        info = manager.create_worktree(repo_path, "QR-125")

        assert info.branch == "ai/QR-125"
        assert info.path == tmp_path / "worktrees" / "QR-125" / "my-repo"
        assert info.repo_path == repo_path

        # Should call: fetch, branch --list, branch -D, ls-remote, worktree add
        assert mock_run.call_count == 5
        fetch_call = mock_run.call_args_list[0]
        assert fetch_call[0][0] == ["git", "fetch", "origin"]

        branch_list_call = mock_run.call_args_list[1]
        assert branch_list_call[0][0] == ["git", "branch", "--list", "ai/QR-125"]

        branch_d_call = mock_run.call_args_list[2]
        assert branch_d_call[0][0] == ["git", "branch", "-D", "ai/QR-125"]

        ls_remote_call = mock_run.call_args_list[3]
        assert ls_remote_call[0][0] == ["git", "ls-remote", "--heads", "origin", "ai/QR-125"]

        add_call = mock_run.call_args_list[4]
        assert "worktree" in add_call[0][0]
        assert "add" in add_call[0][0]
        assert "-b" in add_call[0][0]
        assert "ai/QR-125" in add_call[0][0]
        assert "origin/main" in add_call[0][0]

    @patch("orchestrator.workspace.subprocess.run")
    def test_creates_worktree_existing_remote_branch(self, mock_run, tmp_path) -> None:
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        def mock_run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""

            if cmd == ["git", "fetch", "origin"]:
                return result
            if cmd == ["git", "branch", "--list", "ai/QR-125"]:
                # No local branch exists
                return result
            if cmd == ["git", "ls-remote", "--heads", "origin", "ai/QR-125"]:
                # Remote branch exists
                result.stdout = "abc123\trefs/heads/ai/QR-125\n"
                return result
            if cmd[0:2] == ["git", "worktree"] or cmd == ["git", "branch", "-D", "ai/QR-125"]:
                return result

            return result

        mock_run.side_effect = mock_run_side_effect

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        info = manager.create_worktree(repo_path, "QR-125")

        assert info.branch == "ai/QR-125"

        # Should call: fetch, branch --list, branch -D, ls-remote, worktree add (--track)
        assert mock_run.call_count == 5

        add_call = mock_run.call_args_list[4]
        cmd = add_call[0][0]
        assert "--track" in cmd
        assert "origin/ai/QR-125" in cmd

    @patch("orchestrator.workspace.subprocess.run")
    def test_custom_base_branch(self, mock_run, tmp_path) -> None:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        # ls-remote returns empty
        mock_run.return_value = _ls_remote_result("")

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "wt"))
        manager.create_worktree(repo_path, "QR-1", base_branch="develop")

        # Call order: fetch, branch --list, branch -D, ls-remote, worktree add
        add_call = mock_run.call_args_list[4]
        assert "origin/develop" in add_call[0][0]

    @patch("orchestrator.workspace.subprocess.run")
    def test_removes_existing_worktree_before_create(self, mock_run, tmp_path) -> None:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"
        wt_path.mkdir(parents=True)

        def mock_run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""

            if cmd == ["git", "fetch", "origin"]:
                return result
            if cmd == ["git", "log", "--oneline", "origin/ai/QR-1..HEAD"]:
                # No unpushed commits
                return result
            if cmd == ["git", "status", "--porcelain"]:
                # No uncommitted changes
                return result
            if cmd[0:2] == ["git", "worktree"] and "remove" in cmd:
                return result
            if cmd == ["git", "branch", "--list", "ai/QR-1"]:
                # No local branch
                return result
            if cmd == ["git", "branch", "-D", "ai/QR-1"]:
                return result
            if cmd == ["git", "ls-remote", "--heads", "origin", "ai/QR-1"]:
                result.stdout = ""
                return result
            if cmd[0:2] == ["git", "worktree"] and "add" in cmd:
                return result

            return result

        mock_run.side_effect = mock_run_side_effect

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        manager.create_worktree(repo_path, "QR-1")

        # Should call: fetch, git log (check unpushed), git status (check dirty),
        # worktree remove, branch -D (after worktree removal), branch --list, branch -D, ls-remote, worktree add
        assert mock_run.call_count == 9

    @patch("orchestrator.workspace.subprocess.run")
    def test_preserves_worktree_with_unpushed_commits(self, mock_run, tmp_path) -> None:
        """Test that worktree with unpushed commits is preserved and not recreated."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"
        wt_path.mkdir(parents=True)

        def mock_run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""

            if cmd == ["git", "fetch", "origin"]:
                return result
            if cmd == ["git", "log", "--oneline", "origin/ai/QR-1..HEAD"]:
                # Simulate unpushed commits
                result.stdout = "abc123 feat: some work\ndef456 test: add tests\n"
                return result

            return result

        mock_run.side_effect = mock_run_side_effect

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        info = manager.create_worktree(repo_path, "QR-1")

        assert info.branch == "ai/QR-1"
        assert info.path == wt_path
        assert info.repo_path == repo_path

        # Should only call: fetch, git log (to check unpushed)
        assert mock_run.call_count == 2

        # Verify worktree remove was NOT called
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "remove" not in cmd

        # Verify branch -D was NOT called
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "-D" not in cmd

    @patch("orchestrator.workspace.subprocess.run")
    def test_preserves_worktree_with_dirty_working_tree(self, mock_run, tmp_path) -> None:
        """Test that worktree with uncommitted changes is preserved."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"
        wt_path.mkdir(parents=True)

        def mock_run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""

            if cmd == ["git", "fetch", "origin"]:
                return result
            if cmd == ["git", "log", "--oneline", "origin/ai/QR-1..HEAD"]:
                # No unpushed commits
                return result
            if cmd == ["git", "status", "--porcelain"]:
                # Simulate dirty working tree
                result.stdout = " M orchestrator/workspace.py\n?? new_file.py\n"
                return result

            return result

        mock_run.side_effect = mock_run_side_effect

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        info = manager.create_worktree(repo_path, "QR-1")

        assert info.branch == "ai/QR-1"
        assert info.path == wt_path

        # Should call: fetch, git log, git status
        assert mock_run.call_count == 3

        # Verify worktree was NOT removed
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "remove" not in cmd

    @patch("orchestrator.workspace.subprocess.run")
    def test_recreates_worktree_when_fully_synced(self, mock_run, tmp_path) -> None:
        """Test that worktree without local changes is recreated for fresh base."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"
        wt_path.mkdir(parents=True)

        def mock_run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""

            if cmd == ["git", "fetch", "origin"]:
                return result
            if cmd == ["git", "log", "--oneline", "origin/ai/QR-1..HEAD"]:
                # No unpushed commits
                return result
            if cmd == ["git", "status", "--porcelain"]:
                # No uncommitted changes
                return result
            if cmd == ["git", "branch", "--list", "ai/QR-1"]:
                # Branch doesn't exist locally after removal
                return result
            if (cmd[0:2] == ["git", "worktree"] and "remove" in cmd) or cmd == ["git", "branch", "-D", "ai/QR-1"]:
                return result
            if cmd == ["git", "ls-remote", "--heads", "origin", "ai/QR-1"]:
                result.stdout = ""
                return result
            if cmd[0:2] == ["git", "worktree"] and "add" in cmd:
                return result

            return result

        mock_run.side_effect = mock_run_side_effect

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        info = manager.create_worktree(repo_path, "QR-1")

        assert info.branch == "ai/QR-1"

        # Verify worktree remove WAS called (since no local work)
        remove_called = False
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            if "worktree" in cmd and "remove" in cmd:
                remove_called = True
                break
        assert remove_called

    @patch("orchestrator.workspace.subprocess.run")
    def test_preserves_worktree_when_remote_branch_missing(self, mock_run, tmp_path) -> None:
        """Test that worktree is preserved when remote branch doesn't exist yet but has local commits."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"
        wt_path.mkdir(parents=True)

        def mock_run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""

            if cmd == ["git", "fetch", "origin"]:
                return result
            if cmd == ["git", "log", "--oneline", "origin/ai/QR-1..HEAD"]:
                # Remote branch doesn't exist - simulate failure
                result.returncode = 128
                result.stdout = ""
                return result
            if cmd == ["git", "log", "--oneline", "origin/main..HEAD"]:
                # Fallback check: has local commits beyond origin/main
                result.stdout = "abc123 feat: some work\n"
                return result

            return result

        mock_run.side_effect = mock_run_side_effect

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        info = manager.create_worktree(repo_path, "QR-1")

        assert info.branch == "ai/QR-1"
        assert info.path == wt_path

        # Verify worktree was NOT removed
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "remove" not in cmd

    @patch("orchestrator.workspace.subprocess.run")
    def test_deletes_branch_after_worktree_removal_when_no_local_work(self, mock_run, tmp_path) -> None:
        """Regression test for Issue 1: branch should be deleted after worktree removal."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"
        wt_path.mkdir(parents=True)

        def mock_run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""

            if cmd == ["git", "fetch", "origin"]:
                return result
            if cmd == ["git", "log", "--oneline", "origin/ai/QR-1..HEAD"]:
                # No unpushed commits
                return result
            if cmd == ["git", "status", "--porcelain"]:
                # No uncommitted changes
                return result
            # After worktree removal, simulate that branch still exists locally
            if cmd == ["git", "branch", "--list", "ai/QR-1"]:
                # First call: branch exists (stale from removed worktree)
                # This is the bug: worktree was removed but branch wasn't deleted
                result.stdout = "  ai/QR-1\n"
                return result
            if cmd[0:2] == ["git", "worktree"] and "remove" in cmd:
                return result
            if cmd == ["git", "branch", "-D", "ai/QR-1"]:
                # This should be called to delete the stale branch
                return result
            if cmd == ["git", "ls-remote", "--heads", "origin", "ai/QR-1"]:
                result.stdout = ""
                return result
            if cmd[0:2] == ["git", "worktree"]:
                return result

            return result

        mock_run.side_effect = mock_run_side_effect

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        info = manager.create_worktree(repo_path, "QR-1")

        assert info.branch == "ai/QR-1"

        # Verify that git branch -D was called after worktree removal
        branch_delete_called = False
        worktree_remove_called = False
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            if "worktree" in cmd and "remove" in cmd:
                worktree_remove_called = True
            if cmd == ["git", "branch", "-D", "ai/QR-1"]:
                branch_delete_called = True

        assert worktree_remove_called, "Worktree should have been removed"
        assert branch_delete_called, "Branch should have been deleted after worktree removal"

    @patch("orchestrator.workspace.subprocess.run")
    def test_fallback_uses_custom_base_branch_and_git_log(self, mock_run, tmp_path) -> None:
        """Regression test for Issues 2 & 3: fallback should use custom base_branch with git log."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"
        wt_path.mkdir(parents=True)

        def mock_run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""

            if cmd == ["git", "fetch", "origin"]:
                return result
            if cmd == ["git", "log", "--oneline", "origin/ai/QR-1..HEAD"]:
                # Remote branch doesn't exist - simulate failure
                result.returncode = 128
                return result
            # Issue 2 & 3: fallback should use git log with origin/develop, not git diff with origin/main
            if cmd == ["git", "log", "--oneline", "origin/develop..HEAD"]:
                # Fallback check: no commits beyond develop
                result.stdout = ""
                return result
            if cmd == ["git", "status", "--porcelain"]:
                # No uncommitted changes
                return result
            # This is the buggy behavior - it should NOT call git diff
            if cmd == ["git", "diff", "--stat", "origin/main..HEAD"]:
                # This should NOT be called when base_branch=develop
                result.stdout = "some diff output"
                return result

            return result

        mock_run.side_effect = mock_run_side_effect

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        # Use custom base_branch
        info = manager.create_worktree(repo_path, "QR-1", base_branch="develop")

        # Verify that git log with origin/develop was called, not git diff with origin/main
        git_log_develop_called = False
        git_diff_main_called = False
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            if cmd == ["git", "log", "--oneline", "origin/develop..HEAD"]:
                git_log_develop_called = True
            if cmd == ["git", "diff", "--stat", "origin/main..HEAD"]:
                git_diff_main_called = True

        assert git_log_develop_called, "Fallback should use git log with custom base_branch (origin/develop)"
        assert not git_diff_main_called, "Should use git log, not git diff, and should respect custom base_branch"


class TestRemoveWorktree:
    @patch("orchestrator.workspace.subprocess.run")
    def test_removes_via_git(self, mock_run, tmp_path) -> None:
        repo_path = tmp_path / "repo"
        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        manager.remove_worktree(repo_path, wt_path)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "worktree" in cmd
        assert "remove" in cmd
        assert "--force" in cmd

    @patch("orchestrator.workspace.shutil.rmtree")
    @patch("orchestrator.workspace.subprocess.run")
    def test_fallback_on_failure(self, mock_run, mock_rmtree, tmp_path) -> None:
        import subprocess

        repo_path = tmp_path / "repo"
        wt_path = tmp_path / "worktrees" / "QR-1" / "repo"
        wt_path.mkdir(parents=True)

        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git"),  # worktree remove fails
            MagicMock(),  # worktree prune succeeds
        ]

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "worktrees"))
        manager.remove_worktree(repo_path, wt_path)

        mock_rmtree.assert_called_once_with(wt_path)


class TestCleanupIssue:
    @patch("orchestrator.workspace.subprocess.run")
    def test_removes_all_worktrees_for_issue(self, mock_run, tmp_path) -> None:
        repo1 = tmp_path / "backend"
        repo2 = tmp_path / "frontend"

        base = tmp_path / "worktrees"
        issue_dir = base / "QR-1"
        (issue_dir / "backend").mkdir(parents=True)
        (issue_dir / "frontend").mkdir(parents=True)

        manager = WorkspaceManager(worktree_base_dir=str(base))
        manager.cleanup_issue("QR-1", [repo1, repo2])

        # Two worktree remove calls
        assert mock_run.call_count == 2

    @patch("orchestrator.workspace.subprocess.run")
    def test_skips_nonexistent_worktrees(self, mock_run, tmp_path) -> None:
        repo1 = tmp_path / "backend"
        base = tmp_path / "worktrees"

        manager = WorkspaceManager(worktree_base_dir=str(base))
        manager.cleanup_issue("QR-99", [repo1])

        mock_run.assert_not_called()


class TestRepoLocking:
    """Test per-repo locking prevents concurrent git operations."""

    def test_same_repo_gets_same_lock(self, tmp_path) -> None:
        """Two calls for the same repo path must return the same lock."""
        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "wt"))
        repo = tmp_path / "backend"
        lock1 = manager._get_repo_lock(repo)
        lock2 = manager._get_repo_lock(repo)
        assert lock1 is lock2

    def test_different_repos_get_different_locks(self, tmp_path) -> None:
        """Different repos must have independent locks."""
        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "wt"))
        lock1 = manager._get_repo_lock(tmp_path / "backend")
        lock2 = manager._get_repo_lock(tmp_path / "frontend")
        assert lock1 is not lock2

    @patch("orchestrator.workspace.subprocess.run")
    def test_cleanup_stale_holds_repo_locks(self, mock_run, tmp_path) -> None:
        """cleanup_stale should hold repo locks during git worktree prune."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)

        manager = WorkspaceManager(worktree_base_dir=str(base))
        lock = manager._get_repo_lock(repo_path)

        # Lock should be available before cleanup
        assert lock.acquire(blocking=False)
        lock.release()

        manager.cleanup_stale({"QR-1"}, [repo_path])

        # After cleanup, lock should be released
        assert lock.acquire(blocking=False)
        lock.release()

    @patch("orchestrator.workspace.subprocess.run")
    def test_create_worktree_holds_lock(self, mock_run, tmp_path) -> None:
        """create_worktree should hold the repo lock during git operations."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        manager = WorkspaceManager(worktree_base_dir=str(tmp_path / "wt"))
        lock = manager._get_repo_lock(repo_path)

        entered = threading.Event()
        blocked = threading.Event()

        def blocking_run(cmd, *args, **kwargs):
            if cmd == ["git", "fetch", "origin"]:
                entered.set()
                blocked.wait(timeout=2)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        mock_run.side_effect = blocking_run

        # Start worktree creation in a thread
        t = threading.Thread(target=manager.create_worktree, args=(repo_path, "QR-1"))
        t.start()
        entered.wait(timeout=2)

        # Lock should be held by create_worktree
        acquired = lock.acquire(blocking=False)
        assert not acquired, "Lock should be held during create_worktree"

        blocked.set()
        t.join(timeout=5)

        # Lock should be released after create_worktree finishes
        acquired = lock.acquire(blocking=False)
        assert acquired, "Lock should be released after create_worktree"
        lock.release()


class TestCleanupStale:
    """Tests for cleanup_stale() method."""

    @patch("orchestrator.workspace.subprocess.run")
    def test_removes_stale_directories(self, mock_run, tmp_path) -> None:
        """Stale worktree directories are removed via shutil.rmtree."""
        base = tmp_path / "worktrees"
        stale_dir = base / "QR-1"
        stale_dir.mkdir(parents=True)
        # Put a file inside to verify rmtree works
        (stale_dir / "repo").mkdir()
        (stale_dir / "repo" / "file.txt").write_text("content")

        active_dir = base / "QR-2"
        active_dir.mkdir(parents=True)

        manager = WorkspaceManager(worktree_base_dir=str(base))
        removed = manager.cleanup_stale({"QR-1"}, [tmp_path / "repo"])

        assert removed == 1
        assert not stale_dir.exists()
        assert active_dir.exists()

    @patch("orchestrator.workspace.subprocess.run")
    def test_prunes_all_repos(self, mock_run, tmp_path) -> None:
        """git worktree prune is called for each repo path."""
        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)

        repo1 = tmp_path / "backend"
        repo1.mkdir()
        repo2 = tmp_path / "frontend"
        repo2.mkdir()

        manager = WorkspaceManager(worktree_base_dir=str(base))
        manager.cleanup_stale({"QR-1"}, [repo1, repo2])

        # Two prune calls (one per repo)
        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert cmd == ["git", "worktree", "prune"]

        prune_cwds = [call.kwargs.get("cwd") or call[1].get("cwd") for call in mock_run.call_args_list]
        assert repo1 in prune_cwds
        assert repo2 in prune_cwds

    @patch("orchestrator.workspace.subprocess.run")
    def test_skips_nonexistent_stale_dirs(self, mock_run, tmp_path) -> None:
        """Keys in stale set but no directory on disk are skipped."""
        base = tmp_path / "worktrees"
        base.mkdir(parents=True)

        manager = WorkspaceManager(worktree_base_dir=str(base))
        removed = manager.cleanup_stale({"QR-999"}, [tmp_path / "repo"])

        assert removed == 0

    @patch("orchestrator.workspace.subprocess.run")
    def test_empty_stale_set(self, mock_run, tmp_path) -> None:
        """Empty stale set results in no removals, only prune."""
        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        manager = WorkspaceManager(worktree_base_dir=str(base))
        removed = manager.cleanup_stale(set(), [repo])

        assert removed == 0
        assert (base / "QR-1").exists()
        # Prune still called
        assert mock_run.call_count == 1

    @patch("orchestrator.workspace.subprocess.run")
    def test_rmtree_error_continues(self, mock_run, tmp_path) -> None:
        """If rmtree fails for one key, others still get cleaned."""
        base = tmp_path / "worktrees"
        (base / "QR-1").mkdir(parents=True)
        (base / "QR-2").mkdir(parents=True)

        manager = WorkspaceManager(worktree_base_dir=str(base))

        with patch("orchestrator.workspace.shutil.rmtree") as mock_rmtree:
            # First call fails, second succeeds
            mock_rmtree.side_effect = [
                OSError("permission denied"),
                None,
            ]
            removed = manager.cleanup_stale({"QR-1", "QR-2"}, [])

        # One succeeded despite the other failing
        assert removed == 1

    @patch("orchestrator.workspace.subprocess.run")
    def test_skips_nonexistent_repo_path(self, mock_run, tmp_path) -> None:
        """Repos that don't exist on disk are skipped (no crash)."""
        base = tmp_path / "worktrees"
        base.mkdir(parents=True)

        missing_repo = tmp_path / "nonexistent_repo"
        existing_repo = tmp_path / "existing_repo"
        existing_repo.mkdir()

        manager = WorkspaceManager(worktree_base_dir=str(base))
        removed = manager.cleanup_stale(set(), [missing_repo, existing_repo])

        assert removed == 0
        # Only one prune call — missing repo skipped
        assert mock_run.call_count == 1
        assert mock_run.call_args.kwargs["cwd"] == existing_repo
