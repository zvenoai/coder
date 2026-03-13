"""Tests for PreflightChecker — evidence collector for supervisor review."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from orchestrator.config import Config, RepoInfo, ReposConfig
from orchestrator.preflight_checker import COMMENT_MAX_AGE_DAYS, PreflightChecker


@dataclass
class FakeIssue:
    key: str = "QR-199"
    summary: str = "Test issue"
    description: str = "desc"
    components: list[str] | None = None
    tags: list[str] | None = None


_TEST_REPO = RepoInfo(url="https://github.com/test/repo.git", path="/tmp/test-repo", description="Test repo")


def make_config(**overrides) -> Config:
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(all_repos=[_TEST_REPO]),
        worktree_base_dir="/tmp/test-wt",
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_checker(config: Config | None = None) -> tuple[PreflightChecker, dict[str, MagicMock]]:
    """Create PreflightChecker with mocked dependencies."""
    cfg = config or make_config()
    mocks = {
        "tracker": MagicMock(),
        "github": MagicMock(),
    }
    checker = PreflightChecker(
        tracker=mocks["tracker"],
        github=mocks["github"],
        config=cfg,
    )
    return checker, mocks


class TestCommentsCheck:
    """Tests for _check_comments (Tracker comment analysis)."""

    @pytest.mark.asyncio
    async def test_detects_already_implemented_pattern_russian(self):
        checker, mocks = make_checker()
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        mocks["tracker"].get_comments.return_value = [
            {"text": "Задача уже реализована в PR #123", "createdAt": now},
        ]

        result = await checker._check_comments("QR-199")

        assert result is not None
        assert "Comment from" in result

    @pytest.mark.asyncio
    async def test_detects_already_implemented_pattern_english(self):
        checker, mocks = make_checker()
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        mocks["tracker"].get_comments.return_value = [
            {"text": "Task is already implemented in main branch", "createdAt": now},
        ]

        result = await checker._check_comments("QR-199")

        assert result is not None

    @pytest.mark.asyncio
    async def test_detects_duplicate_pattern(self):
        checker, mocks = make_checker()
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        mocks["tracker"].get_comments.return_value = [
            {"text": "Дубликат QR-100", "createdAt": now},
        ]

        result = await checker._check_comments("QR-199")

        assert result is not None

    @pytest.mark.asyncio
    async def test_ignores_old_comments(self):
        checker, mocks = make_checker()
        # Comment older than COMMENT_MAX_AGE_DAYS
        old_time = datetime.fromtimestamp(time.time() - (COMMENT_MAX_AGE_DAYS + 1) * 86400, tz=UTC)
        mocks["tracker"].get_comments.return_value = [
            {"text": "Задача уже реализована", "createdAt": old_time.isoformat().replace("+00:00", "Z")},
        ]

        result = await checker._check_comments("QR-199")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_matching_pattern(self):
        checker, mocks = make_checker()
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        mocks["tracker"].get_comments.return_value = [
            {"text": "Working on it", "createdAt": now},
            {"text": "Need more info", "createdAt": now},
        ]

        result = await checker._check_comments("QR-199")

        assert result is None

    @pytest.mark.asyncio
    async def test_handles_tracker_api_error(self):
        checker, mocks = make_checker()
        mocks["tracker"].get_comments.side_effect = requests.ConnectionError("API error")

        result = await checker._check_comments("QR-199")

        assert result is None  # Graceful fallback

    @pytest.mark.asyncio
    async def test_parses_timestamp_with_plus_0000_format(self):
        """Bug from PR review: Tracker returns timestamps like '2026-02-15T10:30:45.123+0000'.

        This format (no colon in timezone offset) is not accepted by fromisoformat(),
        causing parsing to fail and matching comments to be ignored.
        """
        checker, mocks = make_checker()
        # Real Tracker API format: +0000 (no colon)
        # Use a recent timestamp within COMMENT_MAX_AGE_DAYS (7 days)
        from datetime import UTC, datetime

        recent = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.123+0000")
        timestamp = recent
        mocks["tracker"].get_comments.return_value = [
            {"text": "Задача уже реализована", "createdAt": timestamp},
        ]

        result = await checker._check_comments("QR-199")

        # Should detect the "already implemented" comment despite the timestamp format
        assert result is not None
        assert "Comment from" in result


class TestGitHistoryCheck:
    """Tests for _check_git_history (git log analysis).

    No LLM confirmation — just reports commits found.
    """

    @pytest.mark.asyncio
    async def test_returns_none_when_unmerged_pr_exists(self):
        """Bug QR-248: git commits from an unmerged PR shouldn't count
        as evidence that the task is done."""
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_unmerged_pr = AsyncMock(return_value=True)
        checker._storage = mock_storage

        with patch(
            "orchestrator.preflight_checker.subprocess.run",
        ) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=("01dd693 feat(QR-199): implement feature\n"),
            )

            result = await checker._check_git_history("QR-199")

            assert result is None

    @pytest.mark.asyncio
    async def test_returns_evidence_when_no_unmerged_pr(self):
        """Git commits with no unmerged PR → evidence."""
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_unmerged_pr = AsyncMock(return_value=False)
        checker._storage = mock_storage

        with patch(
            "orchestrator.preflight_checker.subprocess.run",
        ) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123 feat(QR-199): done\n",
            )

            result = await checker._check_git_history("QR-199")

            assert result is not None
            assert "Git commits found" in result

    @pytest.mark.asyncio
    async def test_returns_evidence_when_storage_is_none(self):
        """No storage → can't check PR state, report evidence."""
        checker, _mocks = make_checker()
        # checker._storage is None by default

        with patch(
            "orchestrator.preflight_checker.subprocess.run",
        ) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123 feat(QR-199): done\n",
            )

            result = await checker._check_git_history("QR-199")

            assert result is not None
            assert "Git commits found" in result

    @pytest.mark.asyncio
    async def test_graceful_on_unmerged_pr_check_error(self):
        """Bug QR-248: if has_unmerged_pr fails, still report evidence
        (fail-open — same as _check_task_runs)."""
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_unmerged_pr = AsyncMock(
            side_effect=RuntimeError("DB error"),
        )
        checker._storage = mock_storage

        with patch(
            "orchestrator.preflight_checker.subprocess.run",
        ) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123 feat(QR-199): done\n",
            )

            result = await checker._check_git_history("QR-199")

            # Fail-open: report evidence even on DB error
            assert result is not None
            assert "Git commits found" in result

    @pytest.mark.asyncio
    async def test_returns_evidence_when_commits_found(self):
        checker, _mocks = make_checker()

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123 feat(QR-199): implement feature\ndef456 fix(QR-199): fix bug\n",
            )

            result = await checker._check_git_history("QR-199")

            assert result is not None
            assert "Git commits found" in result
            assert "abc123" in result or "feat(QR-199)" in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_commits_found(self):
        checker, _mocks = make_checker()

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            result = await checker._check_git_history("QR-199")

            assert result is None

    @pytest.mark.asyncio
    async def test_handles_git_error_gracefully(self):
        checker, _mocks = make_checker()

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.side_effect = RuntimeError("git not found")

            result = await checker._check_git_history("QR-199")

            assert result is None  # Graceful fallback

    @pytest.mark.asyncio
    async def test_continues_checking_repos_after_single_repo_failure(self):
        """Bug QR-194 review: if one repo fails, continue checking remaining repos."""
        repo1 = RepoInfo(url="https://github.com/test/repo1.git", path="/tmp/repo1", description="Repo 1")
        repo2 = RepoInfo(url="https://github.com/test/repo2.git", path="/tmp/repo2", description="Repo 2")
        repo3 = RepoInfo(url="https://github.com/test/repo3.git", path="/tmp/repo3", description="Repo 3")
        cfg = make_config(repos_config=ReposConfig(all_repos=[repo1, repo2, repo3]))
        checker, _mocks = make_checker(config=cfg)

        call_count = 0

        def mock_run_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("cwd") == "/tmp/repo2":
                raise RuntimeError("Repo 2 timeout")
            if kwargs.get("cwd") == "/tmp/repo3":
                return MagicMock(returncode=0, stdout="abc123 feat(QR-199): implemented\n")
            return MagicMock(returncode=0, stdout="")

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.side_effect = mock_run_side_effect

            result = await checker._check_git_history("QR-199")

            assert result is not None
            assert "Git commits found" in result
            assert call_count == 3

    @pytest.mark.asyncio
    async def test_does_not_match_substring_task_keys(self):
        """Bug QR-194: git grep should not match QR-19 when looking for QR-194."""
        checker, _mocks = make_checker()

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",  # No matches for QR-194 with word boundaries
            )

            result = await checker._check_git_history("QR-194")

            assert result is None

            # Verify git was called with word boundary pattern
            call_args = mock_run.call_args
            grep_arg = call_args[0][0][4]  # 5th element: --grep=...
            assert r"\b" in grep_arg and (r"QR\-194" in grep_arg or "QR-194" in grep_arg)

    @pytest.mark.asyncio
    async def test_git_log_uses_extended_regex(self):
        """Bug QR-194 review: git log must use -E for extended regex to support \\b word boundaries."""
        checker, _mocks = make_checker()

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            await checker._check_git_history("QR-194")

            call_args = mock_run.call_args[0][0]
            assert "--extended-regexp" in call_args or "-E" in call_args, (
                f"git log must use --extended-regexp or -E flag for \\b word boundaries to work. Got: {call_args}"
            )


