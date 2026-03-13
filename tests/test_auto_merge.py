"""Tests for PR auto-merge feature — GitHubClient merge methods + PRMonitor auto-merge."""

from __future__ import annotations

import asyncio
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.agent_runner import AgentSession
from orchestrator.config import Config, ReposConfig
from orchestrator.constants import EventType
from orchestrator.github_client import (
    GitHubClient,
    PRStatus,
)
from orchestrator.pr_monitor import PRMonitor, TrackedPR
from orchestrator.workspace_tools import WorkspaceState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


VerdictType = Literal["approve", "reject"] | None


def _make_client_with_session() -> tuple[GitHubClient, MagicMock]:
    """Create a GitHubClient with a mocked HTTP session."""
    client = GitHubClient("fake-token")
    client._session = MagicMock()
    return client, client._session


def make_config(**overrides) -> Config:
    """Build a minimal Config for tests."""
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(),
        review_check_delay_seconds=0,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_monitor(**overrides) -> tuple[PRMonitor, dict[str, MagicMock]]:
    """Create a PRMonitor with all mocked dependencies."""
    mocks = {
        "tracker": MagicMock(),
        "github": MagicMock(),
        "event_bus": AsyncMock(),
        "proposal_manager": AsyncMock(),
        "cleanup_worktrees": MagicMock(),
    }

    kwargs = dict(
        tracker=mocks["tracker"],
        github=mocks["github"],
        event_bus=mocks["event_bus"],
        proposal_manager=mocks["proposal_manager"],
        config=make_config(),
        semaphore=asyncio.Semaphore(1),
        session_locks={},
        shutdown_event=asyncio.Event(),
        cleanup_worktrees_callback=mocks["cleanup_worktrees"],
        storage=None,
        dispatched_set=None,
    )
    kwargs.update(overrides)

    monitor = PRMonitor(**kwargs)
    return monitor, mocks


def make_tracked_pr(
    issue_key: str = "QR-1",
    pr_url: str = "https://github.com/test/repo/pull/1",
    session: AgentSession | None = None,
) -> TrackedPR:
    """Create a TrackedPR for testing."""
    if session is None:
        session = AsyncMock(spec=AgentSession)
    return TrackedPR(
        issue_key=issue_key,
        pr_url=pr_url,
        owner="test",
        repo="repo",
        pr_number=1,
        session=session,
        workspace_state=WorkspaceState(issue_key=issue_key),
        last_check_at=0.0,
        issue_summary="Test task",
        seen_thread_ids=set(),
        seen_failed_checks=set(),
    )


# ===================================================================
# TrackedPR.reset_review_flags
# ===================================================================


class TestResetReviewFlags:
    def test_resets_all_flags(self) -> None:
        """reset_review_flags clears all review/merge state."""
        pr = make_tracked_pr()
        pr.pre_merge_review_requested = True
        reject: VerdictType = "reject"
        pr.pre_merge_review_verdict = reject
        pr.auto_merge_attempted = True

        pr.reset_review_flags()

        assert pr.pre_merge_review_requested is False
        assert pr.pre_merge_review_verdict is None
        assert pr.auto_merge_attempted is False


# ===================================================================
# GitHubClient tests — get_pr_node_id
# ===================================================================


class TestGetPRNodeId:
    def test_get_pr_node_id_returns_id(self) -> None:
        """GraphQL query returns PR node ID successfully."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {"data": {"repository": {"pullRequest": {"id": "PR_kwDOABcDEf5abcde"}}}}
        session.post.return_value = resp

        node_id = client.get_pr_node_id("owner", "repo", 42)

        assert node_id == "PR_kwDOABcDEf5abcde"
        session.post.assert_called_once()

    def test_get_pr_node_id_graphql_error(self) -> None:
        """GraphQL errors should raise RuntimeError."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {"errors": [{"message": "Could not resolve to a PR"}]}
        session.post.return_value = resp

        with pytest.raises(RuntimeError, match="GraphQL errors"):
            client.get_pr_node_id("owner", "repo", 999)


# ===================================================================
# GitHubClient tests — enable_auto_merge
# ===================================================================


class TestEnableAutoMerge:
    def test_enable_auto_merge_success(self) -> None:
        """Mutation returns True on successful auto-merge enablement."""
        client, session = _make_client_with_session()

        # First call: get_pr_node_id (GraphQL)
        node_id_resp = MagicMock()
        node_id_resp.json.return_value = {"data": {"repository": {"pullRequest": {"id": "PR_kwDOABcDEf5abcde"}}}}

        # Second call: enablePullRequestAutoMerge mutation
        merge_resp = MagicMock()
        merge_resp.json.return_value = {
            "data": {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"mergeMethod": "SQUASH"}}}}
        }

        session.post.side_effect = [node_id_resp, merge_resp]

        result = client.enable_auto_merge("owner", "repo", 42)

        assert result is True
        assert session.post.call_count == 2

    def test_enable_auto_merge_failure_on_graphql_error(
        self,
    ) -> None:
        """Returns False when mutation returns GraphQL errors."""
        client, session = _make_client_with_session()

        # First call: get_pr_node_id succeeds
        node_id_resp = MagicMock()
        node_id_resp.json.return_value = {"data": {"repository": {"pullRequest": {"id": "PR_kwDOABcDEf5abcde"}}}}

        # Second call: mutation fails
        merge_resp = MagicMock()
        merge_resp.json.return_value = {
            "errors": [{"message": ("Pull request is not in a state where auto-merge can be enabled")}]
        }

        session.post.side_effect = [node_id_resp, merge_resp]

        result = client.enable_auto_merge("owner", "repo", 42)

        assert result is False

    def test_enable_auto_merge_with_merge_method(self) -> None:
        """Custom merge method is passed to mutation."""
        client, session = _make_client_with_session()

        node_id_resp = MagicMock()
        node_id_resp.json.return_value = {"data": {"repository": {"pullRequest": {"id": "PR_kwDOABcDEf5abcde"}}}}

        merge_resp = MagicMock()
        merge_resp.json.return_value = {
            "data": {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"mergeMethod": "REBASE"}}}}
        }

        session.post.side_effect = [node_id_resp, merge_resp]

        result = client.enable_auto_merge("owner", "repo", 42, method="REBASE")

        assert result is True
        # Verify the mutation call contains REBASE
        mutation_call = session.post.call_args_list[1]
        payload = mutation_call.kwargs.get("json", mutation_call[1] if len(mutation_call) > 1 else {})
        # The method should be in the variables
        if "json" in mutation_call.kwargs:
            payload = mutation_call.kwargs["json"]
        else:
            payload = mutation_call[0][1] if len(mutation_call[0]) > 1 else {}


# ===================================================================
# GitHubClient tests — merge_pr (REST)
# ===================================================================


class TestMergePR:
    def test_merge_pr_success(self) -> None:
        """REST PUT merge returns True on success (200 OK)."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "sha": "abc123",
            "merged": True,
            "message": "Pull Request successfully merged",
        }
        resp.raise_for_status.return_value = None
        session.put.return_value = resp

        result = client.merge_pr("owner", "repo", 42)

        assert result is True
        session.put.assert_called_once()
        call_url = session.put.call_args[0][0]
        assert "/repos/owner/repo/pulls/42/merge" in call_url

    def test_merge_pr_failure_405(self) -> None:
        """Returns False on 405 (merge not allowed)."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.status_code = 405
        resp.ok = False
        resp.json.return_value = {
            "message": "Pull Request is not mergeable",
        }

        from requests.exceptions import HTTPError

        resp.raise_for_status.side_effect = HTTPError(response=resp)
        session.put.return_value = resp

        result = client.merge_pr("owner", "repo", 42)

        assert result is False

    def test_merge_pr_failure_409(self) -> None:
        """Returns False on 409 (conflict / head out of date)."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.status_code = 409
        resp.ok = False
        resp.json.return_value = {
            "message": "Head branch was modified",
        }

        from requests.exceptions import HTTPError

        resp.raise_for_status.side_effect = HTTPError(response=resp)
        session.put.return_value = resp

        result = client.merge_pr("owner", "repo", 42)

        assert result is False

    def test_merge_pr_passes_method(self) -> None:
        """merge_pr should pass the merge method in the request body."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"merged": True}
        resp.raise_for_status.return_value = None
        session.put.return_value = resp

        client.merge_pr("owner", "repo", 42, method="rebase")

        session.put.assert_called_once()
        call_kwargs = session.put.call_args
        # Verify merge method is in the JSON body
        json_body = call_kwargs.kwargs.get("json", {})
        assert json_body.get("merge_method") == "rebase"


