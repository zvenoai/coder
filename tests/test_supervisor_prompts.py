"""Tests for supervisor prompt builders."""

from orchestrator.supervisor_prompt_builder import (
    build_epic_completion_prompt,
    build_epic_plan_prompt,
    build_preflight_skip_prompt,
    build_task_deferred_prompt,
    build_task_unblocked_prompt,
)


class TestBuildPreflightSkipPrompt:
    def test_contains_task_key(self) -> None:
        prompt = build_preflight_skip_prompt("QR-205", "QR-200", "merged PR found", "preflight_checker")
        assert "QR-205" in prompt

    def test_contains_epic_key(self) -> None:
        prompt = build_preflight_skip_prompt("QR-205", "QR-200", "merged PR found", "preflight_checker")
        assert "QR-200" in prompt

    def test_contains_reason(self) -> None:
        prompt = build_preflight_skip_prompt("QR-205", "QR-200", "merged PR found", "preflight_checker")
        assert "merged PR found" in prompt

    def test_contains_source(self) -> None:
        prompt = build_preflight_skip_prompt("QR-205", "QR-200", "merged PR found", "preflight_checker")
        assert "preflight_checker" in prompt

    def test_mentions_epic_reset_child(self) -> None:
        prompt = build_preflight_skip_prompt("QR-205", "QR-200", "merged PR found", "preflight_checker")
        assert "epic_reset_child" in prompt

    def test_mentions_false_positive(self) -> None:
        prompt = build_preflight_skip_prompt("QR-205", "QR-200", "merged PR found", "preflight_checker")
        assert "false positive" in prompt.lower()

    def test_mentions_stats_query(self) -> None:
        prompt = build_preflight_skip_prompt("QR-205", "QR-200", "merged PR found", "preflight_checker")
        assert "stats_query_custom" in prompt


class TestBuildEpicCompletionPrompt:
    def test_contains_epic_key(self) -> None:
        prompt = build_epic_completion_prompt("QR-200", [{"key": "QR-201", "status": "completed"}])
        assert "QR-200" in prompt

    def test_contains_children_summary(self) -> None:
        children = [
            {"key": "QR-201", "status": "completed"},
            {"key": "QR-202", "status": "cancelled"},
        ]
        prompt = build_epic_completion_prompt("QR-200", children)
        assert "QR-201" in prompt
        assert "QR-202" in prompt
        assert "completed" in prompt
        assert "cancelled" in prompt

    def test_mentions_verification(self) -> None:
        prompt = build_epic_completion_prompt("QR-200", [{"key": "QR-201", "status": "completed"}])
        assert "Verification" in prompt

    def test_mentions_epic_reset_child(self) -> None:
        prompt = build_epic_completion_prompt("QR-200", [{"key": "QR-201", "status": "completed"}])
        assert "epic_reset_child" in prompt

    def test_mentions_stats_query(self) -> None:
        prompt = build_epic_completion_prompt("QR-200", [{"key": "QR-201", "status": "completed"}])
        assert "stats_query_custom" in prompt


class TestBuildEpicPlanPrompt:
    def test_contains_epic_key(self) -> None:
        prompt = build_epic_plan_prompt("QR-50", "Epic task", [])
        assert "QR-50" in prompt

    def test_contains_children(self) -> None:
        children = [{"key": "QR-51", "summary": "Task A", "status": "pending"}]
        prompt = build_epic_plan_prompt("QR-50", "Epic task", children)
        assert "QR-51" in prompt
        assert "Task A" in prompt


class TestBuildTaskDeferredPrompt:
    def test_contains_task_key(self) -> None:
        prompt = build_task_deferred_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "QR-204" in prompt

    def test_contains_summary(self) -> None:
        prompt = build_task_deferred_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "Implement feature" in prompt

    def test_contains_blockers(self) -> None:
        prompt = build_task_deferred_prompt("QR-204", "Implement feature", ["QR-203", "QR-202"])
        assert "QR-203" in prompt
        assert "QR-202" in prompt

    def test_mentions_deferred(self) -> None:
        prompt = build_task_deferred_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "deferred" in prompt.lower()

    def test_mentions_available_tools(self) -> None:
        prompt = build_task_deferred_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "list_deferred_tasks" in prompt
        assert "approve_task_dispatch" in prompt
        assert "defer_task" in prompt


class TestBuildTaskUnblockedPrompt:
    def test_contains_task_key(self) -> None:
        prompt = build_task_unblocked_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "QR-204" in prompt

    def test_contains_summary(self) -> None:
        prompt = build_task_unblocked_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "Implement feature" in prompt

    def test_contains_previous_blockers(self) -> None:
        prompt = build_task_unblocked_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "QR-203" in prompt

    def test_mentions_unblocked(self) -> None:
        prompt = build_task_unblocked_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "unblocked" in prompt.lower()

    def test_mentions_dispatch(self) -> None:
        prompt = build_task_unblocked_prompt("QR-204", "Implement feature", ["QR-203"])
        assert "dispatch" in prompt.lower()