class TestMergedPRCheck:
    """Tests for _check_merged_prs (GitHub PR search)."""

    @pytest.mark.asyncio
    async def test_detects_merged_pr(self):
        checker, mocks = make_checker()
        mocks["github"].search_prs.return_value = [
            {
                "number": 123,
                "title": "[QR-199] Implement feature",
                "state": "closed",
                "html_url": "https://github.com/test/repo/pull/123",
                "merged": True,
            },
        ]

        result = await checker._check_merged_prs("QR-199")

        assert result is not None
        assert "Merged PR" in result
        assert "123" in result or "[QR-199]" in result

    @pytest.mark.asyncio
    async def test_calls_search_with_org_and_merged_filter(self):
        """Bug QR-194: search_prs must be called with org and merged_only=True."""
        zvenoai_repo = RepoInfo(
            url="https://github.com/zvenoai/api.git", path="/tmp/test-repo", description="Test repo"
        )
        cfg = make_config(repos_config=ReposConfig(all_repos=[zvenoai_repo]))
        checker, mocks = make_checker(config=cfg)
        mocks["github"].search_prs.return_value = []

        await checker._check_merged_prs("QR-199")

        mocks["github"].search_prs.assert_called_once()
        call_args = mocks["github"].search_prs.call_args
        assert call_args[0][0] == "QR-199"
        assert call_args[1]["org"] == "zvenoai"
        assert call_args[1]["merged_only"] is True

    @pytest.mark.asyncio
    async def test_returns_none_when_no_merged_prs(self):
        checker, mocks = make_checker()
        mocks["github"].search_prs.return_value = []

        result = await checker._check_merged_prs("QR-199")

        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_non_merged_prs(self):
        checker, mocks = make_checker()
        mocks["github"].search_prs.return_value = [
            {
                "number": 123,
                "title": "[QR-199] Implement feature",
                "state": "closed",
                "html_url": "https://github.com/test/repo/pull/123",
                "merged": False,
            },
        ]

        result = await checker._check_merged_prs("QR-199")

        assert result is None

    @pytest.mark.asyncio
    async def test_searches_all_orgs_in_multi_org_setup(self):
        repo1 = RepoInfo(url="https://github.com/org1/repo1.git", path="/tmp/repo1", description="Org1 repo")
        repo2 = RepoInfo(url="https://github.com/org2/repo2.git", path="/tmp/repo2", description="Org2 repo")
        config = make_config(repos_config=ReposConfig(all_repos=[repo1, repo2]))
        checker, mocks = make_checker(config=config)

        def search_prs_side_effect(issue_key, org=None, merged_only=False):
            if org == "org1":
                return []
            if org == "org2":
                return [
                    {
                        "number": 456,
                        "title": "[QR-199] Implement in org2",
                        "state": "closed",
                        "html_url": "https://github.com/org2/repo2/pull/456",
                        "merged": True,
                    }
                ]
            return []

        mocks["github"].search_prs.side_effect = search_prs_side_effect

        result = await checker._check_merged_prs("QR-199")

        assert result is not None
        assert "Merged PR" in result

        assert mocks["github"].search_prs.call_count >= 2
        call_args_list = mocks["github"].search_prs.call_args_list
        orgs_called = {call.kwargs.get("org") for call in call_args_list}
        assert "org1" in orgs_called
        assert "org2" in orgs_called

    @pytest.mark.asyncio
    async def test_handles_github_api_error(self):
        checker, mocks = make_checker()
        mocks["github"].search_prs.side_effect = requests.ConnectionError("API error")

        result = await checker._check_merged_prs("QR-199")

        assert result is None  # Graceful fallback

    @pytest.mark.asyncio
    async def test_ignores_merged_pr_for_different_issue_key(self):
        """Bug: GitHub search is full-text, so searching 'QR-205' can match
        PRs where '205' appears in the body/diff."""
        checker, mocks = make_checker()
        mocks["github"].search_prs.return_value = [
            {
                "number": 24,
                "title": "[QR-144] Трекинг повторов без PR, контекст ретраев",
                "state": "closed",
                "html_url": "https://github.com/test/repo/pull/24",
                "merged": True,
            },
        ]

        result = await checker._check_merged_prs("QR-205")

        assert result is None

    @pytest.mark.asyncio
    async def test_accepts_merged_pr_with_matching_issue_key_in_title(self):
        checker, mocks = make_checker()
        mocks["github"].search_prs.return_value = [
            {
                "number": 50,
                "title": "[QR-205] Реферальные промокоды",
                "state": "closed",
                "html_url": "https://github.com/test/repo/pull/50",
                "merged": True,
            },
        ]

        result = await checker._check_merged_prs("QR-205")

        assert result is not None
        assert "50" in result

    @pytest.mark.asyncio
    async def test_skips_unrelated_pr_but_finds_matching_pr(self):
        checker, mocks = make_checker()
        mocks["github"].search_prs.return_value = [
            {
                "number": 24,
                "title": "[QR-144] Unrelated large refactoring",
                "state": "closed",
                "html_url": "https://github.com/test/repo/pull/24",
                "merged": True,
            },
            {
                "number": 50,
                "title": "[QR-205] Actual implementation",
                "state": "closed",
                "html_url": "https://github.com/test/repo/pull/50",
                "merged": True,
            },
        ]

        result = await checker._check_merged_prs("QR-205")

        assert result is not None
        assert "50" in result

    @pytest.mark.asyncio
    async def test_continues_checking_orgs_after_single_org_failure(self):
        """Bug from PR review: if search_prs fails for one org, continue checking others."""
        repo1 = RepoInfo(url="https://github.com/org1/repo1.git", path="/tmp/repo1", description="Org1 repo")
        repo2 = RepoInfo(url="https://github.com/org2/repo2.git", path="/tmp/repo2", description="Org2 repo")
        repo3 = RepoInfo(url="https://github.com/org3/repo3.git", path="/tmp/repo3", description="Org3 repo")
        config = make_config(repos_config=ReposConfig(all_repos=[repo1, repo2, repo3]))
        checker, mocks = make_checker(config=config)

        def search_prs_side_effect(issue_key, org=None, merged_only=False):
            if org == "org1":
                return []
            if org == "org2":
                raise requests.ConnectionError("org2 API timeout")
            if org == "org3":
                return [
                    {
                        "number": 789,
                        "title": "[QR-199] Fixed in org3",
                        "state": "closed",
                        "html_url": "https://github.com/org3/repo3/pull/789",
                        "merged": True,
                    }
                ]
            return []

        mocks["github"].search_prs.side_effect = search_prs_side_effect

        result = await checker._check_merged_prs("QR-199")

        assert result is not None
        assert "789" in result
        assert mocks["github"].search_prs.call_count == 3