# ===================================================================
# GitHubClient tests — check_merge_readiness
# ===================================================================


def _graphql_readiness_response(
    mergeable: str = "MERGEABLE",
    review_decision: str = "APPROVED",
    unresolved_threads: bool = False,
    failed_check: bool = False,
) -> dict:
    """Build a GraphQL response for check_merge_readiness."""
    thread_nodes = []
    if unresolved_threads:
        thread_nodes.append({"isResolved": False})

    check_nodes: list[dict] = []
    if failed_check:
        check_nodes.append({"__typename": "CheckRun", "conclusion": "FAILURE", "status": "COMPLETED"})
    else:
        check_nodes.append({"__typename": "CheckRun", "conclusion": "SUCCESS", "status": "COMPLETED"})

    return {
        "repository": {
            "pullRequest": {
                "mergeable": mergeable,
                "reviewDecision": review_decision,
                "reviewThreads": {"nodes": thread_nodes},
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "statusCheckRollup": {
                                    "contexts": {
                                        "nodes": check_nodes,
                                    }
                                }
                            }
                        }
                    ]
                },
            }
        }
    }


class TestCheckMergeReadiness:
    def test_all_green(self) -> None:
        """All checks pass, PR approved, MERGEABLE — is_ready=True."""
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(return_value=_graphql_readiness_response())

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is True
        assert readiness.mergeable == "MERGEABLE"
        assert readiness.review_decision == "APPROVED"
        assert readiness.has_failed_checks is False
        assert readiness.has_unresolved_threads is False
        assert readiness.reasons == []

    def test_failed_checks_blocks_merge(self) -> None:
        """Failed CI checks — is_ready=False with reason."""
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(
            return_value=_graphql_readiness_response(
                failed_check=True,
            )
        )

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.has_failed_checks is True
        assert any("failed" in r.lower() or "check" in r.lower() for r in readiness.reasons)

    def test_unresolved_threads_blocks_merge(self) -> None:
        """Unresolved review threads — is_ready=False with reason."""
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(
            return_value=_graphql_readiness_response(
                unresolved_threads=True,
            )
        )

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.has_unresolved_threads is True
        assert any("thread" in r.lower() or "unresolved" in r.lower() for r in readiness.reasons)

    def test_not_mergeable_blocks_merge(self) -> None:
        """CONFLICTING mergeable state — is_ready=False."""
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(
            return_value=_graphql_readiness_response(
                mergeable="CONFLICTING",
            )
        )

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert any("conflict" in r.lower() or "mergeable" in r.lower() for r in readiness.reasons)

    def test_no_review_decision_is_non_blocking(
        self,
    ) -> None:
        """Empty review_decision (no reviews required) — is_ready=True."""
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(
            return_value=_graphql_readiness_response(
                review_decision="",
            )
        )

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.review_decision == ""
        assert readiness.is_ready is True

    def test_changes_requested_blocks_merge(self) -> None:
        """CHANGES_REQUESTED review — is_ready=False."""
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(
            return_value=_graphql_readiness_response(
                review_decision="CHANGES_REQUESTED",
            )
        )

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.review_decision == "CHANGES_REQUESTED"
        assert any("change" in r.lower() or "review" in r.lower() for r in readiness.reasons)

    def test_review_required_blocks_merge(self) -> None:
        """REVIEW_REQUIRED — is_ready=False."""
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(
            return_value=_graphql_readiness_response(
                review_decision="REVIEW_REQUIRED",
            )
        )

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.review_decision == "REVIEW_REQUIRED"
        assert any("review" in r.lower() for r in readiness.reasons)

    def test_multiple_blockers_all_reported(self) -> None:
        """Multiple issues — all reasons collected."""
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(
            return_value=_graphql_readiness_response(
                mergeable="CONFLICTING",
                review_decision="CHANGES_REQUESTED",
                failed_check=True,
                unresolved_threads=True,
            )
        )

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.has_failed_checks is True
        assert readiness.has_unresolved_threads is True
        # At least 2 distinct reasons (checks + threads/conflict)
        assert len(readiness.reasons) >= 2

    def test_status_context_failure_detected(self) -> None:
        """Failed StatusContext (legacy commit status) detected."""
        client = GitHubClient("fake-token")
        resp = _graphql_readiness_response()
        # Replace check nodes with a StatusContext failure
        commits = resp["repository"]["pullRequest"]["commits"]
        contexts = commits["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [
            {"__typename": "StatusContext", "state": "FAILURE"},
        ]
        client._graphql = MagicMock(return_value=resp)

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.has_failed_checks is True

    def test_no_rollup_is_non_blocking(self) -> None:
        """Missing statusCheckRollup (no CI configured) — is_ready=True."""
        client = GitHubClient("fake-token")
        resp = _graphql_readiness_response()
        commits = resp["repository"]["pullRequest"]["commits"]
        commits["nodes"][0]["commit"]["statusCheckRollup"] = None
        client._graphql = MagicMock(return_value=resp)

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is True
        assert readiness.has_failed_checks is False

    def test_in_progress_check_run_blocks_merge(self) -> None:
        """CheckRun with null conclusion (in-progress) — is_ready=False."""
        client = GitHubClient("fake-token")
        resp = _graphql_readiness_response()
        commits = resp["repository"]["pullRequest"]["commits"]
        contexts = commits["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [
            {"__typename": "CheckRun", "conclusion": None, "status": "IN_PROGRESS"},
        ]
        client._graphql = MagicMock(return_value=resp)

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.has_pending_checks is True
        assert any("pending" in r.lower() or "progress" in r.lower() for r in readiness.reasons)

    def test_pending_status_context_blocks_merge(self) -> None:
        """StatusContext with PENDING state — is_ready=False."""
        client = GitHubClient("fake-token")
        resp = _graphql_readiness_response()
        commits = resp["repository"]["pullRequest"]["commits"]
        contexts = commits["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [
            {"__typename": "StatusContext", "state": "PENDING"},
        ]
        client._graphql = MagicMock(return_value=resp)

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.has_pending_checks is True

    def test_queued_check_run_blocks_merge(self) -> None:
        """CheckRun with QUEUED status (not yet started) — is_ready=False."""
        client = GitHubClient("fake-token")
        resp = _graphql_readiness_response()
        commits = resp["repository"]["pullRequest"]["commits"]
        contexts = commits["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [
            {"__typename": "CheckRun", "conclusion": None, "status": "QUEUED"},
        ]
        client._graphql = MagicMock(return_value=resp)

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is False
        assert readiness.has_pending_checks is True

    def test_completed_success_check_run_not_pending(self) -> None:
        """CheckRun COMPLETED/SUCCESS — not pending, is_ready=True."""
        client = GitHubClient("fake-token")
        resp = _graphql_readiness_response()
        commits = resp["repository"]["pullRequest"]["commits"]
        contexts = commits["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [
            {"__typename": "CheckRun", "conclusion": "SUCCESS", "status": "COMPLETED"},
        ]
        client._graphql = MagicMock(return_value=resp)

        readiness = client.check_merge_readiness("owner", "repo", 42)

        assert readiness.is_ready is True
        assert readiness.has_pending_checks is False


# ===================================================================
# PRMonitor auto-merge tests
# ===================================================================


