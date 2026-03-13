"""Tests for pre-merge code review sub-agent."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.config import Config, ReposConfig
from orchestrator.github_client import PRDetails, PRFile
from orchestrator.pre_merge_reviewer import (
    _MAX_DESCRIPTION_CHARS,
    PreMergeReviewer,
    ReviewIssue,
    ReviewVerdict,
    _safe_float,
    assemble_context,
    load_claude_md,
    parse_verdict,
)
from orchestrator.tracker_client import TrackerIssue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> Config:
    defaults = dict(
        tracker_token="t",
        tracker_org_id="o",
        repos_config=ReposConfig(),
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_pr_details(**overrides) -> PRDetails:
    defaults = dict(
        title="Add login endpoint",
        body="Implements POST /api/v1/auth/login",
        author="bot",
        base_branch="main",
        head_branch="feat/login",
        state="OPEN",
        review_decision="APPROVED",
        additions=50,
        deletions=10,
        changed_files=3,
    )
    defaults.update(overrides)
    return PRDetails(**defaults)


def _make_pr_file(
    filename: str = "src/auth.py",
    patch: str = "@@ -0,0 +1,5 @@\n+def login(): pass",
    **overrides,
) -> PRFile:
    defaults = dict(
        filename=filename,
        status="added",
        additions=5,
        deletions=0,
        patch=patch,
    )
    defaults.update(overrides)
    return PRFile(**defaults)


def _make_issue(
    key: str = "QR-100",
    summary: str = "Implement login",
    description: str = "Add POST /api/v1/auth/login endpoint",
) -> TrackerIssue:
    return TrackerIssue(
        key=key,
        summary=summary,
        description=description,
        components=["Бекенд"],
        tags=["ai-task"],
        status="inProgress",
    )


# ===================================================================
# _safe_float tests
# ===================================================================


class TestSafeFloat:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.95, 0.95),
            ("0.5", 0.5),
            ("high", 0.0),
            (None, 0.0),
            (float("inf"), float("inf")),
        ],
        ids=["float", "numeric_str", "non_numeric_str", "none", "inf"],
    )
    def test_conversions(self, value: object, expected: float) -> None:
        assert _safe_float(value) == expected


# ===================================================================
# ReviewVerdict / ReviewIssue dataclass tests
# ===================================================================


class TestReviewDataclasses:
    def test_review_issue_frozen(self) -> None:
        """ReviewIssue is immutable."""
        issue = ReviewIssue(
            severity="critical",
            category="quality",
            file_path="src/auth.py",
            description="Missing input validation",
            suggestion="Add pydantic model",
        )
        with pytest.raises(AttributeError):
            issue.severity = "minor"  # type: ignore[misc]

    def test_review_verdict_frozen(self) -> None:
        """ReviewVerdict is immutable."""
        verdict = ReviewVerdict(
            decision="approve",
            summary="Looks good",
            issues=(),
            confidence=0.9,
            cost_usd=0.05,
            duration_seconds=3.0,
        )
        with pytest.raises(AttributeError):
            verdict.decision = "reject"  # type: ignore[misc]


# ===================================================================
# _parse_verdict tests
# ===================================================================


class TestParseVerdict:
    def test_valid_approve(self) -> None:
        """Valid approve JSON parses correctly."""
        raw = json.dumps(
            {
                "decision": "approve",
                "summary": "Code looks good",
                "issues": [],
                "confidence": 0.95,
            }
        )
        verdict = parse_verdict(raw, cost_usd=0.05, duration=2.0)

        assert verdict.decision == "approve"
        assert verdict.summary == "Code looks good"
        assert verdict.issues == ()
        assert verdict.confidence == 0.95
        assert verdict.cost_usd == 0.05
        assert verdict.duration_seconds == 2.0

    def test_valid_reject_with_issues(self) -> None:
        """Valid reject JSON with issues parses correctly."""
        raw = json.dumps(
            {
                "decision": "reject",
                "summary": "Found problems",
                "issues": [
                    {
                        "severity": "critical",
                        "category": "contracts",
                        "file_path": "src/api.py",
                        "description": "Using mock instead of real client",
                        "suggestion": "Import the real client",
                    }
                ],
                "confidence": 0.85,
            }
        )
        verdict = parse_verdict(raw, cost_usd=0.10, duration=5.0)

        assert verdict.decision == "reject"
        assert len(verdict.issues) == 1
        assert verdict.issues[0].severity == "critical"
        assert verdict.issues[0].file_path == "src/api.py"

    def test_json_in_markdown_code_block(self) -> None:
        """JSON wrapped in markdown code block still parses."""
        raw = '```json\n{"decision": "approve", "summary": "ok", "issues": [], "confidence": 0.9}\n```'
        verdict = parse_verdict(raw, cost_usd=0.0, duration=1.0)
        assert verdict.decision == "approve"

    def test_fail_open_on_invalid_json(self) -> None:
        """Invalid JSON falls back to approve (fail-open)."""
        verdict = parse_verdict(
            "not valid json at all",
            cost_usd=0.0,
            duration=1.0,
            fail_open=True,
        )
        assert verdict.decision == "approve"
        assert "parse" in verdict.summary.lower()

    def test_fail_close_on_invalid_json(self) -> None:
        """Invalid JSON falls back to reject (fail-close default)."""
        verdict = parse_verdict(
            "not valid json at all",
            cost_usd=0.0,
            duration=1.0,
        )
        assert verdict.decision == "reject"
        assert "parse" in verdict.summary.lower()

    def test_fail_open_on_missing_decision(self) -> None:
        """Missing 'decision' key falls back to approve."""
        raw = json.dumps({"summary": "oops", "issues": []})
        verdict = parse_verdict(raw, cost_usd=0.0, duration=1.0, fail_open=True)
        assert verdict.decision == "approve"

    def test_fail_open_on_invalid_decision_value(self) -> None:
        """Invalid decision value falls back to approve."""
        raw = json.dumps(
            {
                "decision": "maybe",
                "summary": "uncertain",
                "issues": [],
                "confidence": 0.5,
            }
        )
        verdict = parse_verdict(raw, cost_usd=0.0, duration=1.0, fail_open=True)
        assert verdict.decision == "approve"

    def test_non_numeric_confidence_defaults_to_zero(self) -> None:
        """Non-numeric confidence value doesn't discard parsed verdict."""
        raw = json.dumps(
            {
                "decision": "reject",
                "summary": "Found issues",
                "issues": [
                    {"description": "Bad code"},
                ],
                "confidence": "high",
            }
        )
        verdict = parse_verdict(raw, cost_usd=0.10, duration=3.0)
        assert verdict.decision == "reject"
        assert verdict.confidence == 0.0
        assert len(verdict.issues) == 1

    @pytest.mark.parametrize(
        ("raw_confidence", "expected"),
        [
            (5.0, 1.0),
            (-0.5, 0.0),
            (0.85, 0.85),
        ],
        ids=["above_1_clamped", "negative_clamped", "normal_passthrough"],
    )
    def test_confidence_clamped_to_unit_interval(
        self,
        raw_confidence: float,
        expected: float,
    ) -> None:
        """Confidence is clamped to [0.0, 1.0]."""
        raw = json.dumps(
            {
                "decision": "approve",
                "summary": "ok",
                "issues": [],
                "confidence": raw_confidence,
            }
        )
        verdict = parse_verdict(raw, cost_usd=0.0, duration=1.0)
        assert verdict.confidence == expected

    def test_json_after_prose_extracted(self) -> None:
        """JSON preceded by prose text is still extracted."""
        raw = (
            "Here is my review of the PR:\n\n"
            '{"decision": "reject", "summary": "Found bugs", '
            '"issues": [], "confidence": 0.8}'
        )
        verdict = parse_verdict(raw, cost_usd=0.0, duration=1.0)
        assert verdict.decision == "reject"
        assert verdict.summary == "Found bugs"

    def test_json_with_trailing_text_extracted(self) -> None:
        """JSON followed by trailing text is still extracted."""
        raw = (
            '{"decision": "approve", "summary": "ok", '
            '"issues": [], "confidence": 0.9}\n\n'
            "Let me know if you need anything else."
        )
        verdict = parse_verdict(raw, cost_usd=0.0, duration=1.0)
        assert verdict.decision == "approve"

    def test_missing_issue_fields_use_defaults(self) -> None:
        """Issues with missing optional fields get defaults."""
        raw = json.dumps(
            {
                "decision": "reject",
                "summary": "problems",
                "issues": [{"description": "Bad code"}],
                "confidence": 0.7,
            }
        )
        verdict = parse_verdict(raw, cost_usd=0.0, duration=1.0)
        assert len(verdict.issues) == 1
        assert verdict.issues[0].severity == "major"
        assert verdict.issues[0].category == "quality"
        assert verdict.issues[0].file_path == ""
        assert verdict.issues[0].suggestion == ""


