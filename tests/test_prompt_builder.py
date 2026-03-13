"""Tests for prompt_builder module."""

import pytest

from orchestrator.config import RepoInfo
from orchestrator.github_client import ReviewThread, ThreadComment
from orchestrator.prompt_builder import (
    build_fallback_context_prompt,
    build_merge_conflict_prompt,
    build_needs_info_response_prompt,
    build_pre_merge_rejection_prompt,
    build_review_prompt,
    build_system_prompt_append,
    build_task_prompt,
)
from orchestrator.tracker_client import TrackerIssue


def _make_issue(**overrides) -> TrackerIssue:
    defaults = dict(
        key="QR-125",
        summary="Add login feature",
        description="Implement OAuth2 login",
        components=["Бекенд"],
        tags=["ai-task"],
        status="open",
    )
    defaults.update(overrides)
    return TrackerIssue(**defaults)


def _make_thread(
    thread_id: str = "T_1",
    path: str | None = "src/main.py",
    line: int | None = 42,
    comments: list[ThreadComment] | None = None,
) -> ReviewThread:
    if comments is None:
        comments = [
            ThreadComment(
                author="reviewer",
                body="Fix this",
                created_at="2025-01-01T00:00:00Z",
            )
        ]
    return ReviewThread(
        id=thread_id,
        is_resolved=False,
        path=path,
        line=line,
        comments=comments,
    )


class TestBuildTaskPrompt:
    @pytest.mark.parametrize(
        "expected_text",
        [
            "QR-125",
            "Add login feature",
            "Implement OAuth2 login",
            "TDD",
            "Russian",
        ],
        ids=[
            "issue_key",
            "summary",
            "description",
            "tdd_instruction",
            "russian_note",
        ],
    )
    def test_contains_expected_text(self, expected_text) -> None:
        prompt = build_task_prompt(_make_issue())
        assert expected_text in prompt

    def test_no_retry_context_in_prompt(self) -> None:
        prompt = build_task_prompt(_make_issue())
        assert "Retry Context" not in prompt

    def test_repo_catalog_shown_with_all_repos(self) -> None:
        all_repos = [
            RepoInfo(
                url="https://github.com/org/back.git",
                path="/ws/back",
                description="Go backend",
            ),
            RepoInfo(
                url="https://github.com/org/front.git",
                path="/ws/front",
                description="React frontend",
            ),
        ]
        prompt = build_task_prompt(_make_issue(), all_repos=all_repos)
        assert "Available repositories" in prompt
        assert "Go backend" in prompt
        assert "React frontend" in prompt

    def test_repo_catalog_omitted_without_all_repos(self) -> None:
        prompt = build_task_prompt(_make_issue(), all_repos=None)
        assert "Available repositories" not in prompt

    def test_workspace_tool_instructions(self) -> None:
        all_repos = [
            RepoInfo(
                url="https://github.com/org/back.git",
                path="/ws/back",
                description="Go backend",
            ),
        ]
        prompt = build_task_prompt(_make_issue(), all_repos=all_repos)
        assert "request_worktree" in prompt
        assert "list_available_repos" in prompt


class TestBuildTaskPromptWithPeers:
    def test_peers_section_present(self) -> None:
        from orchestrator.prompt_builder import PeerInfo

        peers = [
            PeerInfo(
                task_key="QR-207",
                summary="Backend auth API",
                status="running",
            ),
        ]
        prompt = build_task_prompt(_make_issue(), peers=peers)
        assert "Running Peer Agents" in prompt
        assert "QR-207" in prompt
        assert "Backend auth API" in prompt

    @pytest.mark.parametrize(
        "peers",
        [None, []],
        ids=["none", "empty_list"],
    )
    def test_no_peers_section(self, peers) -> None:
        prompt = build_task_prompt(_make_issue(), peers=peers)
        assert "Running Peer Agents" not in prompt

    def test_multiple_peers_listed(self) -> None:
        from orchestrator.prompt_builder import PeerInfo

        peers = [
            PeerInfo(
                task_key="QR-207",
                summary="Backend auth API",
                status="running",
            ),
            PeerInfo(
                task_key="QR-208",
                summary="Frontend login page",
                status="running",
            ),
        ]
        prompt = build_task_prompt(_make_issue(), peers=peers)
        assert "QR-207" in prompt
        assert "QR-208" in prompt
        assert "Frontend login page" in prompt