class TestProcessAutoMergeDisabled:
    async def test_does_nothing_when_disabled(self) -> None:
        """Auto-merge disabled in config — _process_auto_merge is no-op."""
        monitor, mocks = make_monitor(config=make_config(auto_merge_enabled=False))

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-300", session=session)
        monitor._tracked_prs["QR-300"] = pr

        # Mock PR as OPEN with all green
        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
        )

        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # Ensure check_merge_readiness / enable_auto_merge
        # are NOT called
        mocks["github"].check_merge_readiness = MagicMock()
        mocks["github"].enable_auto_merge = MagicMock()

        await monitor._check_all()

        mocks["github"].check_merge_readiness.assert_not_called()
        mocks["github"].enable_auto_merge.assert_not_called()


class TestProcessAutoMergeSuccess:
    async def test_enables_auto_merge_and_publishes_event(
        self,
    ) -> None:
        """Auto-merge enabled, conditions met — enables auto-merge."""
        monitor, mocks = make_monitor(config=make_config(auto_merge_enabled=True))

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-301", session=session)
        monitor._tracked_prs["QR-301"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
        )

        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # check_merge_readiness returns all-green
        readiness = MagicMock()
        readiness.is_ready = True
        readiness.mergeable = "MERGEABLE"
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(return_value=readiness)

        mocks["github"].enable_auto_merge = MagicMock(return_value=True)

        await monitor._check_all()

        # Verify enable_auto_merge was called (method passed positionally, uppercased)
        mocks["github"].enable_auto_merge.assert_called_once_with("test", "repo", 1, "SQUASH")

        # Verify PR_AUTO_MERGE_ENABLED event was published
        published_events = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
        auto_merge_events = [e for e in published_events if e.type == EventType.PR_AUTO_MERGE_ENABLED]
        assert len(auto_merge_events) == 1
        assert auto_merge_events[0].task_key == "QR-301"


class TestProcessAutoMergeNotReady:
    async def test_skips_when_conditions_not_met(self) -> None:
        """Auto-merge enabled but conditions not met — skips."""
        monitor, mocks = make_monitor(config=make_config(auto_merge_enabled=True))

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-302", session=session)
        monitor._tracked_prs["QR-302"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="CHANGES_REQUESTED",
            mergeable="MERGEABLE",
        )

        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # check_merge_readiness returns not ready
        readiness = MagicMock()
        readiness.is_ready = False
        readiness.mergeable = "MERGEABLE"
        readiness.review_decision = "CHANGES_REQUESTED"
        readiness.has_failed_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = ["Review not approved"]
        mocks["github"].check_merge_readiness = MagicMock(return_value=readiness)

        mocks["github"].enable_auto_merge = MagicMock()

        await monitor._check_all()

        # enable_auto_merge should NOT be called
        mocks["github"].enable_auto_merge.assert_not_called()

        # No PR_AUTO_MERGE_ENABLED event
        published_events = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
        auto_merge_events = [e for e in published_events if e.type == EventType.PR_AUTO_MERGE_ENABLED]
        assert len(auto_merge_events) == 0


class TestProcessAutoMergeAttemptedOnce:
    async def test_does_not_retry_after_attempt(self) -> None:
        """Once auto-merge is attempted, don't retry on same push."""
        monitor, mocks = make_monitor(config=make_config(auto_merge_enabled=True))

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-303", session=session)
        # Simulate auto_merge_attempted already set
        pr.auto_merge_attempted = True
        monitor._tracked_prs["QR-303"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
        )

        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # These should NOT be called if already attempted
        mocks["github"].check_merge_readiness = MagicMock()
        mocks["github"].enable_auto_merge = MagicMock()

        await monitor._check_all()

        # Should skip because already attempted
        mocks["github"].check_merge_readiness.assert_not_called()
        mocks["github"].enable_auto_merge.assert_not_called()

    async def test_sets_attempted_flag_on_success(self) -> None:
        """Successful auto-merge attempt sets the flag."""
        monitor, mocks = make_monitor(config=make_config(auto_merge_enabled=True))

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-304", session=session)
        monitor._tracked_prs["QR-304"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
        )

        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.mergeable = "MERGEABLE"
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(return_value=readiness)
        mocks["github"].enable_auto_merge = MagicMock(return_value=True)

        await monitor._check_all()

        # After successful auto-merge, flag should be set
        assert pr.auto_merge_attempted is True

        # Reset check time for second pass
        pr.last_check_at = 0.0
        mocks["github"].check_merge_readiness.reset_mock()
        mocks["github"].enable_auto_merge.reset_mock()

        # Second pass should skip
        await monitor._check_all()
        mocks["github"].check_merge_readiness.assert_not_called()
        mocks["github"].enable_auto_merge.assert_not_called()


class TestAutoMergeFailedEventPublished:
    async def test_publishes_failed_event_on_error(self) -> None:
        """When enable_auto_merge returns False, publish failure event."""
        monitor, mocks = make_monitor(config=make_config(auto_merge_enabled=True))

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-305", session=session)
        monitor._tracked_prs["QR-305"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
        )

        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.mergeable = "MERGEABLE"
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(return_value=readiness)

        # enable_auto_merge fails
        mocks["github"].enable_auto_merge = MagicMock(return_value=False)
        # Direct merge fallback also fails
        mocks["github"].merge_pr = MagicMock(return_value=False)

        await monitor._check_all()

        # Verify PR_AUTO_MERGE_FAILED event was published
        published_events = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
        failed_events = [e for e in published_events if e.type == EventType.PR_AUTO_MERGE_FAILED]
        assert len(failed_events) == 1
        assert failed_events[0].task_key == "QR-305"


class TestAutoMergeMethodFromConfig:
    async def test_uses_config_merge_method(self) -> None:
        """PRMonitor passes config.auto_merge_method to GitHub."""
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                auto_merge_method="rebase",
            )
        )

        session = AsyncMock(spec=AgentSession)
        pr = make_tracked_pr(issue_key="QR-306", session=session)
        monitor._tracked_prs["QR-306"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
        )

        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.mergeable = "MERGEABLE"
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(return_value=readiness)
        mocks["github"].enable_auto_merge = MagicMock(return_value=True)

        await monitor._check_all()

        # Verify method is "REBASE" from config (uppercased, positional)
        mocks["github"].enable_auto_merge.assert_called_once_with("test", "repo", 1, "REBASE")


# ===================================================================
# Pre-merge review gate tests
# ===================================================================


def _make_review_ready_mocks(
    mocks: dict[str, MagicMock],
) -> None:
    """Set up mocks so PR passes all checks before review gate."""
    status = PRStatus(
        state="OPEN",
        review_decision="APPROVED",
        mergeable="MERGEABLE",
    )
    mocks["github"].get_pr_status = MagicMock(return_value=status)
    mocks["github"].get_unresolved_threads = MagicMock(return_value=[])
    mocks["github"].get_failed_checks = MagicMock(return_value=[])

    readiness = MagicMock()
    readiness.is_ready = True
    readiness.mergeable = "MERGEABLE"
    readiness.review_decision = "APPROVED"
    readiness.has_failed_checks = False
    readiness.has_unresolved_threads = False
    readiness.reasons = []
    mocks["github"].check_merge_readiness = MagicMock(
        return_value=readiness,
    )


class TestPreMergeReviewGateRequested:
    async def test_review_requested_on_first_pass(self) -> None:
        """First pass requests review; does NOT merge yet."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-400")
        monitor._tracked_prs["QR-400"] = pr
        _make_review_ready_mocks(mocks)

        # _request_pre_merge_review sets the flag and spawns a task
        await monitor._check_all()

        assert pr.pre_merge_review_requested is True
        # merge should NOT have been called yet
        mocks["github"].enable_auto_merge.assert_not_called()
        # check_merge_readiness should NOT be called — we short-
        # circuit before it when requesting a review.
        mocks["github"].check_merge_readiness.assert_not_called()


class TestPreMergeReviewGateVerdicts:
    """Review gate behavior for different verdict states."""

    @pytest.mark.parametrize(
        (
            "verdict",
            "expect_merge_called",
            "expect_attempted",
            "expect_readiness_called",
        ),
        [
            ("approve", True, True, True),
            ("reject", False, True, False),
            (None, False, False, False),
        ],
        ids=["approve_merges", "reject_blocks", "waiting_skips"],
    )
    async def test_verdict_outcome(
        self,
        verdict: VerdictType,
        expect_merge_called: bool,
        expect_attempted: bool,
        expect_readiness_called: bool,
    ) -> None:
        """Parametrized: approve merges, reject blocks, empty waits."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-401")
        pr.pre_merge_review_requested = True
        pr.pre_merge_review_verdict = verdict
        monitor._tracked_prs["QR-401"] = pr

        _make_review_ready_mocks(mocks)
        mocks["github"].enable_auto_merge = MagicMock(return_value=True)

        await monitor._check_all()

        if expect_merge_called:
            mocks["github"].enable_auto_merge.assert_called_once()
        else:
            mocks["github"].enable_auto_merge.assert_not_called()
        assert pr.auto_merge_attempted is expect_attempted
        # check_merge_readiness should only be called when verdict
        # is approve (reject/waiting short-circuit before it).
        if expect_readiness_called:
            mocks["github"].check_merge_readiness.assert_called_once()
        else:
            mocks["github"].check_merge_readiness.assert_not_called()