# ===================================================================
# _assemble_context tests
# ===================================================================


class TestAssembleContext:
    def test_basic_assembly(self) -> None:
        """Assembles context from PR details, files, and issue."""
        details = _make_pr_details()
        files = [_make_pr_file()]
        issue = _make_issue()

        ctx = assemble_context(
            details=details,
            files=files,
            issue_key="QR-100",
            issue_summary="Implement login",
            issue_description="Add endpoint",
            claude_md="# Style Guide\nFollow PEP8",
        )

        assert "QR-100" in ctx
        assert "Implement login" in ctx
        assert "Add login endpoint" in ctx
        assert "src/auth.py" in ctx
        assert "Style Guide" in ctx

    def test_large_diff_truncation(self) -> None:
        """Files exceeding total limit are truncated."""
        # Create many files with large patches
        files = [
            _make_pr_file(
                filename=f"file_{i}.py",
                patch="+" * 3000,
            )
            for i in range(20)
        ]
        details = _make_pr_details(changed_files=20)

        ctx = assemble_context(
            details=details,
            files=files,
            issue_key="QR-1",
            issue_summary="big change",
            issue_description="lots of files",
            claude_md="",
        )

        # Should contain truncation notice
        assert "truncated" in ctx.lower() or len(ctx) < 60_000

    def test_long_description_truncated(self) -> None:
        """Issue description exceeding limit is truncated."""
        details = _make_pr_details()
        files = [_make_pr_file()]
        long_desc = "x" * (_MAX_DESCRIPTION_CHARS + 500)

        ctx = assemble_context(
            details=details,
            files=files,
            issue_key="QR-1",
            issue_summary="test",
            issue_description=long_desc,
            claude_md="",
        )

        assert "truncated" in ctx.lower()
        # Full description NOT included
        assert long_desc not in ctx

    def test_no_claude_md(self) -> None:
        """Missing CLAUDE.md doesn't break assembly."""
        details = _make_pr_details()
        files = [_make_pr_file()]

        ctx = assemble_context(
            details=details,
            files=files,
            issue_key="QR-1",
            issue_summary="test",
            issue_description="desc",
            claude_md="",
        )

        assert "QR-1" in ctx