class TestBuildReviewPrompt:
    @pytest.mark.parametrize(
        "expected_text",
        [
            "QR-10",
            "https://github.com/org/repo/pull/1",
            "fix(QR-10):",
            "Verify via test",
            "gh api",
        ],
        ids=[
            "issue_key",
            "pr_url",
            "commit_msg",
            "tdd_instruction",
            "reply_instruction",
        ],
    )
    def test_contains_expected_text(self, expected_text) -> None:
        prompt = build_review_prompt(
            "QR-10",
            "https://github.com/org/repo/pull/1",
            [_make_thread()],
        )
        assert expected_text in prompt

    def test_contains_thread_count(self) -> None:
        threads = [_make_thread("T_1"), _make_thread("T_2")]
        prompt = build_review_prompt(
            "QR-10",
            "https://github.com/org/repo/pull/1",
            threads,
        )
        assert "2 unresolved review conversation(s)" in prompt

    def test_contains_file_location(self) -> None:
        prompt = build_review_prompt(
            "QR-10",
            "https://github.com/org/repo/pull/1",
            [_make_thread(path="foo.py", line=10)],
        )
        assert "`foo.py:10`" in prompt

    def test_contains_comment_body(self) -> None:
        comments = [
            ThreadComment(
                author="alice",
                body="Please refactor",
                created_at="2025-01-01T00:00:00Z",
            )
        ]
        thread = _make_thread(comments=comments)
        prompt = build_review_prompt(
            "QR-10",
            "https://github.com/org/repo/pull/1",
            [thread],
        )
        assert "Please refactor" in prompt
        assert "**alice**" in prompt

    def test_no_path_omits_location(self) -> None:
        prompt = build_review_prompt(
            "QR-10",
            "https://github.com/org/repo/pull/1",
            [_make_thread(path=None, line=None)],
        )
        assert "Conversation 1\n" in prompt


class TestBuildNeedsInfoResponsePrompt:
    def test_contains_issue_key(self) -> None:
        comments = [
            {
                "createdBy": {"display": "John"},
                "text": "Here is the info",
                "createdAt": "2025-06-01",
            }
        ]
        prompt = build_needs_info_response_prompt("QR-10", comments)
        assert "QR-10" in prompt

    def test_contains_comment_text(self) -> None:
        comments = [
            {
                "createdBy": {"display": "John"},
                "text": "The API uses OAuth2",
                "createdAt": "2025-06-01",
            }
        ]
        prompt = build_needs_info_response_prompt("QR-10", comments)
        assert "The API uses OAuth2" in prompt
        assert "**John**" in prompt

    def test_multiple_comments(self) -> None:
        comments = [
            {
                "createdBy": {"display": "Alice"},
                "text": "First answer",
                "createdAt": "2025-06-01",
            },
            {
                "createdBy": {"display": "Bob"},
                "text": "Second answer",
                "createdAt": "2025-06-02",
            },
        ]
        prompt = build_needs_info_response_prompt("QR-10", comments)
        assert "First answer" in prompt
        assert "Second answer" in prompt
        assert "**Alice**" in prompt
        assert "**Bob**" in prompt

    def test_contains_instructions(self) -> None:
        comments = [
            {
                "createdBy": {"display": "John"},
                "text": "Info",
                "createdAt": "2025-06-01",
            }
        ]
        prompt = build_needs_info_response_prompt("QR-10", comments)
        assert "tracker_request_info" in prompt
        assert "Russian" in prompt


