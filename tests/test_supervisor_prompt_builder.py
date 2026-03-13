"""Tests for supervisor prompt builder."""

from orchestrator.heartbeat import AgentHealthReport, HeartbeatResult
from orchestrator.supervisor_prompt_builder import (
    build_epic_plan_prompt,
    build_heartbeat_prompt,
    build_supervisor_system_prompt,
)


class TestBuildEpicPlanPrompt:
    def test_contains_epic_key_and_summary(self) -> None:
        result = build_epic_plan_prompt(
            "QR-50", "Build auth", [{"key": "QR-51", "summary": "Login", "status": "pending"}]
        )
        assert "QR-50" in result
        assert "Build auth" in result

    def test_lists_all_children(self) -> None:
        children = [
            {"key": "QR-51", "summary": "Login", "status": "pending"},
            {"key": "QR-52", "summary": "JWT", "status": "pending"},
            {"key": "QR-53", "summary": "Tests", "status": "completed"},
        ]
        result = build_epic_plan_prompt("QR-50", "Auth", children)
        assert "QR-51" in result
        assert "QR-52" in result
        assert "QR-53" in result
        assert "Login" in result
        assert "JWT" in result

    def test_mentions_tool_names(self) -> None:
        result = build_epic_plan_prompt("QR-50", "Auth", [{"key": "QR-51", "summary": "X", "status": "pending"}])
        assert "epic_set_plan" in result
        assert "epic_activate_child" in result

    def test_empty_children(self) -> None:
        result = build_epic_plan_prompt("QR-50", "Empty", [])
        assert "QR-50" in result
        assert "0 children" in result

    def test_exported_in_all(self) -> None:
        from orchestrator import supervisor_prompt_builder

        assert "build_epic_plan_prompt" in supervisor_prompt_builder.__all__


def _make_report(
    task_key: str = "QR-1",
    issue_summary: str = "Test task",
    **kwargs,
) -> AgentHealthReport:
    """Create an AgentHealthReport with defaults."""
    defaults = dict(
        status="running",
        elapsed_seconds=600.0,
        idle_seconds=100.0,
        compaction_count=0,
        pr_url="",
        is_stuck=False,
        is_long_running=False,
        is_review_stale=False,
        cost_usd=None,
        last_output_snippet="",
        tracker_status="",
        input_tokens=0,
        output_tokens=0,
    )
    defaults.update(kwargs)
    return AgentHealthReport(
        task_key=task_key,
        issue_summary=issue_summary,
        **defaults,
    )


class TestBuildHeartbeatPromptEnriched:
    """Heartbeat prompt includes cost, tokens, snippet, tracker."""

    def test_stuck_includes_cost_and_tracker(self) -> None:
        report = _make_report(
            task_key="QR-123",
            issue_summary="Fix auth",
            is_stuck=True,
            idle_seconds=1200.0,
            cost_usd=0.45,
            tracker_status="inProgress",
            last_output_snippet="running tests...",
        )
        hb = HeartbeatResult(
            total_agents=1,
            healthy_agents=0,
            stuck=[report],
            all_agents=[report],
        )
        prompt = build_heartbeat_prompt(
            result=hb,
            stuck=[report],
            long_running=[],
            stale_reviews=[],
            is_full_report=False,
        )
        assert "cost=$0.45" in prompt
        assert "inProgress" in prompt
        assert "running tests..." in prompt

    def test_stuck_cost_na_when_none(self) -> None:
        report = _make_report(
            task_key="QR-124",
            is_stuck=True,
            idle_seconds=1200.0,
            cost_usd=None,
        )
        hb = HeartbeatResult(
            total_agents=1,
            healthy_agents=0,
            stuck=[report],
            all_agents=[report],
        )
        prompt = build_heartbeat_prompt(
            result=hb,
            stuck=[report],
            long_running=[],
            stale_reviews=[],
            is_full_report=False,
        )
        assert "cost=N/A" in prompt

    def test_long_running_includes_tokens(self) -> None:
        report = _make_report(
            task_key="QR-125",
            issue_summary="Deploy feature",
            is_long_running=True,
            elapsed_seconds=3600.0,
            compaction_count=1,
            cost_usd=1.20,
            input_tokens=45000,
            output_tokens=12000,
        )
        hb = HeartbeatResult(
            total_agents=1,
            healthy_agents=0,
            long_running=[report],
            all_agents=[report],
        )
        prompt = build_heartbeat_prompt(
            result=hb,
            stuck=[],
            long_running=[report],
            stale_reviews=[],
            is_full_report=False,
        )
        assert "cost=$1.20" in prompt
        assert "tokens: 45K" in prompt or "tokens: 57K" in prompt


class TestBuildSupervisorSystemPrompt:
    def test_is_same_as_build_system_prompt_append(self) -> None:
        """build_supervisor_system_prompt should reuse build_system_prompt_append, not duplicate."""
        from orchestrator.prompt_builder import build_system_prompt_append

        assert build_supervisor_system_prompt is build_system_prompt_append

    def test_returns_empty_for_missing_file(self) -> None:
        result = build_supervisor_system_prompt("/nonexistent/path.md")
        assert result == ""

    def test_reads_existing_file(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        md_file = tmp_path / "workflow.md"
        md_file.write_text("# Supervisor\nDo stuff.", encoding="utf-8")
        result = build_supervisor_system_prompt(str(md_file))
        assert "# Supervisor" in result
        assert "Do stuff." in result

    def test_exported_in_all(self) -> None:
        """Verify build_supervisor_system_prompt is listed in __all__."""
        from orchestrator import supervisor_prompt_builder

        assert "build_supervisor_system_prompt" in supervisor_prompt_builder.__all__