# ===================================================================
# PreMergeReviewer.review tests (mocked SDK)
# ===================================================================


class TestPreMergeReviewerReview:
    async def test_review_approve(self) -> None:
        """Successful review returning approve verdict."""
        github = MagicMock()
        tracker = MagicMock()
        config = _make_config()

        github.get_pr_details.return_value = _make_pr_details()
        github.get_pr_files.return_value = [_make_pr_file()]
        tracker.get_issue.return_value = _make_issue()

        reviewer = PreMergeReviewer(github, tracker, config)

        approve_json = json.dumps(
            {
                "decision": "approve",
                "summary": "Code is clean",
                "issues": [],
                "confidence": 0.9,
            }
        )

        with patch.object(
            reviewer,
            "_run_review_agent",
            return_value=(approve_json, 0.05),
        ):
            verdict = await reviewer.review(
                owner="test",
                repo="repo",
                pr_number=1,
                issue_key="QR-100",
                issue_summary="Implement login",
            )

        assert verdict.decision == "approve"
        assert verdict.cost_usd == 0.05

    async def test_review_reject(self) -> None:
        """Review returning reject with issues."""
        github = MagicMock()
        tracker = MagicMock()
        config = _make_config()

        github.get_pr_details.return_value = _make_pr_details()
        github.get_pr_files.return_value = [_make_pr_file()]
        tracker.get_issue.return_value = _make_issue()

        reviewer = PreMergeReviewer(github, tracker, config)

        reject_json = json.dumps(
            {
                "decision": "reject",
                "summary": "Found critical issue",
                "issues": [
                    {
                        "severity": "critical",
                        "category": "contracts",
                        "file_path": "src/auth.py",
                        "description": "Mock instead of real client",
                        "suggestion": "Import real client",
                    }
                ],
                "confidence": 0.85,
            }
        )

        with patch.object(
            reviewer,
            "_run_review_agent",
            return_value=(reject_json, 0.10),
        ):
            verdict = await reviewer.review(
                owner="test",
                repo="repo",
                pr_number=1,
                issue_key="QR-100",
                issue_summary="Implement login",
            )

        assert verdict.decision == "reject"
        assert len(verdict.issues) == 1

    async def test_review_fail_open_on_agent_error(self) -> None:
        """Agent error results in approve (fail-open)."""
        github = MagicMock()
        tracker = MagicMock()
        config = _make_config(pre_merge_review_fail_open=True)

        github.get_pr_details.return_value = _make_pr_details()
        github.get_pr_files.return_value = [_make_pr_file()]
        tracker.get_issue.return_value = _make_issue()

        reviewer = PreMergeReviewer(github, tracker, config)

        with patch.object(
            reviewer,
            "_run_review_agent",
            side_effect=RuntimeError("SDK crash"),
        ):
            verdict = await reviewer.review(
                owner="test",
                repo="repo",
                pr_number=1,
                issue_key="QR-100",
                issue_summary="Implement login",
            )

        assert verdict.decision == "approve"
        assert "error" in verdict.summary.lower()

    async def test_review_fail_open_on_tracker_error(self) -> None:
        """Tracker error fetching issue doesn't block review."""
        github = MagicMock()
        tracker = MagicMock()
        config = _make_config()

        github.get_pr_details.return_value = _make_pr_details()
        github.get_pr_files.return_value = [_make_pr_file()]
        # Tracker fails — review should still work with empty description
        tracker.get_issue.side_effect = Exception("Tracker down")

        reviewer = PreMergeReviewer(github, tracker, config)

        approve_json = json.dumps(
            {
                "decision": "approve",
                "summary": "Looks ok",
                "issues": [],
                "confidence": 0.8,
            }
        )

        with patch.object(
            reviewer,
            "_run_review_agent",
            return_value=(approve_json, 0.03),
        ):
            verdict = await reviewer.review(
                owner="test",
                repo="repo",
                pr_number=1,
                issue_key="QR-100",
                issue_summary="Test task",
            )

        assert verdict.decision == "approve"

    async def test_review_loads_claude_md(self, tmp_path) -> None:
        """Review loads CLAUDE.md from repo paths if available."""
        github = MagicMock()
        tracker = MagicMock()
        config = _make_config()

        github.get_pr_details.return_value = _make_pr_details()
        github.get_pr_files.return_value = [_make_pr_file()]
        tracker.get_issue.return_value = _make_issue()

        reviewer = PreMergeReviewer(github, tracker, config)

        approve_json = json.dumps(
            {
                "decision": "approve",
                "summary": "ok",
                "issues": [],
                "confidence": 0.9,
            }
        )

        # Create a CLAUDE.md
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Project conventions\nUse pytest")

        with patch.object(
            reviewer,
            "_run_review_agent",
            return_value=(approve_json, 0.02),
        ) as mock_agent:
            verdict = await reviewer.review(
                owner="test",
                repo="repo",
                pr_number=1,
                issue_key="QR-100",
                issue_summary="Test",
                repo_paths=[tmp_path],
            )

        assert verdict.decision == "approve"
        # Verify CLAUDE.md content was passed to agent
        call_args = mock_agent.call_args
        prompt = call_args[0][0] if call_args[0] else call_args[1].get("prompt", "")
        assert "Project conventions" in prompt