class TestBuildSystemPromptAppend:
    def test_loads_file(self, tmp_path) -> None:
        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow\nFollow TDD.")

        result = build_system_prompt_append(wf)
        assert "Follow TDD" in result

    def test_returns_empty_for_missing(self, tmp_path) -> None:
        result = build_system_prompt_append(tmp_path / "nonexistent.md")
        assert result == ""

    def test_bundles_plan_agent_md(self, tmp_path) -> None:
        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow\nDo stuff.")
        plan = tmp_path / "plan_agent.md"
        plan.write_text("# Planning Agent\nYou are a planner.")

        result = build_system_prompt_append(wf)
        assert "Do stuff" in result
        assert "You are a planner" in result
        assert "Planning Agent Prompt" in result

    def test_works_without_plan_agent_md(self, tmp_path) -> None:
        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow\nJust workflow.")

        result = build_system_prompt_append(wf)
        assert "Just workflow" in result
        assert "Planning Agent Prompt" not in result

    def test_real_plan_agent_bundled(self) -> None:
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent / "prompts" / "workflow.md"
        result = build_system_prompt_append(workflow_path)
        assert "Planning Agent Instructions" in result
        assert "READ-ONLY MODE" in result

    def test_bundles_simplify_agent_md(self, tmp_path) -> None:
        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow\nDo stuff.")
        simplify = tmp_path / "simplify_agent.md"
        simplify.write_text("# Simplify Agent\nYou review code.")

        result = build_system_prompt_append(wf)
        assert "You review code" in result
        assert "Simplify Agent Prompt" in result

    def test_works_without_simplify_agent_md(
        self,
        tmp_path,
    ) -> None:
        wf = tmp_path / "workflow.md"
        wf.write_text("# Workflow\nJust workflow.")

        result = build_system_prompt_append(wf)
        assert "Simplify Agent Prompt" not in result

    def test_real_simplify_agent_bundled(self) -> None:
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent / "prompts" / "workflow.md"
        result = build_system_prompt_append(workflow_path)
        assert "Simplify Agent Instructions" in result
        assert "Code Reuse" in result
        assert "Code Quality" in result
        assert "Efficiency" in result


class TestBuildMergeConflictPrompt:
    _URL = "https://github.com/org/repo/pull/1"

    @pytest.mark.parametrize(
        "expected_text",
        [
            "QR-10",
            "https://github.com/org/repo/pull/1",
            "rebase",
            "force-with-lease",
            "Russian",
        ],
        ids=[
            "issue_key",
            "pr_url",
            "rebase_instruction",
            "force_push",
            "russian_note",
        ],
    )
    def test_contains_expected_text(self, expected_text) -> None:
        prompt = build_merge_conflict_prompt("QR-10", self._URL)
        assert expected_text in prompt


class TestWorkflowNeedsInfoCommitInstructions:
    """Verify workflow.md instructs the agent to commit before needs-info."""

    @pytest.fixture
    def workflow_content(self) -> str:
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent / "prompts" / "workflow.md"
        return build_system_prompt_append(workflow_path)

    def test_requesting_info_section_mentions_git_commit(self, workflow_content) -> None:
        assert "git add" in workflow_content
        assert "git push" in workflow_content

    def test_requesting_info_section_commit_before_tracker_request_info(self, workflow_content) -> None:
        section_start = workflow_content.index("## Requesting Information")
        section = workflow_content[section_start:]

        git_add_pos = section.lower().index("git add")
        git_push_pos = section.lower().index("git push")
        tracker_pos = section.index("tracker_request_info")

        assert git_add_pos < tracker_pos
        assert git_push_pos < tracker_pos

    def test_blockers_section_mentions_commit(self, workflow_content) -> None:
        blockers_idx = workflow_content.index("If the plan has blockers")
        next_section = workflow_content.find("##", blockers_idx + 1)
        if next_section == -1:
            next_section = blockers_idx + 1000
        section = workflow_content[blockers_idx:next_section]

        assert "git add" in section.lower()
        assert "git push" in section.lower()

        git_add_pos = section.lower().index("git add")
        git_push_pos = section.lower().index("git push")
        tracker_pos = section.index("tracker_request_info")

        assert git_add_pos < tracker_pos
        assert git_push_pos < tracker_pos