class TestPreMergeReviewGateDisabled:
    async def test_skips_review_when_disabled(self) -> None:
        """With review disabled, auto-merge proceeds directly."""
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=False,
            ),
        )

        pr = make_tracked_pr(issue_key="QR-404")
        monitor._tracked_prs["QR-404"] = pr

        _make_review_ready_mocks(mocks)
        mocks["github"].enable_auto_merge = MagicMock(return_value=True)

        await monitor._check_all()

        # Should merge without review
        mocks["github"].enable_auto_merge.assert_called_once()
        assert pr.pre_merge_review_requested is False


class TestPreMergeReviewResetOnFix:
    async def test_flags_reset_after_successful_ci_fix(
        self,
    ) -> None:
        """After agent fixes CI, review flags are reset and a fresh review is requested.

        Flow: _process_failed_checks resets flags (success fix),
        then _process_auto_merge in the same cycle re-requests review.
        """
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        session = AsyncMock(spec=AgentSession)
        from orchestrator.agent_runner import AgentResult

        session.send.return_value = AgentResult(
            success=True,
            output="Fixed",
        )
        session.drain_pending_messages.return_value = AgentResult(
            success=True,
            output="Fixed",
        )

        pr = make_tracked_pr(issue_key="QR-405", session=session)
        pr.pre_merge_review_requested = True
        reject: VerdictType = "reject"
        pr.pre_merge_review_verdict = reject
        monitor._tracked_prs["QR-405"] = pr

        # Simulate: new failed check that agent will fix
        from orchestrator.github_client import FailedCheck

        failed = FailedCheck(
            name="ci",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            summary=None,
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=PRStatus(
                state="OPEN",
                review_decision="APPROVED",
                mergeable="MERGEABLE",
            )
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[failed],
        )

        await monitor._check_all()

        # _process_failed_checks resets flags, then _process_auto_merge
        # re-requests review in the same cycle. Verdict is cleared,
        # merge not attempted, but review requested again.
        assert pr.pre_merge_review_verdict is None
        assert pr.auto_merge_attempted is False
        assert pr.pre_merge_review_requested is True


# ===================================================================
# Pre-merge review — event data contains issues
# ===================================================================


class TestPreMergeReviewEventIssues:
    async def test_event_data_includes_serialized_issues(
        self,
    ) -> None:
        """PR_REVIEW_COMPLETED event includes serialized issue dicts."""
        from orchestrator.pre_merge_reviewer import (
            ReviewIssue,
            ReviewVerdict,
        )

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-500")
        monitor._tracked_prs["QR-500"] = pr
        _make_review_ready_mocks(mocks)

        # Simulate the verdict with issues
        verdict = ReviewVerdict(
            decision="reject",
            summary="Found problems",
            issues=(
                ReviewIssue(
                    severity="critical",
                    category="contracts",
                    file_path="src/api.py",
                    description="Bad import",
                    suggestion="Fix import",
                ),
            ),
            confidence=0.85,
            cost_usd=0.10,
            duration_seconds=3.0,
        )
        reviewer.review = AsyncMock(return_value=verdict)
        mocks["github"].post_review = MagicMock(return_value=True)

        # Call _request_pre_merge_review directly
        await monitor._request_pre_merge_review("QR-500", pr)

        # Wait for the background task to finish
        for task in list(monitor._review_tasks.values()):
            await task

        # Find PR_REVIEW_COMPLETED event
        published = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
        review_events = [e for e in published if e.type == EventType.PR_REVIEW_COMPLETED]
        assert len(review_events) == 1
        data = review_events[0].data

        # Issues should be a list of dicts, not empty
        assert len(data["issues"]) == 1
        assert data["issues"][0]["severity"] == "critical"
        assert data["issues"][0]["file_path"] == "src/api.py"
        assert data["issues"][0]["description"] == "Bad import"


# ===================================================================
# Pre-merge review — SHA change cancels in-flight review
# ===================================================================


class TestPreMergeReviewShaReset:
    async def test_new_commit_resets_review_and_cancels_task(
        self,
    ) -> None:
        """New commit SHA resets review flags and cancels in-flight review task."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-600")
        # Simulate: review was already requested, previous SHA known
        pr.pre_merge_review_requested = True
        reject: VerdictType = "reject"
        pr.pre_merge_review_verdict = reject
        pr.auto_merge_attempted = True
        pr.last_seen_head_sha = "aaaa1111"
        monitor._tracked_prs["QR-600"] = pr

        # Create a fake in-flight review task
        review_event = asyncio.Event()

        async def _slow_review() -> None:
            await review_event.wait()

        task = asyncio.get_running_loop().create_task(_slow_review())
        monitor._review_tasks["QR-600"] = task

        # New commit changes head SHA
        new_status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
            head_sha="bbbb2222",
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=new_status,
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        await monitor._check_all()

        # Old task should be cancelled
        assert task.cancelled()
        # Flags were reset, but the same _check_all cycle
        # re-requests review (review enabled + not yet requested
        # after reset), so pre_merge_review_requested is True again.
        assert pr.pre_merge_review_requested is True
        assert pr.pre_merge_review_verdict is None
        assert pr.auto_merge_attempted is False
        # SHA updated
        assert pr.last_seen_head_sha == "bbbb2222"

    async def test_first_poll_does_not_reset(self) -> None:
        """First poll (empty last_seen_head_sha) should not reset flags."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-601")
        # First poll — no previous SHA
        pr.last_seen_head_sha = ""
        pr.pre_merge_review_requested = True
        pr.pre_merge_review_verdict = "approve"
        monitor._tracked_prs["QR-601"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
            head_sha="cccc3333",
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=status,
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=MagicMock(
                is_ready=True,
                review_decision="APPROVED",
            ),
        )
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=True,
        )

        await monitor._check_all()

        # Should NOT have reset — first poll with empty SHA
        assert pr.last_seen_head_sha == "cccc3333"
        # Verdict should still be approve (not reset)
        # (merge would have been attempted, so auto_merge_attempted
        #  is True from the merge path, not from a reset)


# ===================================================================
# Pre-merge review — timeout fail-open
# ===================================================================


class TestPreMergeReviewTimeout:
    async def test_timeout_rejects_when_fail_closed(self) -> None:
        """Review timeout rejects when fail_open=False (default)."""
        reviewer = AsyncMock()
        monitor, _mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
                pre_merge_review_timeout_seconds=0,
                pre_merge_review_fail_open=False,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-700")
        monitor._tracked_prs["QR-700"] = pr

        # Reviewer hangs forever — will be timed out
        hang_event = asyncio.Event()

        async def _hang(*args, **kwargs):
            await hang_event.wait()
            # Unreachable — timeout will fire first
            return MagicMock()  # pragma: no cover

        reviewer.review = _hang

        await monitor._request_pre_merge_review("QR-700", pr)

        # Wait for the background task to finish (timeout)
        for task in list(monitor._review_tasks.values()):
            try:
                await task
            except Exception:
                pass

        # Timeout with fail_open=False should reject
        assert pr.pre_merge_review_verdict == "reject"


# ===================================================================
# Pre-merge review — max concurrent reviews limit
# ===================================================================