# ===================================================================
# GitHubClient.post_review tests
# ===================================================================


class TestPostReview:
    def test_post_review_comment(self) -> None:
        """Posts a review comment via REST API."""
        from orchestrator.github_client import GitHubClient

        client = GitHubClient("fake-token")
        client._session = MagicMock()

        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        client._session.post.return_value = resp

        result = client.post_review(
            "owner",
            "repo",
            42,
            body="Looks good overall",
        )

        assert result is True
        client._session.post.assert_called_once()
        call_url = client._session.post.call_args[0][0]
        assert "/repos/owner/repo/pulls/42/reviews" in call_url
        call_json = client._session.post.call_args[1]["json"]
        assert call_json["event"] == "COMMENT"
        assert call_json["body"] == "Looks good overall"

    def test_post_review_request_changes(self) -> None:
        """Posts REQUEST_CHANGES review with inline comments."""
        from orchestrator.github_client import GitHubClient

        client = GitHubClient("fake-token")
        client._session = MagicMock()

        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        client._session.post.return_value = resp

        comments = [
            {
                "path": "src/auth.py",
                "body": "Use real client here",
                "line": 10,
            }
        ]

        result = client.post_review(
            "owner",
            "repo",
            42,
            body="Found issues",
            event="REQUEST_CHANGES",
            comments=comments,
        )

        assert result is True
        call_json = client._session.post.call_args[1]["json"]
        assert call_json["event"] == "REQUEST_CHANGES"
        assert len(call_json["comments"]) == 1

    def test_post_review_failure(self) -> None:
        """Returns False on HTTP error."""
        from requests.exceptions import HTTPError

        from orchestrator.github_client import GitHubClient

        client = GitHubClient("fake-token")
        client._session = MagicMock()

        resp = MagicMock()
        resp.raise_for_status.side_effect = HTTPError(response=resp)
        client._session.post.return_value = resp

        result = client.post_review(
            "owner",
            "repo",
            42,
            body="review",
        )

        assert result is False

    @pytest.mark.parametrize(
        "status_code",
        [401, 403],
        ids=["unauthorized", "forbidden"],
    )
    def test_post_review_auth_error_logged_as_error(
        self,
        status_code: int,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Auth errors (401/403) are logged at ERROR level."""
        import logging

        from requests.exceptions import HTTPError

        from orchestrator.github_client import GitHubClient

        client = GitHubClient("fake-token")
        client._session = MagicMock()

        resp = MagicMock()
        resp.status_code = status_code
        resp.raise_for_status.side_effect = HTTPError(
            response=resp,
        )
        client._session.post.return_value = resp

        with caplog.at_level(
            logging.ERROR,
            logger="orchestrator.github_client",
        ):
            result = client.post_review(
                "owner",
                "repo",
                42,
                body="review",
            )

        assert result is False
        assert any("Auth error" in rec.message and str(status_code) in rec.message for rec in caplog.records)


# ===================================================================
# build_pre_merge_review_prompt tests — dict and object issues
# ===================================================================


class TestBuildPreMergeReviewPrompt:
    def test_handles_dict_issues(self) -> None:
        """Prompt builder handles issues as plain dicts (from event data)."""
        from orchestrator.supervisor_prompt_builder import (
            build_pre_merge_review_prompt,
        )

        issues = [
            {
                "severity": "critical",
                "category": "contracts",
                "file_path": "src/api.py",
                "description": "Mock in production",
            },
        ]
        prompt = build_pre_merge_review_prompt(
            "QR-100",
            "https://github.com/test/repo/pull/1",
            "Found critical issue",
            issues,
        )

        assert "critical/contracts" in prompt
        assert "src/api.py" in prompt
        assert "Mock in production" in prompt

    def test_handles_dataclass_issues(self) -> None:
        """Prompt builder handles issues as ReviewIssue dataclasses."""
        from orchestrator.supervisor_prompt_builder import (
            build_pre_merge_review_prompt,
        )

        issues = [
            ReviewIssue(
                severity="major",
                category="quality",
                file_path="foo.py",
                description="Bad code",
                suggestion="Fix it",
            ),
        ]
        prompt = build_pre_merge_review_prompt(
            "QR-101",
            "https://github.com/test/repo/pull/2",
            "Quality issues",
            issues,
        )

        assert "major/quality" in prompt
        assert "foo.py" in prompt
        assert "Bad code" in prompt

    def test_handles_empty_issues(self) -> None:
        """Prompt builder works with no issues."""
        from orchestrator.supervisor_prompt_builder import (
            build_pre_merge_review_prompt,
        )

        prompt = build_pre_merge_review_prompt(
            "QR-102",
            "https://github.com/test/repo/pull/3",
            "Some summary",
            [],
        )

        assert "QR-102" in prompt
        assert "Issues Found" not in prompt


# ===================================================================
# _load_claude_md tests
# ===================================================================


class TestLoadClaudeMd:
    def test_first_path_without_second_with(self, tmp_path) -> None:
        """Returns content from second path when first has no CLAUDE.md."""
        dir_a = tmp_path / "repo_a"
        dir_a.mkdir()
        # No CLAUDE.md in dir_a

        dir_b = tmp_path / "repo_b"
        dir_b.mkdir()
        (dir_b / "CLAUDE.md").write_text("# From repo B")

        result = load_claude_md([dir_a, dir_b])
        assert result == "# From repo B"

    def test_oserror_falls_back_to_next(self, tmp_path) -> None:
        """OSError on read_text() falls back to the next path."""
        dir_a = tmp_path / "repo_a"
        dir_a.mkdir()
        (dir_a / "CLAUDE.md").write_text("# From repo A")

        dir_b = tmp_path / "repo_b"
        dir_b.mkdir()
        (dir_b / "CLAUDE.md").write_text("# From repo B")

        # Simulate OSError via mock — chmod doesn't work as
        # root in Docker.
        original_read_text = Path.read_text

        def _read_text_oserror(self, *a, **kw):
            if str(self).endswith("repo_a/CLAUDE.md"):
                raise OSError("Permission denied")
            return original_read_text(self, *a, **kw)

        with patch.object(
            Path,
            "read_text",
            _read_text_oserror,
        ):
            result = load_claude_md([dir_a, dir_b])
            assert result == "# From repo B"

    def test_first_path_wins(self, tmp_path) -> None:
        """First path with CLAUDE.md wins over subsequent paths."""
        dir_a = tmp_path / "repo_a"
        dir_a.mkdir()
        (dir_a / "CLAUDE.md").write_text("# From repo A")

        dir_b = tmp_path / "repo_b"
        dir_b.mkdir()
        (dir_b / "CLAUDE.md").write_text("# From repo B")

        result = load_claude_md([dir_a, dir_b])
        assert result == "# From repo A"


# ===================================================================
# Fail-close / fail-open tests (parse_verdict + review)
# ===================================================================


class TestParseVerdictFailClose:
    """Tests for fail-close behavior in parse_verdict."""

    def test_invalid_json_returns_reject_when_fail_close(
        self,
    ) -> None:
        """Invalid JSON returns reject when fail_open=False."""
        verdict = parse_verdict(
            "not valid json",
            cost_usd=0.0,
            duration=1.0,
            fail_open=False,
        )
        assert verdict.decision == "reject"
        assert "parse" in verdict.summary.lower()

    def test_invalid_json_returns_approve_when_fail_open(
        self,
    ) -> None:
        """Invalid JSON returns approve when fail_open=True."""
        verdict = parse_verdict(
            "not valid json",
            cost_usd=0.0,
            duration=1.0,
            fail_open=True,
        )
        assert verdict.decision == "approve"

    def test_invalid_decision_returns_reject_when_fail_close(
        self,
    ) -> None:
        """Invalid decision value returns reject (fail-close)."""
        raw = json.dumps(
            {
                "decision": "maybe",
                "summary": "uncertain",
                "issues": [],
                "confidence": 0.5,
            }
        )
        verdict = parse_verdict(
            raw,
            cost_usd=0.0,
            duration=1.0,
            fail_open=False,
        )
        assert verdict.decision == "reject"

    def test_invalid_decision_returns_approve_when_fail_open(
        self,
    ) -> None:
        """Invalid decision value returns approve (fail-open)."""
        raw = json.dumps(
            {
                "decision": "maybe",
                "summary": "uncertain",
                "issues": [],
                "confidence": 0.5,
            }
        )
        verdict = parse_verdict(
            raw,
            cost_usd=0.0,
            duration=1.0,
            fail_open=True,
        )
        assert verdict.decision == "approve"

    def test_missing_decision_returns_reject_when_fail_close(
        self,
    ) -> None:
        """Missing 'decision' key returns reject (fail-close)."""
        raw = json.dumps({"summary": "oops", "issues": []})
        verdict = parse_verdict(
            raw,
            cost_usd=0.0,
            duration=1.0,
            fail_open=False,
        )
        assert verdict.decision == "reject"


class TestReviewerFailClose:
    """Tests for fail-close in PreMergeReviewer.review."""

    async def test_agent_error_returns_reject_when_fail_close(
        self,
    ) -> None:
        """Agent exception returns reject when fail_open=False."""
        github = MagicMock()
        tracker = MagicMock()
        config = _make_config(pre_merge_review_fail_open=False)

        github.get_pr_details.return_value = _make_pr_details()
        github.get_pr_files.return_value = [_make_pr_file()]
        tracker.get_issue.return_value = _make_issue()

        reviewer = PreMergeReviewer(github, tracker, config)

        with patch.object(
            reviewer,
            "_run_review_agent",
            side_effect=RuntimeError("SDK crash"),
        ):
            verdict = await reviewer.review(
                owner="test",
                repo="repo",
                pr_number=1,
                issue_key="QR-100",
                issue_summary="Test",
            )

        assert verdict.decision == "reject"
        assert "error" in verdict.summary.lower()

    async def test_agent_error_returns_approve_when_fail_open(
        self,
    ) -> None:
        """Agent exception returns approve when fail_open=True."""
        github = MagicMock()
        tracker = MagicMock()
        config = _make_config(pre_merge_review_fail_open=True)

        github.get_pr_details.return_value = _make_pr_details()
        github.get_pr_files.return_value = [_make_pr_file()]
        tracker.get_issue.return_value = _make_issue()

        reviewer = PreMergeReviewer(github, tracker, config)

        with patch.object(
            reviewer,
            "_run_review_agent",
            side_effect=RuntimeError("SDK crash"),
        ):
            verdict = await reviewer.review(
                owner="test",
                repo="repo",
                pr_number=1,
                issue_key="QR-100",
                issue_summary="Test",
            )

        assert verdict.decision == "approve"
        assert "error" in verdict.summary.lower()


# ===================================================================
# Security criteria in review prompt
# ===================================================================


class TestSecurityInReviewPrompt:
    def test_security_category_in_review_prompt(self) -> None:
        """Review prompt includes 'security' as a valid category."""
        from orchestrator.pre_merge_reviewer import (
            _REVIEW_SYSTEM_PROMPT,
        )

        assert "security" in _REVIEW_SYSTEM_PROMPT.lower()

    @pytest.mark.parametrize(
        "keyword",
        [
            "sql injection",
            "xss",
            "command injection",
            "hardcoded secret",
            "deserialization",
            "input validation",
            "path traversal",
        ],
        ids=[
            "sql_injection",
            "xss",
            "command_injection",
            "hardcoded_secrets",
            "insecure_deserialization",
            "input_validation",
            "path_traversal",
        ],
    )
    def test_owasp_criteria_in_prompt(
        self,
        keyword: str,
    ) -> None:
        """Review prompt contains OWASP security criteria."""
        from orchestrator.pre_merge_reviewer import (
            _REVIEW_SYSTEM_PROMPT,
        )

        assert keyword in _REVIEW_SYSTEM_PROMPT.lower()

    def test_security_issue_in_verdict(self) -> None:
        """Security category is accepted in parsed verdict."""
        raw = json.dumps(
            {
                "decision": "reject",
                "summary": "SQL injection found",
                "issues": [
                    {
                        "severity": "critical",
                        "category": "security",
                        "file_path": "src/db.py",
                        "description": "String concat in query",
                        "suggestion": "Use parameterized queries",
                    }
                ],
                "confidence": 0.95,
            }
        )
        verdict = parse_verdict(
            raw,
            cost_usd=0.1,
            duration=2.0,
        )
        assert verdict.decision == "reject"
        assert verdict.issues[0].category == "security"