class TestBuildPreMergeRejectionPrompt:
    """Tests for build_pre_merge_rejection_prompt."""

    _URL = "https://github.com/org/repo/pull/42"

    def _make_verdict(self):
        from orchestrator.pre_merge_reviewer import (
            ReviewIssue,
            ReviewVerdict,
        )

        return ReviewVerdict(
            decision="reject",
            summary="Missing error handling in API layer",
            issues=(
                ReviewIssue(
                    severity="critical",
                    category="correctness",
                    file_path="src/api.py",
                    description="No error handling for network failures",
                    suggestion="Add try/except around HTTP calls",
                ),
                ReviewIssue(
                    severity="major",
                    category="quality",
                    file_path="tests/test_api.py",
                    description="Missing test for error path",
                    suggestion="Add test for network error case",
                ),
            ),
            confidence=0.85,
            cost_usd=0.10,
            duration_seconds=3.0,
        )

    @pytest.mark.parametrize(
        "expected_text",
        [
            "QR-99",
            "https://github.com/org/repo/pull/42",
            "Missing error handling",
            "src/api.py",
            "tests/test_api.py",
            "Add try/except",
            "TDD",
            "Russian",
        ],
        ids=[
            "issue_key",
            "pr_url",
            "summary",
            "file_path_1",
            "file_path_2",
            "suggestion",
            "tdd_instruction",
            "russian_note",
        ],
    )
    def test_contains_expected_text(
        self,
        expected_text: str,
    ) -> None:
        verdict = self._make_verdict()
        prompt = build_pre_merge_rejection_prompt(
            "QR-99",
            self._URL,
            verdict,
        )
        assert expected_text in prompt

    def test_contains_severity_labels(self) -> None:
        verdict = self._make_verdict()
        prompt = build_pre_merge_rejection_prompt(
            "QR-99",
            self._URL,
            verdict,
        )
        assert "critical" in prompt
        assert "major" in prompt

    def test_empty_issues(self) -> None:
        from orchestrator.pre_merge_reviewer import ReviewVerdict

        verdict = ReviewVerdict(
            decision="reject",
            summary="Generic issues",
            issues=(),
            confidence=0.5,
            cost_usd=0.05,
            duration_seconds=1.0,
        )
        prompt = build_pre_merge_rejection_prompt(
            "QR-99",
            self._URL,
            verdict,
        )
        assert "QR-99" in prompt
        assert "Generic issues" in prompt


class TestBuildFallbackContextPrompt:
    """Tests for build_fallback_context_prompt."""

    def test_contains_task_key_and_summary(self) -> None:
        issue = _make_issue(key="QR-50", summary="Fix auth bug")
        prompt = build_fallback_context_prompt(issue)
        assert "QR-50" in prompt
        assert "Fix auth bug" in prompt

    def test_contains_description(self) -> None:
        issue = _make_issue(description="OAuth2 flow is broken")
        prompt = build_fallback_context_prompt(issue)
        assert "OAuth2 flow is broken" in prompt

    def test_contains_comments(self) -> None:
        issue = _make_issue()
        comments = [
            {
                "createdBy": {"display": "Alice"},
                "text": "Try endpoint /v2",
                "createdAt": "2025-06-01",
            },
        ]
        prompt = build_fallback_context_prompt(issue, comments=comments)
        assert "Try endpoint /v2" in prompt
        assert "Alice" in prompt

    def test_no_comments_section_when_none(self) -> None:
        prompt = build_fallback_context_prompt(_make_issue(), comments=None)
        assert "Recent Comments" not in prompt

    def test_no_comments_section_when_empty(self) -> None:
        prompt = build_fallback_context_prompt(_make_issue(), comments=[])
        assert "Recent Comments" not in prompt

    def test_contains_message_history(self) -> None:
        issue = _make_issue()
        messages = ["[QR-10 -> QR-50] (request): What API endpoint?"]
        prompt = build_fallback_context_prompt(issue, message_history=messages)
        assert "What API endpoint?" in prompt

    def test_no_messages_section_when_none(self) -> None:
        prompt = build_fallback_context_prompt(_make_issue(), message_history=None)
        assert "Inter-Agent Message History" not in prompt

    def test_no_messages_section_when_empty(self) -> None:
        prompt = build_fallback_context_prompt(_make_issue(), message_history=[])
        assert "Inter-Agent Message History" not in prompt

    def test_truncates_long_comments(self) -> None:
        """Only last MAX_FALLBACK_COMMENTS comments are included."""
        issue = _make_issue()
        comments = [
            {
                "createdBy": {"display": f"User{i}"},
                "text": f"Comment {i}",
                "createdAt": "2025-06-01",
            }
            for i in range(20)
        ]
        prompt = build_fallback_context_prompt(issue, comments=comments)
        # Last 5 should be present (MAX_FALLBACK_COMMENTS = 5)
        assert "Comment 19" in prompt
        assert "Comment 15" in prompt
        # Comment 0-14 should not be included
        assert "Comment 0" not in prompt
        assert "Comment 14" not in prompt

    def test_fallback_session_header(self) -> None:
        prompt = build_fallback_context_prompt(_make_issue())
        assert "Fallback Session" in prompt