class TestMaxConcurrentReviews:
    async def test_skips_when_limit_reached(self) -> None:
        """Review is skipped when max_concurrent_reviews limit is reached."""
        reviewer = AsyncMock()
        monitor, _mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
                max_concurrent_reviews=1,
            ),
            reviewer=reviewer,
        )

        # Create a running review task for QR-800
        pr1 = make_tracked_pr(issue_key="QR-800")
        monitor._tracked_prs["QR-800"] = pr1
        running_event = asyncio.Event()

        async def _block() -> None:
            await running_event.wait()

        task = asyncio.get_running_loop().create_task(_block())
        monitor._review_tasks["QR-800"] = task

        # Try to request review for QR-801
        pr2 = make_tracked_pr(issue_key="QR-801")
        monitor._tracked_prs["QR-801"] = pr2

        await monitor._request_pre_merge_review("QR-801", pr2)

        # QR-801 should NOT have a review task — limit reached
        assert "QR-801" not in monitor._review_tasks
        # Flag stays False — early return before flag assignment
        assert pr2.pre_merge_review_requested is False

        # Clean up
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_done_tasks_dont_count_toward_limit(self) -> None:
        """Completed tasks in _review_tasks don't block new reviews."""
        reviewer = AsyncMock()
        monitor, _mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
                max_concurrent_reviews=1,
            ),
            reviewer=reviewer,
        )

        # Create a DONE task still in the dict
        # (done-callback hasn't fired yet)
        pr1 = make_tracked_pr(issue_key="QR-810")
        monitor._tracked_prs["QR-810"] = pr1

        async def _noop() -> None:
            pass

        done_task = asyncio.get_running_loop().create_task(_noop())
        await done_task  # Let it finish
        # Intentionally NOT adding done-callback — simulates
        # callback delay
        monitor._review_tasks["QR-810"] = done_task

        # New review should be allowed despite dict having 1 entry
        pr2 = make_tracked_pr(issue_key="QR-811")
        monitor._tracked_prs["QR-811"] = pr2

        from orchestrator.pre_merge_reviewer import ReviewVerdict

        reviewer.review = AsyncMock(
            return_value=ReviewVerdict(
                decision="approve",
                summary="ok",
                issues=(),
                confidence=0.9,
                cost_usd=0.0,
                duration_seconds=1.0,
            ),
        )

        await monitor._request_pre_merge_review("QR-811", pr2)

        # QR-811 SHOULD have a review task — done task doesn't count
        assert "QR-811" in monitor._review_tasks

        # Clean up
        for task in list(monitor._review_tasks.values()):
            if not task.done():
                await task


# ===================================================================
# PRMonitor.close_all cancels review tasks
# ===================================================================


class TestCloseAllCancelsReviews:
    async def test_close_all_cancels_in_flight_reviews(
        self,
    ) -> None:
        """close_all cancels running review tasks and awaits them."""
        monitor, _mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
        )

        pr = make_tracked_pr(issue_key="QR-900")
        monitor._tracked_prs["QR-900"] = pr

        hang_event = asyncio.Event()
        cancel_seen = False

        async def _hang() -> None:
            nonlocal cancel_seen
            try:
                await hang_event.wait()
            except asyncio.CancelledError:
                cancel_seen = True
                raise

        task = asyncio.get_running_loop().create_task(_hang())
        monitor._review_tasks["QR-900"] = task

        # Yield so _hang() starts and reaches its await point.
        await asyncio.sleep(0)

        await monitor.close_all()

        assert cancel_seen
        assert task.cancelled() or task.done()
        assert len(monitor._review_tasks) == 0
        assert len(monitor._tracked_prs) == 0


# ===================================================================
# Pre-merge review — background task tracked in _review_tasks
# ===================================================================


