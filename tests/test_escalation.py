"""Tests for supervisor escalation mechanism."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.escalation import (
    build_escalation_comment,
    escalate_to_human,
)
from orchestrator.supervisor_prompt_builder import (
    build_escalation_prompt,
)


class TestBuildEscalationComment:
    def test_format_contains_reason(self) -> None:
        result = build_escalation_comment(
            reason="Unclear requirements for auth module",
            options=["Option A", "Option B"],
        )
        assert "Unclear requirements for auth module" in result

    def test_lists_options(self) -> None:
        options = [
            "Implement OAuth2 flow",
            "Use simple JWT tokens",
            "Defer to human architect",
        ]
        result = build_escalation_comment(
            reason="Architecture decision needed",
            options=options,
        )
        for opt in options:
            assert opt in result

    def test_tagged_as_machine_generated(self) -> None:
        result = build_escalation_comment(
            reason="Some reason",
            options=["A"],
        )
        assert "machine-generated" in result.lower() or ("auto" in result.lower() and "generated" in result.lower())

    def test_empty_options(self) -> None:
        result = build_escalation_comment(
            reason="Need guidance",
            options=[],
        )
        assert "Need guidance" in result


class TestEscalateToHuman:
    @pytest.mark.asyncio
    async def test_adds_tag(self) -> None:
        client = MagicMock()
        issue = MagicMock()
        issue.tags = ["ai-task", "backend"]
        client.get_issue.return_value = issue
        client.update_issue_tags.return_value = {}
        client.add_comment.return_value = {}

        await escalate_to_human(
            tracker_client=client,
            issue_key="QR-100",
            reason="Unclear scope",
            options=["A", "B"],
            tag="needs-human-review",
        )

        client.update_issue_tags.assert_called_once()
        new_tags = client.update_issue_tags.call_args[0][1]
        assert "needs-human-review" in new_tags

    @pytest.mark.asyncio
    async def test_posts_comment(self) -> None:
        client = MagicMock()
        issue = MagicMock()
        issue.tags = ["ai-task"]
        client.get_issue.return_value = issue
        client.update_issue_tags.return_value = {}
        client.add_comment.return_value = {}

        await escalate_to_human(
            tracker_client=client,
            issue_key="QR-100",
            reason="Need human decision",
            options=["Option 1", "Option 2"],
            tag="needs-human-review",
        )

        client.add_comment.assert_called_once()
        comment_text = client.add_comment.call_args[0][1]
        assert "Need human decision" in comment_text
        assert "Option 1" in comment_text

    @pytest.mark.asyncio
    async def test_removes_ai_task_tag(self) -> None:
        client = MagicMock()
        issue = MagicMock()
        issue.tags = ["ai-task", "backend"]
        client.get_issue.return_value = issue
        client.update_issue_tags.return_value = {}
        client.add_comment.return_value = {}

        await escalate_to_human(
            tracker_client=client,
            issue_key="QR-100",
            reason="Reason",
            options=["A"],
            tag="needs-human-review",
        )

        new_tags = client.update_issue_tags.call_args[0][1]
        assert "ai-task" not in new_tags
        assert "needs-human-review" in new_tags
        assert "backend" in new_tags

    @pytest.mark.asyncio
    async def test_returns_confirmation(self) -> None:
        client = MagicMock()
        issue = MagicMock()
        issue.tags = []
        client.get_issue.return_value = issue
        client.update_issue_tags.return_value = {}
        client.add_comment.return_value = {}

        result = await escalate_to_human(
            tracker_client=client,
            issue_key="QR-100",
            reason="Reason",
            options=["A"],
            tag="needs-human-review",
        )

        assert "QR-100" in result
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_no_ai_task_tag_present(self) -> None:
        """When ai-task tag is not present, should not fail."""
        client = MagicMock()
        issue = MagicMock()
        issue.tags = ["some-other-tag"]
        client.get_issue.return_value = issue
        client.update_issue_tags.return_value = {}
        client.add_comment.return_value = {}

        await escalate_to_human(
            tracker_client=client,
            issue_key="QR-100",
            reason="Reason",
            options=["A"],
            tag="needs-human-review",
        )

        new_tags = client.update_issue_tags.call_args[0][1]
        assert "needs-human-review" in new_tags
        assert "some-other-tag" in new_tags


class TestBuildEscalationPrompt:
    def test_format_contains_issue_key(self) -> None:
        result = build_escalation_prompt(
            issue_key="QR-200",
            reason="Cross-service change needed",
        )
        assert "QR-200" in result

    def test_format_contains_reason(self) -> None:
        result = build_escalation_prompt(
            issue_key="QR-200",
            reason="Epic has 8 children, unclear requirements",
        )
        assert "Epic has 8 children" in result

    def test_exported_in_all(self) -> None:
        from orchestrator import supervisor_prompt_builder

        assert "build_escalation_prompt" in (supervisor_prompt_builder.__all__)
