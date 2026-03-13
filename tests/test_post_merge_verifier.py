"""Tests for post-merge verification sub-agent."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.config import Config, ReposConfig
from orchestrator.constants import EventType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> Config:
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(),
        post_merge_verification_enabled=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_verifier(
    config: Config | None = None,
    github: MagicMock | None = None,
    tracker: MagicMock | None = None,
    k8s_client: MagicMock | None = None,
    storage: AsyncMock | None = None,
    event_bus: AsyncMock | None = None,
):
    """Build a PostMergeVerifier with mocked dependencies."""
    from orchestrator.post_merge_verifier import PostMergeVerifier

    return PostMergeVerifier(
        github=github or MagicMock(),
        tracker=tracker or MagicMock(),
        k8s_client=k8s_client or MagicMock(),
        storage=storage or AsyncMock(),
        config=config or _make_config(),
        event_bus=event_bus or AsyncMock(),
    )


# ===================================================================
# Dataclass tests
# ===================================================================


class TestVerificationResult:
    def test_fields(self) -> None:
        from orchestrator.post_merge_verifier import (
            VerificationResult,
        )

        result = VerificationResult(
            decision="pass",
            summary="All checks green",
            checks_passed=True,
            k8s_ready=True,
            cost_usd=0.05,
            duration_seconds=120.0,
        )
        assert result.decision == "pass"
        assert result.checks_passed is True
        assert result.k8s_ready is True
        assert result.cost_usd == 0.05

    def test_frozen(self) -> None:
        from orchestrator.post_merge_verifier import (
            VerificationResult,
        )

        result = VerificationResult(
            decision="fail",
            summary="broken",
            checks_passed=False,
            k8s_ready=False,
            cost_usd=0.0,
            duration_seconds=0.0,
        )
        with pytest.raises(AttributeError):
            result.decision = "pass"  # type: ignore[misc]


class TestVerificationIssue:
    def test_fields(self) -> None:
        from orchestrator.post_merge_verifier import (
            VerificationIssue,
        )

        issue = VerificationIssue(
            category="api",
            description="500 on /health",
            evidence="HTTP 500 Internal Server Error",
        )
        assert issue.category == "api"
        assert issue.evidence == "HTTP 500 Internal Server Error"


# ===================================================================
# parse_verification_result tests
# ===================================================================


class TestParseVerificationResult:
    def test_valid_pass(self) -> None:
        from orchestrator.post_merge_verifier import (
            parse_verification_result,
        )

        raw = json.dumps(
            {
                "decision": "pass",
                "summary": "All endpoints respond 200",
                "issues": [],
            }
        )
        result = parse_verification_result(raw, 0.05, 60.0)
        assert result.decision == "pass"
        assert result.summary == "All endpoints respond 200"
        assert result.checks_passed is True
        assert result.k8s_ready is True
        assert result.cost_usd == 0.05

    def test_valid_fail_with_issues(self) -> None:
        from orchestrator.post_merge_verifier import (
            parse_verification_result,
        )

        raw = json.dumps(
            {
                "decision": "fail",
                "summary": "Health check broken",
                "issues": [
                    {
                        "category": "api",
                        "description": "500 on /health",
                        "evidence": "HTTP 500",
                    }
                ],
            }
        )
        result = parse_verification_result(raw, 0.1, 30.0)
        assert result.decision == "fail"
        assert len(result.issues) == 1
        assert result.issues[0].category == "api"

    def test_invalid_json_returns_skip(self) -> None:
        from orchestrator.post_merge_verifier import (
            parse_verification_result,
        )

        result = parse_verification_result("not json", 0.0, 10.0)
        assert result.decision == "skip"
        assert "parse error" in result.summary.lower()

    def test_invalid_decision_returns_skip(self) -> None:
        from orchestrator.post_merge_verifier import (
            parse_verification_result,
        )

        raw = json.dumps({"decision": "maybe", "summary": "x"})
        result = parse_verification_result(raw, 0.0, 10.0)
        assert result.decision == "skip"

    def test_markdown_fence_extraction(self) -> None:
        from orchestrator.post_merge_verifier import (
            parse_verification_result,
        )

        raw = 'Here is my result:\n```json\n{"decision": "pass", "summary": "ok", "issues": []}\n```'
        result = parse_verification_result(raw, 0.0, 5.0)
        assert result.decision == "pass"


# ===================================================================
# PostMergeVerifier.verify tests
# ===================================================================


class TestVerifySkipsWhenDisabled:
    @pytest.mark.asyncio
    async def test_returns_skip(self) -> None:
        config = _make_config(
            post_merge_verification_enabled=False,
        )
        verifier = _make_verifier(config=config)
        result = await verifier.verify(
            issue_key="QR-1",
            owner="org",
            repo="repo",
            pr_number=42,
            merge_sha="abc123",
            issue_summary="Fix bug",
            issue_description="Fix the bug",
        )
        assert result.decision == "skip"
        assert "disabled" in result.summary.lower()


class TestVerifyWaitsForCIGreen:
    @pytest.mark.asyncio
    async def test_ci_passes(self) -> None:
        github = MagicMock()
        # First call: in progress, second call: success
        check_in_progress = MagicMock(name="ci", conclusion="", status="IN_PROGRESS")
        check_success = MagicMock(name="ci", conclusion="SUCCESS", status="COMPLETED")
        github.get_commit_check_runs.side_effect = [
            [check_in_progress],
            [check_success],
        ]

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=False,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            storage=storage,
        )

        pass_json = json.dumps(
            {
                "decision": "pass",
                "summary": "ok",
                "issues": [],
            }
        )

        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.05),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Fix it",
            )
        assert result.decision == "pass"
        assert result.checks_passed is True


class TestVerifyCITimeoutReturnsSkip:
    @pytest.mark.asyncio
    async def test_ci_timeout(self) -> None:
        github = MagicMock()
        # Always in progress — will timeout
        check_pending = MagicMock(name="ci", conclusion="", status="IN_PROGRESS")
        github.get_commit_check_runs.return_value = [
            check_pending,
        ]

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_ci_timeout_seconds=0,
            post_merge_wait_for_k8s=False,
        )
        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }
        verifier = _make_verifier(
            config=config,
            github=github,
            storage=storage,
        )

        pass_json = json.dumps(
            {
                "decision": "pass",
                "summary": "ok",
                "issues": [],
            }
        )

        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.0),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )
        # CI timeout means checks_passed=False but we still
        # proceed with verification (skip would only happen
        # if the agent itself fails).
        assert result.checks_passed is False


def _make_monotonic_clock(step: float = 100.0):
    """Return a fake monotonic clock advancing by *step*."""
    t = 0.0

    def _monotonic():
        nonlocal t
        t += step
        return t

    return _monotonic


class TestVerifyCIEarlyExitOnFailure:
    @pytest.mark.asyncio
    async def test_ci_terminal_failure_exits_early(self) -> None:
        """_wait_for_ci should exit early on FAILURE, not wait full timeout."""
        github = MagicMock()
        check_fail = MagicMock(
            name="ci",
            conclusion="FAILURE",
            status="COMPLETED",
        )
        github.get_commit_check_runs.return_value = [check_fail]

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_ci_timeout_seconds=600,
            post_merge_wait_for_k8s=False,
        )
        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }
        verifier = _make_verifier(
            config=config,
            github=github,
            storage=storage,
        )

        pass_json = json.dumps(
            {
                "decision": "pass",
                "summary": "ok",
                "issues": [],
            }
        )

        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.0),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )
        # Should exit on first poll — no sleep needed
        mock_sleep.assert_not_called()
        assert result.checks_passed is False


class TestVerifyCIMixedCompletedAndPending:
    @pytest.mark.asyncio
    async def test_waits_when_some_checks_still_pending(self) -> None:
        """Should keep polling when some checks complete but others pending."""
        github = MagicMock()
        # First poll: one SUCCESS, one IN_PROGRESS
        mixed = [
            MagicMock(name="tests", conclusion="SUCCESS", status="COMPLETED"),
            MagicMock(name="deploy", conclusion="", status="IN_PROGRESS"),
        ]
        # Second poll: both SUCCESS
        all_pass = [
            MagicMock(name="tests", conclusion="SUCCESS", status="COMPLETED"),
            MagicMock(name="deploy", conclusion="SUCCESS", status="COMPLETED"),
        ]
        github.get_commit_check_runs.side_effect = [mixed, all_pass]

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=False,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            storage=storage,
        )

        pass_json = json.dumps(
            {"decision": "pass", "summary": "ok", "issues": []},
        )
        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.05),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )
        assert result.decision == "pass"
        assert result.checks_passed is True
        # Should have polled twice
        assert github.get_commit_check_runs.call_count == 2


class TestVerifyCISkippedConclusion:
    @pytest.mark.asyncio
    async def test_skipped_checks_treated_as_acceptable(self) -> None:
        """SKIPPED conclusion should be treated as acceptable, not block."""
        github = MagicMock()
        checks = [
            MagicMock(
                name="tests",
                conclusion="SUCCESS",
                status="COMPLETED",
            ),
            MagicMock(
                name="optional",
                conclusion="SKIPPED",
                status="COMPLETED",
            ),
        ]
        github.get_commit_check_runs.return_value = checks

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=False,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            storage=storage,
        )

        pass_json = json.dumps(
            {"decision": "pass", "summary": "ok", "issues": []},
        )
        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.05),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )
        assert result.checks_passed is True
        # Should pass on first poll — no extra polls
        assert github.get_commit_check_runs.call_count == 1


class TestVerifyEnvironmentConfigurable:
    @pytest.mark.asyncio
    async def test_uses_configured_environment(self) -> None:
        """Verification should use post_merge_verification_environment."""
        github = MagicMock()
        check_ok = MagicMock(
            name="ci",
            conclusion="SUCCESS",
            status="COMPLETED",
        )
        github.get_commit_check_runs.return_value = [check_ok]

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "staging",
            "config": {"base_url": "https://staging.example.com"},
        }

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=False,
            post_merge_verification_environment="staging",
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            storage=storage,
        )

        pass_json = json.dumps(
            {"decision": "pass", "summary": "ok", "issues": []},
        )
        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.05),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )
        assert result.decision == "pass"
        # Verify storage was called with "staging", not "dev"
        storage.get_environment.assert_called_once_with("staging")


class TestVerifyWaitsForK8sReady:
    @pytest.mark.asyncio
    async def test_k8s_ready(self) -> None:
        github = MagicMock()
        check_ok = MagicMock(name="ci", conclusion="SUCCESS", status="COMPLETED")
        github.get_commit_check_runs.return_value = [check_ok]

        k8s = MagicMock()
        pod_ready = MagicMock()
        pod_ready.phase = "Running"
        pod_ready.name = "myapp-abc"
        container = MagicMock()
        container.ready = True
        pod_ready.containers = [container]
        # Always ready — first call succeeds
        k8s.list_pods.return_value = [pod_ready]
        k8s.available = True

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=True,
            post_merge_k8s_deployment="myapp",
            post_merge_k8s_timeout_seconds=600,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            k8s_client=k8s,
            storage=storage,
        )

        pass_json = json.dumps(
            {
                "decision": "pass",
                "summary": "ok",
                "issues": [],
            }
        )
        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.05),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )
        assert result.decision == "pass"
        assert result.k8s_ready is True


class TestVerifyK8sTimeoutReturnsSkip:
    @pytest.mark.asyncio
    async def test_k8s_timeout(self) -> None:
        github = MagicMock()
        check_ok = MagicMock(name="ci", conclusion="SUCCESS", status="COMPLETED")
        github.get_commit_check_runs.return_value = [check_ok]

        k8s = MagicMock()
        pod_pending = MagicMock()
        pod_pending.phase = "Pending"
        pod_pending.name = "myapp-xyz"
        pod_pending.containers = []
        k8s.list_pods.return_value = [pod_pending]
        k8s.available = True

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=True,
            post_merge_k8s_deployment="myapp",
            post_merge_k8s_timeout_seconds=0,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            k8s_client=k8s,
            storage=storage,
        )

        pass_json = json.dumps(
            {
                "decision": "pass",
                "summary": "ok",
                "issues": [],
            }
        )
        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.0),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )
        assert result.k8s_ready is False


class TestVerifyPassPublishesEvent:
    @pytest.mark.asyncio
    async def test_publishes_task_verified(self) -> None:
        github = MagicMock()
        check_ok = MagicMock(name="ci", conclusion="SUCCESS", status="COMPLETED")
        github.get_commit_check_runs.return_value = [check_ok]

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        event_bus = AsyncMock()

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=False,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            storage=storage,
            event_bus=event_bus,
        )

        pass_json = json.dumps(
            {
                "decision": "pass",
                "summary": "ok",
                "issues": [],
            }
        )
        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.05),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )
        assert result.decision == "pass"
        event_bus.publish.assert_called_once()
        event = event_bus.publish.call_args[0][0]
        assert event.type == EventType.TASK_VERIFIED
        assert event.task_key == "QR-1"


class TestVerifyFailCreatesHotfixTask:
    @pytest.mark.asyncio
    async def test_creates_tracker_issue(self) -> None:
        github = MagicMock()
        check_ok = MagicMock(name="ci", conclusion="SUCCESS", status="COMPLETED")
        github.get_commit_check_runs.return_value = [check_ok]

        tracker = MagicMock()
        tracker.create_issue.return_value = {
            "key": "QR-99",
        }

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        event_bus = AsyncMock()

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=False,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            tracker=tracker,
            storage=storage,
            event_bus=event_bus,
        )

        fail_json = json.dumps(
            {
                "decision": "fail",
                "summary": "Health check returns 500",
                "issues": [
                    {
                        "category": "api",
                        "description": "/health returns 500",
                        "evidence": "HTTP 500",
                    }
                ],
            }
        )
        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(fail_json, 0.1),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix bug",
                issue_description="Fix the bug",
            )
        assert result.decision == "fail"

        # Should create a hotfix task
        tracker.create_issue.assert_called_once()
        call_kwargs = tracker.create_issue.call_args
        assert call_kwargs[1]["issue_type"] == 1  # Bug
        assert "QR-1" in call_kwargs[0][1]  # summary

        # Should publish VERIFICATION_FAILED event
        event_bus.publish.assert_called_once()
        event = event_bus.publish.call_args[0][0]
        assert event.type == EventType.VERIFICATION_FAILED


class TestVerificationAgentTimeout:
    """Bug: verification agent timeout reuses CI timeout config."""

    @pytest.mark.asyncio
    async def test_uses_dedicated_timeout(self) -> None:
        """Agent should use post_merge_verification_timeout_seconds,
        not post_merge_ci_timeout_seconds."""
        github = MagicMock()
        check_ok = MagicMock(
            name="ci",
            conclusion="SUCCESS",
            status="COMPLETED",
        )
        github.get_commit_check_runs.return_value = [check_ok]

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=False,
            post_merge_ci_timeout_seconds=600,
            post_merge_verification_timeout_seconds=120,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            storage=storage,
        )

        pass_json = json.dumps(
            {
                "decision": "pass",
                "summary": "ok",
                "issues": [],
            }
        )

        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.05),
            ) as mock_agent,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("asyncio.wait_for", wraps=asyncio.wait_for) as mock_wait,
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )

        assert result.decision == "pass"
        # Check that wait_for was called with the dedicated
        # timeout (120), not the CI timeout (600)
        for call in mock_wait.call_args_list:
            if call[1].get("timeout") is not None:
                assert call[1]["timeout"] == 120


class TestK8sPodMatchingPrecision:
    """Bug: substring match on pod names causes false matches."""

    @pytest.mark.asyncio
    async def test_does_not_match_pods_with_substring_overlap(
        self,
    ) -> None:
        """deployment='myapp' must not match pod 'myappx-abc123'."""
        github = MagicMock()
        check_ok = MagicMock(
            name="ci",
            conclusion="SUCCESS",
            status="COMPLETED",
        )
        github.get_commit_check_runs.return_value = [check_ok]

        k8s = MagicMock()

        # Pod whose name contains "myapp" but belongs to
        # a different deployment "myappx"
        pod_other = MagicMock()
        pod_other.name = "myappx-abc123"
        pod_other.phase = "Running"
        container = MagicMock()
        container.ready = True
        pod_other.containers = [container]

        k8s.list_pods.return_value = [pod_other]
        k8s.available = True

        storage = AsyncMock()
        storage.get_environment.return_value = {
            "name": "dev",
            "config": {"base_url": "http://localhost"},
        }

        config = _make_config(
            post_merge_verification_enabled=True,
            post_merge_wait_for_k8s=True,
            post_merge_k8s_deployment="myapp",
            post_merge_k8s_timeout_seconds=1,
        )
        verifier = _make_verifier(
            config=config,
            github=github,
            k8s_client=k8s,
            storage=storage,
        )

        pass_json = json.dumps(
            {
                "decision": "pass",
                "summary": "ok",
                "issues": [],
            }
        )

        with (
            patch.object(
                verifier,
                "_run_verification_agent",
                return_value=(pass_json, 0.05),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await verifier.verify(
                issue_key="QR-1",
                owner="org",
                repo="repo",
                pr_number=42,
                merge_sha="abc123",
                issue_summary="Fix",
                issue_description="Desc",
            )

        # myappx pods should NOT match deployment "myapp"
        # so k8s_ready should be False (timeout, no matching pods)
        assert result.k8s_ready is False


class TestAssembleContext:
    @pytest.mark.asyncio
    async def test_includes_env_config(self) -> None:
        from orchestrator.post_merge_verifier import (
            assemble_verification_context,
        )

        env_config = {
            "base_url": "https://api.example.com",
            "api_key": "test-key",
        }
        ctx = assemble_verification_context(
            issue_key="QR-1",
            issue_summary="Add login",
            issue_description="Implement login endpoint",
            merge_sha="abc123",
            env_config=env_config,
        )
        assert "QR-1" in ctx
        assert "Add login" in ctx
        assert "abc123" in ctx
        assert "https://api.example.com" in ctx
        assert "Environment" in ctx

    @pytest.mark.asyncio
    async def test_no_env_config(self) -> None:
        from orchestrator.post_merge_verifier import (
            assemble_verification_context,
        )

        ctx = assemble_verification_context(
            issue_key="QR-1",
            issue_summary="Fix",
            issue_description="Fix it",
            merge_sha="abc",
            env_config=None,
        )
        assert "QR-1" in ctx
        assert "No environment" in ctx