class TestPreMergeReviewTaskTracking:
    async def test_background_task_stored_in_review_tasks(
        self,
    ) -> None:
        """Background review task is tracked in _review_tasks dict."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-501")
        monitor._tracked_prs["QR-501"] = pr
        _make_review_ready_mocks(mocks)

        from orchestrator.pre_merge_reviewer import ReviewVerdict

        verdict = ReviewVerdict(
            decision="approve",
            summary="ok",
            issues=(),
            confidence=0.9,
            cost_usd=0.05,
            duration_seconds=1.0,
        )
        reviewer.review = AsyncMock(return_value=verdict)

        await monitor._request_pre_merge_review("QR-501", pr)

        # Task should be in the set while running
        assert len(monitor._review_tasks) >= 1

        # Wait for completion
        for task in list(monitor._review_tasks.values()):
            await task

        # After completion, done callback removes it
        assert len(monitor._review_tasks) == 0

    async def test_duplicate_review_prevented(self) -> None:
        """Second review request for same issue is a no-op while first runs."""
        from orchestrator.pre_merge_reviewer import ReviewVerdict

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-502")
        monitor._tracked_prs["QR-502"] = pr
        _make_review_ready_mocks(mocks)

        # Make review hang so the task stays in-flight
        hang = asyncio.Event()

        async def slow_review(**kwargs):
            await hang.wait()
            return ReviewVerdict(
                decision="approve",
                summary="ok",
                issues=(),
                confidence=0.9,
                cost_usd=0.05,
                duration_seconds=1.0,
            )

        reviewer.review = slow_review

        await monitor._request_pre_merge_review("QR-502", pr)
        assert len(monitor._review_tasks) == 1

        # Second request should be a no-op (task still in-flight)
        start_events = mocks["event_bus"].publish.call_count
        await monitor._request_pre_merge_review("QR-502", pr)
        # No new PR_REVIEW_STARTED event published
        assert mocks["event_bus"].publish.call_count == start_events
        assert len(monitor._review_tasks) == 1

        # Cleanup: unblock and await
        hang.set()
        for task in list(monitor._review_tasks.values()):
            await task

    async def test_cancel_review_on_new_commit(self) -> None:
        """New commit cancels in-flight review and resets flags."""
        from orchestrator.pre_merge_reviewer import ReviewVerdict

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-503")
        monitor._tracked_prs["QR-503"] = pr
        _make_review_ready_mocks(mocks)

        # Make review hang
        hang = asyncio.Event()

        async def slow_review(**kwargs):
            await hang.wait()
            return ReviewVerdict(
                decision="reject",
                summary="bad",
                issues=(),
                confidence=0.8,
                cost_usd=0.05,
                duration_seconds=1.0,
            )

        reviewer.review = slow_review

        await monitor._request_pre_merge_review("QR-503", pr)
        assert "QR-503" in monitor._review_tasks

        # Simulate what _check_all does on new commit
        monitor._cancel_review_task("QR-503")
        pr.reset_review_flags()

        # Task should be removed from dict
        assert "QR-503" not in monitor._review_tasks
        # Flags should be reset
        assert not pr.pre_merge_review_requested
        assert pr.pre_merge_review_verdict is None
        assert not pr.auto_merge_attempted


# ===================================================================
# Pre-merge review — post_review failure logs warning
# ===================================================================


class TestPostReviewCommentsWarning:
    async def test_logs_warning_on_post_review_failure(
        self,
    ) -> None:
        """_post_review_comments logs warning when post_review fails."""
        from orchestrator.pre_merge_reviewer import (
            ReviewIssue,
            ReviewVerdict,
        )

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-502")
        monitor._tracked_prs["QR-502"] = pr

        verdict = ReviewVerdict(
            decision="reject",
            summary="Bad code",
            issues=(
                ReviewIssue(
                    severity="major",
                    category="quality",
                    file_path="foo.py",
                    description="Bad",
                    suggestion="Fix",
                ),
            ),
            confidence=0.8,
            cost_usd=0.05,
            duration_seconds=2.0,
        )

        # post_review returns False (failure)
        mocks["github"].post_review = MagicMock(return_value=False)

        # Call directly — should not raise, just log warning
        await monitor._post_review_comments("QR-502", pr, verdict)

        # Verify post_review was called
        mocks["github"].post_review.assert_called_once()


# ===================================================================
# Rejection feedback delivered to worker session
# ===================================================================


class TestRejectFeedbackDeliveredToWorker:
    async def test_session_send_called_on_reject(self) -> None:
        """After reject, worker session receives rejection prompt."""
        from orchestrator.pre_merge_reviewer import (
            ReviewIssue,
            ReviewVerdict,
        )

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        session = AsyncMock(spec=AgentSession)
        result_mock = MagicMock()
        result_mock.proposals = []
        session.send = AsyncMock(return_value=result_mock)
        session.drain_pending_messages = AsyncMock(
            return_value=result_mock,
        )

        pr = make_tracked_pr(
            issue_key="QR-600",
            session=session,
        )
        monitor._tracked_prs["QR-600"] = pr
        _make_review_ready_mocks(mocks)

        verdict = ReviewVerdict(
            decision="reject",
            summary="Bad code",
            issues=(
                ReviewIssue(
                    severity="major",
                    category="quality",
                    file_path="foo.py",
                    description="Bad function",
                    suggestion="Refactor",
                ),
            ),
            confidence=0.8,
            cost_usd=0.05,
            duration_seconds=2.0,
        )
        reviewer.review = AsyncMock(return_value=verdict)
        mocks["github"].post_review = MagicMock(return_value=True)

        await monitor._request_pre_merge_review("QR-600", pr)
        for task in list(monitor._review_tasks.values()):
            await task

        # Worker session.send must have been called with
        # the rejection prompt
        session.send.assert_called_once()
        prompt_arg = session.send.call_args[0][0]
        assert "Bad code" in prompt_arg
        assert "foo.py" in prompt_arg
        assert "QR-600" in prompt_arg

    async def test_proposals_from_rejection_fix_processed(
        self,
    ) -> None:
        """Proposals returned by the worker fix are processed."""
        from orchestrator.pre_merge_reviewer import (
            ReviewIssue,
            ReviewVerdict,
        )

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        session = AsyncMock(spec=AgentSession)
        result_mock = MagicMock()
        result_mock.proposals = ["some-proposal"]
        session.send = AsyncMock(return_value=result_mock)
        session.drain_pending_messages = AsyncMock(
            return_value=result_mock,
        )

        pr = make_tracked_pr(
            issue_key="QR-601",
            session=session,
        )
        monitor._tracked_prs["QR-601"] = pr
        _make_review_ready_mocks(mocks)

        verdict = ReviewVerdict(
            decision="reject",
            summary="Issues found",
            issues=(
                ReviewIssue(
                    severity="major",
                    category="quality",
                    file_path="bar.py",
                    description="Bad",
                    suggestion="Fix",
                ),
            ),
            confidence=0.8,
            cost_usd=0.05,
            duration_seconds=2.0,
        )
        reviewer.review = AsyncMock(return_value=verdict)
        mocks["github"].post_review = MagicMock(return_value=True)

        await monitor._request_pre_merge_review("QR-601", pr)
        for task in list(monitor._review_tasks.values()):
            await task

        mocks["proposal_manager"].process_proposals.assert_called_once_with(
            "QR-601",
            ["some-proposal"],
        )


# ===================================================================
# Direct merge emits PR_DIRECT_MERGED event
# ===================================================================


class TestDirectMergeEventType:
    async def test_direct_merge_publishes_pr_direct_merged(
        self,
    ) -> None:
        """REST fallback merge success emits PR_DIRECT_MERGED."""
        monitor, mocks = make_monitor(
            config=make_config(auto_merge_enabled=True),
        )

        pr = make_tracked_pr(issue_key="QR-700")
        monitor._tracked_prs["QR-700"] = pr

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.review_decision = "APPROVED"
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=readiness,
        )
        # GraphQL auto-merge fails → REST fallback succeeds
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=False,
        )
        mocks["github"].merge_pr = MagicMock(return_value=True)

        status = MagicMock()
        status.state = "OPEN"
        status.head_sha = ""
        status.mergeable = "MERGEABLE"

        await monitor._process_auto_merge("QR-700", pr, status)

        published = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
        direct_events = [e for e in published if e.type == EventType.PR_DIRECT_MERGED]
        assert len(direct_events) == 1
        assert direct_events[0].task_key == "QR-700"
        assert direct_events[0].data["merged"] is True

    async def test_graphql_success_still_uses_auto_merge_enabled(
        self,
    ) -> None:
        """GraphQL success still publishes PR_AUTO_MERGE_ENABLED."""
        monitor, mocks = make_monitor(
            config=make_config(auto_merge_enabled=True),
        )

        pr = make_tracked_pr(issue_key="QR-701")
        monitor._tracked_prs["QR-701"] = pr

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.review_decision = "APPROVED"
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=readiness,
        )
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=True,
        )

        status = MagicMock()
        status.state = "OPEN"
        status.head_sha = ""
        status.mergeable = "MERGEABLE"

        await monitor._process_auto_merge("QR-701", pr, status)

        published = [call[0][0] for call in mocks["event_bus"].publish.call_args_list]
        enabled_events = [e for e in published if e.type == EventType.PR_AUTO_MERGE_ENABLED]
        assert len(enabled_events) == 1


# ===================================================================
# Race condition fix — verdict set after side effects
# ===================================================================


class TestRejectVerdictSetAfterComments:
    async def test_verdict_not_set_before_comments_posted(
        self,
    ) -> None:
        """On reject, verdict stays empty until after comments are posted."""
        from orchestrator.pre_merge_reviewer import (
            ReviewIssue,
            ReviewVerdict,
        )

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-510")
        monitor._tracked_prs["QR-510"] = pr
        _make_review_ready_mocks(mocks)

        verdict = ReviewVerdict(
            decision="reject",
            summary="Bad code",
            issues=(
                ReviewIssue(
                    severity="critical",
                    category="contracts",
                    file_path="src/api.py",
                    description="Bad import",
                    suggestion="Fix import",
                ),
            ),
            confidence=0.85,
            cost_usd=0.10,
            duration_seconds=3.0,
        )
        reviewer.review = AsyncMock(return_value=verdict)

        # Capture verdict state when _post_review_comments is called
        verdict_during_post: list[str] = []
        original_post = monitor._post_review_comments

        async def spy_post(*args, **kwargs):
            verdict_during_post.append(pr.pre_merge_review_verdict)
            return await original_post(*args, **kwargs)

        monitor._post_review_comments = spy_post  # type: ignore[assignment]
        mocks["github"].post_review = MagicMock(return_value=True)

        await monitor._request_pre_merge_review("QR-510", pr)
        for task in list(monitor._review_tasks.values()):
            await task

        # Verdict should have been None when comments were posted
        assert verdict_during_post == [None]
        # But now it should be set
        assert pr.pre_merge_review_verdict == "reject"


# ===================================================================
# Head SHA change — reset review/merge flags on new commits
# ===================================================================


class TestHeadSHAChangeResetsFlags:
    async def test_flags_reset_on_new_commit(self) -> None:
        """New commit (head SHA change) resets flags and triggers fresh review."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-520")
        pr.pre_merge_review_requested = True
        reject: VerdictType = "reject"
        pr.pre_merge_review_verdict = reject
        pr.auto_merge_attempted = True
        pr.last_seen_head_sha = "old_sha_abc123"
        monitor._tracked_prs["QR-520"] = pr

        # Return status with new head SHA
        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
            head_sha="new_sha_def456",
        )
        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        # Setup merge readiness (will be checked after flag reset)
        readiness = MagicMock()
        readiness.is_ready = True
        readiness.mergeable = "MERGEABLE"
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=readiness,
        )

        await monitor._check_all()

        # Old reject verdict should be cleared
        assert pr.pre_merge_review_verdict is None
        # auto_merge_attempted should be reset (not permanently blocked)
        assert pr.auto_merge_attempted is False
        # A new review should be re-requested for the fresh commit
        assert pr.pre_merge_review_requested is True
        assert pr.last_seen_head_sha == "new_sha_def456"

    async def test_no_reset_on_same_sha(self) -> None:
        """Same head SHA — flags NOT reset."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-521")
        pr.pre_merge_review_requested = True
        pr.pre_merge_review_verdict = "reject"
        pr.auto_merge_attempted = True
        pr.last_seen_head_sha = "same_sha_abc123"
        monitor._tracked_prs["QR-521"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
            head_sha="same_sha_abc123",
        )
        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        await monitor._check_all()

        # Flags should NOT be reset
        assert pr.pre_merge_review_requested is True
        assert pr.pre_merge_review_verdict == "reject"
        assert pr.auto_merge_attempted is True

    async def test_first_sha_stored_without_reset(self) -> None:
        """First SHA observation stores value without resetting."""
        monitor, mocks = make_monitor(
            config=make_config(auto_merge_enabled=True),
        )

        pr = make_tracked_pr(issue_key="QR-522")
        assert pr.last_seen_head_sha == ""
        monitor._tracked_prs["QR-522"] = pr

        status = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
            head_sha="first_sha_abc",
        )
        mocks["github"].get_pr_status = MagicMock(return_value=status)
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(return_value=[])

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.mergeable = "MERGEABLE"
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=readiness,
        )

        await monitor._check_all()

        # SHA stored, no reset triggered (was empty before)
        assert pr.last_seen_head_sha == "first_sha_abc"


# ===================================================================
# Bug #1: Review task cancelled on PR close/merge
# ===================================================================


class TestPRClosedCancelsReview:
    """_handle_pr_closed_or_merged must cancel in-flight review tasks."""

    async def test_pr_closed_cancels_review_task(self) -> None:
        """Closing a PR cancels any running review sub-agent."""
        monitor, mocks = make_monitor()
        pr = make_tracked_pr()
        monitor._tracked_prs["QR-1"] = pr

        # Simulate an in-flight review task
        review_task = AsyncMock(spec=asyncio.Task)
        review_task.done.return_value = False
        monitor._review_tasks["QR-1"] = review_task

        mocks["tracker"].transition_to_closed = MagicMock()

        await monitor._handle_pr_closed_or_merged(
            "QR-1",
            pr,
            "closed",
        )

        review_task.cancel.assert_called_once()
        assert "QR-1" not in monitor._review_tasks

    async def test_pr_merged_cancels_review_task(self) -> None:
        """Merging a PR cancels any running review sub-agent."""
        monitor, mocks = make_monitor()
        pr = make_tracked_pr()
        monitor._tracked_prs["QR-1"] = pr

        review_task = AsyncMock(spec=asyncio.Task)
        review_task.done.return_value = False
        monitor._review_tasks["QR-1"] = review_task

        mocks["tracker"].transition_to_closed = MagicMock()
        mocks["tracker"].get_links = MagicMock(return_value=[])

        await monitor._handle_pr_closed_or_merged(
            "QR-1",
            pr,
            "merged",
        )

        review_task.cancel.assert_called_once()


# ===================================================================
# Bug #7: Stale review cancelled on CI fix
# ===================================================================


class TestCIFixCancelsStaleReview:
    """_process_failed_checks must cancel stale review on successful fix."""

    async def test_ci_fix_cancels_stale_review(self) -> None:
        """Successful CI fix cancels in-flight review before reset."""
        monitor, mocks = make_monitor()
        pr = make_tracked_pr()
        pr.seen_failed_checks = {"check:FAILURE"}
        pr.pre_merge_review_requested = True
        monitor._tracked_prs["QR-1"] = pr

        review_task = AsyncMock(spec=asyncio.Task)
        review_task.done.return_value = False
        monitor._review_tasks["QR-1"] = review_task

        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        # Simulate a successful agent fix
        result = MagicMock()
        result.success = True
        result.proposals = []
        result.output = "Fixed"
        pr.session.send = AsyncMock(return_value=result)
        pr.session.drain_pending_messages = AsyncMock(
            return_value=result,
        )

        # Add a fresh failure to trigger the agent
        from orchestrator.github_client import FailedCheck

        fresh_failure = FailedCheck(
            name="quality",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url="",
            summary="",
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[fresh_failure],
        )

        await monitor._process_failed_checks("QR-1", pr)

        review_task.cancel.assert_called_once()
        assert pr.pre_merge_review_requested is False


# ===================================================================
# Bug #2: Merge method case consistency
# ===================================================================


class TestMergeMethodCase:
    """GraphQL gets uppercase, REST fallback gets lowercase."""

    @pytest.mark.parametrize(
        "config_value",
        ["squash", "SQUASH", "Squash"],
        ids=["lowercase", "uppercase", "mixed"],
    )
    async def test_graphql_gets_uppercase_rest_gets_lowercase(
        self,
        config_value: str,
    ) -> None:
        """Merge method normalization: .upper() for GraphQL, .lower() for REST."""
        config = make_config(
            auto_merge_enabled=True,
            auto_merge_method=config_value,
        )
        monitor, mocks = make_monitor(config=config)
        pr = make_tracked_pr()
        monitor._tracked_prs["QR-1"] = pr

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.review_decision = "APPROVED"
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=readiness,
        )
        # GraphQL auto-merge fails → falls back to REST
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=False,
        )
        mocks["github"].merge_pr = MagicMock(return_value=True)

        status = MagicMock()
        status.state = "OPEN"
        status.head_sha = ""
        status.mergeable = "MERGEABLE"

        await monitor._process_auto_merge("QR-1", pr, status)

        # GraphQL call: uppercase
        graphql_call = mocks["github"].enable_auto_merge
        graphql_call.assert_called_once()
        graphql_method = graphql_call.call_args[0][3]
        assert graphql_method == config_value.upper()

        # REST call: lowercase
        rest_call = mocks["github"].merge_pr
        rest_call.assert_called_once()
        rest_method = rest_call.call_args[0][3]
        assert rest_method == config_value.lower()


# ===================================================================
# Stale REQUEST_CHANGES dismissed on re-review approve
# ===================================================================


class TestDismissStaleRequestChanges:
    """After reject→fix→re-review approve, bot must post APPROVE
    to dismiss its own REQUEST_CHANGES so GitHub reviewDecision
    flips back to APPROVED and auto-merge can proceed.
    """

    async def test_approve_posted_when_request_changes_was_posted(
        self,
    ) -> None:
        """Re-review approve posts APPROVE to dismiss stale review."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        session = AsyncMock(spec=AgentSession)
        result_mock = MagicMock()
        result_mock.proposals = []
        session.send = AsyncMock(return_value=result_mock)
        session.drain_pending_messages = AsyncMock(
            return_value=result_mock,
        )

        pr = make_tracked_pr(
            issue_key="QR-800",
            session=session,
        )
        # Simulate: previous cycle already posted REQUEST_CHANGES
        pr.request_changes_posted = True
        # Internal re-review approves
        pr.pre_merge_review_requested = True
        pr.pre_merge_review_verdict = "approve"
        monitor._tracked_prs["QR-800"] = pr

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_pending_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=readiness,
        )
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=True,
        )
        mocks["github"].post_review = MagicMock(return_value=True)

        status = MagicMock()
        status.state = "OPEN"
        status.head_sha = ""
        status.mergeable = "MERGEABLE"

        await monitor._process_auto_merge("QR-800", pr, status)

        # APPROVE review must have been posted to dismiss
        # stale REQUEST_CHANGES
        mocks["github"].post_review.assert_called_once_with(
            "test",
            "repo",
            1,
            body="Pre-merge review approved — dismissing previous request for changes.",
            event="APPROVE",
        )
        # Flag cleared after posting
        assert pr.request_changes_posted is False

    async def test_no_approve_posted_when_no_prior_request_changes(
        self,
    ) -> None:
        """If no REQUEST_CHANGES was posted, no APPROVE is needed."""
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-801")
        pr.pre_merge_review_requested = True
        pr.pre_merge_review_verdict = "approve"
        # request_changes_posted defaults to False
        monitor._tracked_prs["QR-801"] = pr

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_pending_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=readiness,
        )
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=True,
        )
        mocks["github"].post_review = MagicMock(return_value=True)

        status = MagicMock()
        status.state = "OPEN"
        status.head_sha = ""
        status.mergeable = "MERGEABLE"

        await monitor._process_auto_merge("QR-801", pr, status)

        # post_review should NOT be called
        mocks["github"].post_review.assert_not_called()

    async def test_dismiss_after_restart_uses_readiness(
        self,
    ) -> None:
        """After restart, flag is lost but GitHub still has
        CHANGES_REQUESTED — dismiss based on readiness data.
        """
        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        pr = make_tracked_pr(issue_key="QR-803")
        pr.pre_merge_review_requested = True
        pr.pre_merge_review_verdict = "approve"
        # After restart: flag lost (defaults to False)
        assert pr.request_changes_posted is False
        monitor._tracked_prs["QR-803"] = pr

        # GitHub still reports CHANGES_REQUESTED
        readiness = MagicMock()
        readiness.is_ready = False
        readiness.review_decision = "CHANGES_REQUESTED"
        readiness.has_failed_checks = False
        readiness.has_pending_checks = False
        readiness.has_unresolved_threads = False
        readiness.mergeable = "MERGEABLE"
        readiness.reasons = ["review: CHANGES_REQUESTED"]

        # After dismiss, re-check returns ready
        readiness_after = MagicMock()
        readiness_after.is_ready = True
        readiness_after.review_decision = "APPROVED"
        readiness_after.has_failed_checks = False
        readiness_after.has_pending_checks = False
        readiness_after.has_unresolved_threads = False
        readiness_after.reasons = []

        mocks["github"].check_merge_readiness = MagicMock(
            side_effect=[readiness, readiness_after],
        )
        mocks["github"].post_review = MagicMock(return_value=True)
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=True,
        )

        status = MagicMock()
        status.state = "OPEN"
        status.head_sha = ""
        status.mergeable = "MERGEABLE"

        await monitor._process_auto_merge("QR-803", pr, status)

        # APPROVE posted to dismiss stale CHANGES_REQUESTED
        mocks["github"].post_review.assert_called_once_with(
            "test",
            "repo",
            1,
            body="Pre-merge review approved — dismissing previous request for changes.",
            event="APPROVE",
        )
        # Auto-merge should have proceeded after dismiss
        mocks["github"].enable_auto_merge.assert_called_once()

    async def test_post_review_sets_request_changes_flag(
        self,
    ) -> None:
        """_post_review_comments sets request_changes_posted flag."""
        from orchestrator.pre_merge_reviewer import (
            ReviewIssue,
            ReviewVerdict,
        )

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        session = AsyncMock(spec=AgentSession)
        result_mock = MagicMock()
        result_mock.proposals = []
        session.send = AsyncMock(return_value=result_mock)
        session.drain_pending_messages = AsyncMock(
            return_value=result_mock,
        )

        pr = make_tracked_pr(
            issue_key="QR-802",
            session=session,
        )
        assert pr.request_changes_posted is False
        monitor._tracked_prs["QR-802"] = pr
        _make_review_ready_mocks(mocks)

        verdict = ReviewVerdict(
            decision="reject",
            summary="Issues",
            issues=(
                ReviewIssue(
                    severity="major",
                    category="quality",
                    file_path="x.py",
                    description="Bad",
                    suggestion="Fix",
                ),
            ),
            confidence=0.8,
            cost_usd=0.05,
            duration_seconds=2.0,
        )
        reviewer.review = AsyncMock(return_value=verdict)
        mocks["github"].post_review = MagicMock(return_value=True)

        await monitor._request_pre_merge_review("QR-802", pr)
        for task in list(monitor._review_tasks.values()):
            await task

        assert pr.request_changes_posted is True

    async def test_request_changes_flag_not_reset_by_review_flags(
        self,
    ) -> None:
        """reset_review_flags does NOT clear request_changes_posted."""
        pr = make_tracked_pr()
        pr.request_changes_posted = True
        pr.reset_review_flags()
        assert pr.request_changes_posted is True

    async def test_lifecycle_reject_fix_approve_merge(
        self,
    ) -> None:
        """Full lifecycle: reject → fix → approve → dismiss → merge.

        This is a scenario test exercising the complete multi-cycle
        flow to prevent state-transition bugs.
        """
        from orchestrator.pre_merge_reviewer import (
            ReviewIssue,
            ReviewVerdict,
        )

        reviewer = AsyncMock()
        monitor, mocks = make_monitor(
            config=make_config(
                auto_merge_enabled=True,
                pre_merge_review_enabled=True,
            ),
            reviewer=reviewer,
        )

        session = AsyncMock(spec=AgentSession)
        result_mock = MagicMock()
        result_mock.proposals = []
        session.send = AsyncMock(return_value=result_mock)
        session.drain_pending_messages = AsyncMock(
            return_value=result_mock,
        )

        pr = make_tracked_pr(
            issue_key="QR-900",
            session=session,
        )
        monitor._tracked_prs["QR-900"] = pr

        # --- Step 1: First review → reject ---
        reject_verdict = ReviewVerdict(
            decision="reject",
            summary="Problems found",
            issues=(
                ReviewIssue(
                    severity="critical",
                    category="bug",
                    file_path="a.py",
                    description="Bug",
                    suggestion="Fix it",
                ),
            ),
            confidence=0.9,
            cost_usd=0.05,
            duration_seconds=2.0,
        )
        reviewer.review = AsyncMock(return_value=reject_verdict)
        mocks["github"].post_review = MagicMock(return_value=True)
        _make_review_ready_mocks(mocks)

        status_open = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
            head_sha="sha_v1",
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=status_open,
        )
        mocks["github"].get_unresolved_threads = MagicMock(
            return_value=[],
        )
        mocks["github"].get_failed_checks = MagicMock(
            return_value=[],
        )

        await monitor._check_all()
        # Wait for review task
        for task in list(monitor._review_tasks.values()):
            await task

        verdict_step1: VerdictType = pr.pre_merge_review_verdict
        assert verdict_step1 == "reject"
        changes_posted: bool = pr.request_changes_posted
        assert changes_posted is True

        # --- Step 2: Worker fixes → new commit resets flags ---
        status_new_commit = PRStatus(
            state="OPEN",
            review_decision="CHANGES_REQUESTED",
            mergeable="MERGEABLE",
            head_sha="sha_v2",
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=status_new_commit,
        )

        await monitor._check_all()

        # review flags reset, but request_changes_posted persists
        assert pr.pre_merge_review_requested is True  # re-requested
        changes_posted = pr.request_changes_posted
        assert changes_posted is True

        # --- Step 3: Re-review approves ---
        approve_verdict = ReviewVerdict(
            decision="approve",
            summary="All good now",
            issues=(),
            confidence=0.95,
            cost_usd=0.03,
            duration_seconds=1.5,
        )
        reviewer.review = AsyncMock(return_value=approve_verdict)

        # Wait for the new review task
        for task in list(monitor._review_tasks.values()):
            await task

        assert pr.pre_merge_review_verdict == "approve"

        # --- Step 4: Next cycle — dismiss + merge ---
        status_after_fix = PRStatus(
            state="OPEN",
            review_decision="APPROVED",
            mergeable="MERGEABLE",
            head_sha="sha_v2",
        )
        mocks["github"].get_pr_status = MagicMock(
            return_value=status_after_fix,
        )

        readiness = MagicMock()
        readiness.is_ready = True
        readiness.review_decision = "APPROVED"
        readiness.has_failed_checks = False
        readiness.has_pending_checks = False
        readiness.has_unresolved_threads = False
        readiness.reasons = []
        mocks["github"].check_merge_readiness = MagicMock(
            return_value=readiness,
        )
        mocks["github"].enable_auto_merge = MagicMock(
            return_value=True,
        )

        await monitor._check_all()

        # APPROVE should have been posted to dismiss stale review
        approve_calls = [
            c
            for c in mocks["github"].post_review.call_args_list
            if c.kwargs.get("event") == "APPROVE" or (len(c.args) > 4 and c.args[4] == "APPROVE")
        ]
        assert len(approve_calls) == 1
        assert pr.request_changes_posted is False

        # Auto-merge should have been enabled
        mocks["github"].enable_auto_merge.assert_called_once()