class TestTaskRunsCheck:
    """Tests for _check_task_runs (SQLite task_runs source of truth)."""

    @pytest.mark.asyncio
    async def test_returns_evidence_when_successful_task_run(self):
        """Task with a successful task_run and no unmerged PR → evidence."""
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_successful_task_run = AsyncMock(return_value=True)
        mock_storage.has_unmerged_pr = AsyncMock(return_value=False)
        checker._storage = mock_storage

        result = await checker._check_task_runs("QR-199")

        assert result is not None
        assert "Successful task_run" in result
        mock_storage.has_successful_task_run.assert_called_once_with("QR-199")

    @pytest.mark.asyncio
    async def test_returns_none_when_no_task_runs(self):
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_successful_task_run = AsyncMock(return_value=False)
        checker._storage = mock_storage

        result = await checker._check_task_runs("QR-199")

        assert result is None

    @pytest.mark.asyncio
    async def test_graceful_when_storage_is_none(self):
        checker, _mocks = make_checker()

        result = await checker._check_task_runs("QR-199")

        assert result is None

    @pytest.mark.asyncio
    async def test_graceful_on_storage_error(self):
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_successful_task_run = AsyncMock(
            side_effect=RuntimeError("DB error"),
        )
        checker._storage = mock_storage

        result = await checker._check_task_runs("QR-199")

        assert result is None

    @pytest.mark.asyncio
    async def test_allows_dispatch_when_successful_but_pr_unmerged(self):
        """Successful task_run + unmerged PR → no evidence (agent needs to resume)."""
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_successful_task_run = AsyncMock(return_value=True)
        mock_storage.has_unmerged_pr = AsyncMock(return_value=True)
        checker._storage = mock_storage

        result = await checker._check_task_runs("QR-199")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_evidence_when_successful_and_pr_merged(self):
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_successful_task_run = AsyncMock(return_value=True)
        mock_storage.has_unmerged_pr = AsyncMock(return_value=False)
        checker._storage = mock_storage

        result = await checker._check_task_runs("QR-199")

        assert result is not None
        assert "Successful task_run" in result

    @pytest.mark.asyncio
    async def test_graceful_on_unmerged_pr_check_error(self):
        checker, _mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_successful_task_run = AsyncMock(return_value=True)
        mock_storage.has_unmerged_pr = AsyncMock(
            side_effect=RuntimeError("DB error"),
        )
        checker._storage = mock_storage

        result = await checker._check_task_runs("QR-199")

        assert result is None


