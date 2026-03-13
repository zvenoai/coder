"""Prompt construction for supervisor chat sessions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from orchestrator.prompt_builder import build_system_prompt_append as build_supervisor_system_prompt

if TYPE_CHECKING:
    from orchestrator.heartbeat import AgentHealthReport, HeartbeatResult

__all__ = [
    "build_auto_merge_failed_prompt",
    "build_epic_completion_prompt",
    "build_epic_decompose_prompt",
    "build_epic_plan_prompt",
    "build_escalation_prompt",
    "build_heartbeat_prompt",
    "build_pre_merge_review_prompt",
    "build_preflight_skip_prompt",
    "build_supervisor_system_prompt",
    "build_task_deferred_prompt",
    "build_task_unblocked_prompt",
]


def build_epic_plan_prompt(
    epic_key: str,
    epic_summary: str,
    children: list[dict[str, str]],
) -> str:
    """Build a prompt asking the supervisor to plan epic child execution order.

    Args:
        epic_key: Epic issue key (e.g. "QR-50").
        epic_summary: Epic title/summary.
        children: List of dicts with "key", "summary", "status" for each child.
    """
    count = len(children)
    lines = [
        f'[System] Epic **{epic_key}** ("{epic_summary}") discovered with {count} children awaiting planning.',
        "",
    ]

    if children:
        lines.append("Children:")
        for child in children:
            lines.append(f"- **{child['key']}**: {child['summary']} (status: {child['status']})")
        lines.append("")

    lines.extend(
        [
            "Analyze the children and set the execution plan:",
            "1. Use `epic_get_children` to review full details",
            "2. Determine dependencies between children",
            "3. Use `epic_set_plan` to define the dependency graph and activate ready children",
            '   Example: `{"QR-52": ["QR-51"], "QR-51": []}`',
            "4. Or use `epic_activate_child` to activate children one by one",
        ]
    )

    return "\n".join(lines)


def build_preflight_skip_prompt(
    task_key: str,
    epic_key: str,
    reason: str,
    source: str,
) -> str:
    """Build a prompt notifying supervisor that an epic child was skipped.

    Args:
        task_key: The child task key that was skipped.
        epic_key: The parent epic key.
        reason: Why the task was skipped (e.g., "merged PR found").
        source: What triggered the skip (e.g., "preflight_checker").
    """
    return (
        f"[System] Epic child **{task_key}** (epic: **{epic_key}**) was **skipped** by {source}.\n\n"
        f"**Reason:** {reason}\n\n"
        f"## Action Required\n"
        f"This skip may be a false positive (e.g., a PR mentioning {task_key} in the body "
        f"but not actually implementing it). Please validate:\n\n"
        f"1. Use `stats_query_custom` to check if {task_key} has any actual task_runs "
        f"(SELECT * FROM task_runs WHERE task_key = '{task_key}')\n"
        f"2. Use `tracker_get_issue` to verify the current status of {task_key}\n"
        f"3. Use `tracker_get_comments` to check for completion evidence\n"
        f"4. If this is a **false positive** — use `epic_reset_child` to reset {task_key} "
        f"back to PENDING for re-dispatch\n"
        f"5. If the skip is **correct** — no action needed\n"
    )


def build_epic_completion_prompt(
    epic_key: str,
    children_summary: list[dict[str, str]],
) -> str:
    """Build a prompt asking supervisor to verify epic completion.

    Args:
        epic_key: The epic key that was completed.
        children_summary: List of dicts with "key", "status" for each child.
    """
    lines = [
        f"[System] Epic **{epic_key}** has been marked as **completed**.",
        "",
        "## Children Summary",
    ]

    for child in children_summary:
        lines.append(f"- **{child['key']}**: {child['status']}")

    lines.extend(
        [
            "",
            "## Verification Required",
            "Please verify that all children actually ran and completed properly:",
            "",
            "1. Use `stats_query_custom` to check task_runs for each child "
            "(SELECT task_key, success, pr_url FROM task_runs WHERE task_key IN (...))",
            "2. If any child was falsely completed without an agent run — use `epic_reset_child` "
            "to reset it and re-dispatch",
            "3. Write observations to memory for future reference",
        ]
    )

    return "\n".join(lines)


def _build_preflight_review_prompt(
    task_key: str,
    evidence: str,
) -> str:
    """Build a prompt for supervisor to review preflight evidence.

    Args:
        task_key: The task key requiring review.
        evidence: Collected evidence summary.
    """
    return (
        f"[System] Task **{task_key}** has prior implementation"
        f" evidence and needs your review before dispatch.\n\n"
        f"**Evidence:** {evidence}\n\n"
        f"## Investigation Steps\n"
        f"1. `tracker_get_issue` — check current Tracker status"
        f" and description\n"
        f"2. `github_search_prs` — check for open PRs with"
        f" review comments or CI failures\n"
        f"3. `stats_query_custom` — check task_runs history"
        f" (SELECT * FROM task_runs WHERE task_key ="
        f" '{task_key}')\n"
        f"4. `tracker_get_comments` — check for context\n\n"
        f"## Decision\n"
        f'- `resolve_preflight("{task_key}", "dispatch")`'
        f" — task needs work, send agent\n"
        f'- `resolve_preflight("{task_key}", "skip")`'
        f" — task is complete, skip it\n"
    )


def build_task_deferred_prompt(
    task_key: str,
    task_summary: str,
    blockers: list[str],
) -> str:
    """Build a prompt notifying supervisor that a task was deferred.

    Detects preflight review deferrals (blockers starting with
    "preflight_review:") and routes to the preflight review prompt.

    Args:
        task_key: The deferred task key.
        task_summary: The deferred task summary.
        blockers: List of blocker issue keys or preflight evidence.
    """
    # Detect preflight review deferrals
    if blockers and str(blockers[0]).startswith(
        "preflight_review:",
    ):
        evidence = str(blockers[0]).removeprefix(
            "preflight_review: ",
        )
        return _build_preflight_review_prompt(
            task_key,
            evidence,
        )

    blockers_str = ", ".join(f"**{b}**" for b in blockers)
    return (
        f'[System] Task **{task_key}** ("{task_summary}") was **deferred** — '
        f"blocked by unresolved dependencies: {blockers_str}.\n\n"
        f"The task will be automatically dispatched when all blockers are resolved.\n\n"
        f"## Available Actions\n"
        f"- `list_deferred_tasks` — see all deferred tasks\n"
        f"- `approve_task_dispatch` — force-dispatch {task_key} despite unresolved blockers\n"
        f"- `defer_task` — manually defer other tasks with semantic dependencies\n"
    )


def build_task_unblocked_prompt(
    task_key: str,
    task_summary: str,
    previous_blockers: list[str],
) -> str:
    """Build a prompt informing supervisor that a deferred task is now unblocked.

    Args:
        task_key: The unblocked task key.
        task_summary: The task summary.
        previous_blockers: List of blocker keys that are now resolved.
    """
    blockers_str = ", ".join(f"**{b}**" for b in previous_blockers)
    return (
        f'[System] Task **{task_key}** ("{task_summary}") is now **unblocked** — '
        f"previous blockers resolved: {blockers_str}.\n\n"
        f"The task will be dispatched on the next polling cycle."
    )


def build_heartbeat_prompt(
    result: HeartbeatResult,
    stuck: Sequence[AgentHealthReport],
    long_running: Sequence[AgentHealthReport],
    stale_reviews: Sequence[AgentHealthReport],
    is_full_report: bool,
) -> str:
    """Build a heartbeat prompt for the supervisor.

    Args:
        result: HeartbeatResult with aggregate metrics.
        stuck: Agents exceeding idle threshold (new alerts only).
        long_running: Agents exceeding elapsed threshold.
        stale_reviews: In-review agents with no review activity.
        is_full_report: Whether this is a periodic full summary.
    """
    total = result.total_agents
    healthy = result.healthy_agents

    has_problems = bool(stuck or long_running or stale_reviews)

    if not has_problems and is_full_report:
        return f"[Heartbeat] All clear: {total} agent(s) running, {healthy} healthy. No issues detected."

    lines = [
        f"[Heartbeat] {total} agent(s) running, {healthy} healthy.",
        "",
    ]

    if stuck:
        lines.append("## Stuck Agents (idle too long)")
        for r in stuck:
            cost_str = f"cost=${r.cost_usd:.2f}" if r.cost_usd is not None else "cost=N/A"
            tracker_part = f", tracker: {r.tracker_status}" if r.tracker_status else ""
            lines.append(
                f"- **{r.task_key}**: {r.issue_summary} (idle {int(r.idle_seconds)}s, {cost_str}{tracker_part})"
            )
            if r.last_output_snippet:
                lines.append(f"  Last output: `{r.last_output_snippet}`")
        lines.append("")

    if long_running:
        lines.append("## Long-Running Agents")
        for r in long_running:
            cost_str = f"cost=${r.cost_usd:.2f}" if r.cost_usd is not None else "cost=N/A"
            total_k = (r.input_tokens + r.output_tokens) // 1000
            lines.append(
                f"- **{r.task_key}**: "
                f"{r.issue_summary} "
                f"(elapsed {int(r.elapsed_seconds)}s, "
                f"compactions: {r.compaction_count}, "
                f"{cost_str}, tokens: {total_k}K)"
            )
        lines.append("")

    if stale_reviews:
        lines.append("## Stale Reviews (no review activity)")
        for r in stale_reviews:
            lines.append(f"- **{r.task_key}**: {r.pr_url} (idle {int(r.idle_seconds)}s)")
        lines.append("")

    lines.extend(
        [
            "## Available Actions",
            "- `orchestrator_diagnose_task` — deep diagnostics",
            "- `send_message_to_task` — nudge an agent",
            "- `abort_task` — kill a stuck agent",
            "- `get_agent_health` — refresh health data",
        ]
    )

    return "\n".join(lines)


def build_epic_decompose_prompt(
    epic_key: str,
    epic_summary: str,
    description: str,
) -> str:
    """Build a prompt asking supervisor to decompose an epic into subtasks.

    Args:
        epic_key: Epic issue key.
        epic_summary: Epic title/summary.
        description: Full epic description text.
    """
    desc_preview = description[:2000] if description else "(no description)"
    if len(description) > 2000:
        desc_preview += "\n... (truncated)"

    return (
        f"[System] Epic **{epic_key}** "
        f'("{epic_summary}") has **no children** '
        f"and needs decomposition.\n\n"
        f"## Epic Description\n{desc_preview}\n\n"
        f"## Instructions\n"
        f"1. Read the epic description and understand the scope\n"
        f"2. Explore the codebase to understand what needs to change "
        f"(use `Read`, `Grep`, `Glob`)\n"
        f"3. Create 2-6 subtasks via `epic_create_child` with:\n"
        f"   - Clear summary and detailed description\n"
        f"   - Appropriate component and assignee\n"
        f"4. Set the dependency graph via `epic_set_plan`\n"
    )


def build_auto_merge_failed_prompt(
    task_key: str,
    pr_url: str,
    reason: str,
) -> str:
    """Build a prompt notifying supervisor that auto-merge failed.

    Args:
        task_key: The task key associated with the PR.
        pr_url: The GitHub PR URL.
        reason: Why the merge failed.
    """
    return (
        f"[System] Auto-merge **failed** for "
        f"**{task_key}** ({pr_url}).\n\n"
        f"**Reason:** {reason}\n\n"
        f"## Available Actions\n"
        f"- `github_merge_pr` — manual merge\n"
        f"- `github_get_pr_checks` — check CI status\n"
        f"- `github_get_pr_reviews` — check review status\n"
    )


def build_pre_merge_review_prompt(
    task_key: str,
    pr_url: str,
    summary: str,
    issues: Sequence[dict[str, Any] | Any],
) -> str:
    """Build a prompt notifying supervisor that pre-merge review rejected a PR.

    Args:
        task_key: The task key associated with the PR.
        pr_url: The GitHub PR URL.
        summary: Review summary from the sub-agent.
        issues: Sequence of ReviewIssue dataclasses or serialized dicts.
    """
    lines = [
        f"[System] Pre-merge review **rejected** PR for **{task_key}** ({pr_url}).",
        "",
        f"**Summary:** {summary}",
        "",
    ]

    if issues:
        lines.append("## Issues Found")
        for issue in issues:
            # Support both dataclass objects and plain dicts
            # (event data contains serialized dicts)
            if isinstance(issue, dict):
                sev = issue.get("severity", "?")
                cat = issue.get("category", "?")
                fpath = issue.get("file_path", "")
                desc = issue.get("description", "")
            else:
                sev = getattr(issue, "severity", "?")
                cat = getattr(issue, "category", "?")
                fpath = getattr(issue, "file_path", "")
                desc = getattr(issue, "description", "")
            loc = f" in `{fpath}`" if fpath else ""
            lines.append(f"- [{sev}/{cat}]{loc}: {desc}")
        lines.append("")

    lines.extend(
        [
            "The worker agent has been sent the review feedback and will attempt to fix the issues.",
            "",
            "## Available Actions",
            "- `github_get_pr_diff` — review the full diff",
            "- `github_merge_pr` — manual merge if review is a false positive",
        ]
    )

    return "\n".join(lines)


def build_escalation_prompt(
    issue_key: str,
    reason: str,
) -> str:
    """Build a prompt notifying supervisor about an escalation.

    Args:
        issue_key: The escalated issue key.
        reason: Why the issue was escalated.
    """
    return (
        f"[System] Issue **{issue_key}** has been "
        f"**escalated** for human review.\n\n"
        f"**Reason:** {reason}\n\n"
        f"The issue has been tagged for human attention "
        f"and the `ai-task` tag removed to prevent "
        f"auto-dispatch."
    )