class TestCombinedCheck:
    """Tests for check() (combined evidence collection)."""

    @pytest.mark.asyncio
    async def test_collects_evidence_from_all_sources(self):
        """All sources produce evidence — all included in result."""
        checker, mocks = make_checker()
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        mocks["tracker"].get_comments.return_value = [
            {"text": "Already implemented", "createdAt": now},
        ]

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123 feat(QR-199): done\n",
            )

            mocks["github"].search_prs.return_value = [
                {
                    "number": 123,
                    "title": "[QR-199] Done",
                    "merged": True,
                    "html_url": "https://github.com/test/repo/pull/123",
                },
            ]

            issue = FakeIssue(key="QR-199")
            result = await checker.check(issue)

            assert result.needs_review is True
            # Should have evidence from comments, git, and merged PR
            assert len(result.evidence) == 3

    @pytest.mark.asyncio
    async def test_no_short_circuit_on_first_evidence(self):
        """All checks run even when first check finds evidence."""
        checker, mocks = make_checker()
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        mocks["tracker"].get_comments.return_value = [
            {"text": "Already implemented", "createdAt": now},
        ]

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            mocks["github"].search_prs.return_value = []

            issue = FakeIssue(key="QR-199")
            result = await checker.check(issue)

            assert result.needs_review is True
            # Git and PR checks should have been called (no short-circuit)
            mock_run.assert_called_once()
            mocks["github"].search_prs.assert_called()

    @pytest.mark.asyncio
    async def test_no_evidence_returns_no_review(self):
        """No evidence from any source → no review needed."""
        checker, mocks = make_checker()
        mocks["tracker"].get_comments.return_value = []

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            mocks["github"].search_prs.return_value = []

            issue = FakeIssue(key="QR-199")
            result = await checker.check(issue)

            assert result.needs_review is False
            assert result.evidence == ()

    @pytest.mark.asyncio
    async def test_approved_task_bypasses_all_checks(self):
        """Supervisor-approved task bypasses evidence collection."""
        checker, mocks = make_checker()
        checker.approve_for_dispatch("QR-199")

        issue = FakeIssue(key="QR-199")
        result = await checker.check(issue)

        assert result.needs_review is False
        # No checks should have been called
        mocks["tracker"].get_comments.assert_not_called()
        mocks["github"].search_prs.assert_not_called()

    @pytest.mark.asyncio
    async def test_approval_consumed_after_use(self):
        """Approval is consumed after one check — next check collects evidence again."""
        checker, mocks = make_checker()
        mocks["tracker"].get_comments.return_value = []

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            mocks["github"].search_prs.return_value = []

            # Approve and use
            checker.approve_for_dispatch("QR-199")
            issue = FakeIssue(key="QR-199")
            await checker.check(issue)

            # Second check should not be pre-approved
            result = await checker.check(issue)
            # Checks should have been called on the second pass
            assert mocks["tracker"].get_comments.call_count >= 1

    @pytest.mark.asyncio
    async def test_evidence_reason_joins_all_items(self):
        """Multiple evidence items are joined in reason string."""
        checker, mocks = make_checker()
        mock_storage = MagicMock()
        mock_storage.has_successful_task_run = AsyncMock(return_value=True)
        mock_storage.has_unmerged_pr = AsyncMock(return_value=False)
        checker._storage = mock_storage

        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        mocks["tracker"].get_comments.return_value = [
            {"text": "Already implemented", "createdAt": now},
        ]

        with patch("orchestrator.preflight_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            mocks["github"].search_prs.return_value = []

            issue = FakeIssue(key="QR-199")
            result = await checker.check(issue)

            assert result.needs_review is True
            assert ";" in result.reason  # Multiple items joined
            assert result.source == "evidence_collector"
