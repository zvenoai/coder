"""Prompt construction for agent tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.config import RepoInfo
from orchestrator.github_client import FailedCheck, ReviewThread
from orchestrator.tracker_client import TrackerIssue
from orchestrator.tracker_types import TrackerCommentDict

if TYPE_CHECKING:
    from orchestrator.pre_merge_reviewer import ReviewVerdict

# Maximum number of recent comments to include in fallback context
MAX_FALLBACK_COMMENTS = 5


@dataclass
class PeerInfo:
    """Information about a peer agent for prompt construction."""

    task_key: str
    summary: str
    status: str


def build_task_prompt(
    issue: TrackerIssue,
    all_repos: list[RepoInfo] | None = None,
    peers: list[PeerInfo] | None = None,
) -> str:
    """Build the main task prompt sent to the agent."""
    # Show repo catalog so the agent knows what's available
    repo_hint = ""
    if all_repos:
        catalog = "\n".join(f"- **{r.path.split('/')[-1]}**: {r.description}" for r in all_repos if r.description)
        repo_hint = (
            f"\n## Available repositories\n"
            f"Use `list_available_repos` to see all repositories, then call `request_worktree` "
            f"for each repository you need.\n\n"
            f"{catalog}\n\n"
        )

    peer_hint = ""
    if peers:
        peer_lines = "\n".join(f"- **{p.task_key}**: {p.summary} (status: {p.status})" for p in peers)
        peer_hint = (
            f"\n## Running Peer Agents\n"
            f"These agents are working concurrently. Coordinate with "
            f"them using `list_running_agents` and "
            f"`send_message_to_agent` BEFORE starting implementation "
            f"if your tasks share an API boundary, data model, or "
            f"common dependency.\n\n"
            f"{peer_lines}\n\n"
        )

    return (
        f"Execute Yandex Tracker task {issue.key}: {issue.summary}\n\n"
        f"## Description\n{issue.description}\n\n"
        f"{repo_hint}"
        f"{peer_hint}"
        f"## Instructions\n"
        f"1. Read the full task details from Tracker using the tracker_get_issue tool\n"
        f"2. Use `list_available_repos` to see which repositories are available\n"
        f"3. Call `request_worktree` for each repository you need to work in\n"
        f"4. Follow the workflow instructions from your system prompt\n"
        f"5. Implement using TDD: write tests first, then code, then refactor\n"
        f"6. Commit, push the branch, and create a PR with gh\n"
        f"7. Comment on the task with results using tracker_add_comment\n"
        f"\n"
        f"**IMPORTANT**: All public-facing text (PR title/body, Tracker comments, commit message descriptions) "
        f"MUST be written in Russian.\n"
    )


def build_review_prompt(
    issue_key: str,
    pr_url: str,
    threads: list[ReviewThread],
) -> str:
    """Build prompt for addressing unresolved PR review conversations."""
    reviews_text: list[str] = []
    for i, thread in enumerate(threads, 1):
        location = f" in `{thread.path}:{thread.line}`" if thread.path else ""
        reviews_text.append(f"### Conversation {i}{location}\n")
        for comment in thread.comments:
            reviews_text.append(f"**{comment.author}**:\n{comment.body}\n")

    return (
        f"There are {len(threads)} unresolved review conversation(s) on PR {pr_url} "
        f"for task {issue_key}.\n\n"
        f"## Unresolved Conversations\n\n" + "\n---\n".join(reviews_text) + f"\n\n## Instructions\n"
        f"For each unresolved conversation:\n"
        f"1. **Verify via test (TDD)**: Write a test that reproduces the issue described in the review comment. "
        f"Run it and confirm it fails, demonstrating the bug exists.\n"
        f"2. **Fix the code**: Make the minimal change to address the review feedback.\n"
        f"3. **Run tests**: Ensure the new test passes and no existing tests break.\n"
        f"4. **Commit**: `git commit` with message `fix({issue_key}): <what was fixed>`\n"
        f"5. **Push**: `git push` to the existing branch.\n"
        f"6. **Reply on PR**: For each conversation, reply using:\n"
        f"   ```\n"
        f"   gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments -f body='<your reply explaining what was fixed>'\n"
        f"   ```\n"
        f"   Extract OWNER, REPO, PR_NUMBER from the PR URL: {pr_url}\n"
        f"7. **Report**: Comment on the Tracker task with a summary using tracker_add_comment.\n"
        f"\n"
        f"If the review comment is not actionable or is a false positive, reply on the PR explaining why "
        f"and skip the test/fix steps for that conversation.\n"
        f"\n"
        f"**IMPORTANT**: All public-facing text (PR replies, Tracker comments, commit message descriptions) "
        f"MUST be written in Russian.\n"
    )


def build_pipeline_failure_prompt(
    issue_key: str,
    pr_url: str,
    failed_checks: list[FailedCheck],
) -> str:
    """Build prompt for addressing failed CI pipeline checks."""
    checks_text: list[str] = []
    for i, check in enumerate(failed_checks, 1):
        parts = [f"### {i}. {check.name}"]
        parts.append(f"- **Conclusion**: {check.conclusion}")
        if check.summary:
            # Truncate long summaries
            summary = check.summary[:1000]
            parts.append(f"- **Summary**:\n```\n{summary}\n```")
        if check.details_url:
            parts.append(f"- **Details**: {check.details_url}")
        checks_text.append("\n".join(parts))

    return (
        f"CI pipeline checks have **failed** on PR {pr_url} for task {issue_key}.\n\n"
        f"## Failed Checks ({len(failed_checks)})\n\n" + "\n\n---\n\n".join(checks_text) + f"\n\n## Instructions\n"
        f"1. **Investigate** each failed check — read the summary and if needed fetch the details URL "
        f"to understand the failure\n"
        f"2. **Fix the code** to address the pipeline failures (test failures, lint errors, build errors, etc.)\n"
        f"3. **Run tests locally** to verify your fixes work\n"
        f"4. **Commit**: `git commit` with message `fix({issue_key}): fix CI pipeline failures`\n"
        f"5. **Push**: `git push` to the existing branch — this will trigger a new pipeline run\n"
        f"6. **Report**: Comment on the Tracker task with a summary using tracker_add_comment\n"
        f"\n"
        f"**IMPORTANT**: All public-facing text (Tracker comments, commit message descriptions) "
        f"MUST be written in Russian.\n"
    )


def build_needs_info_response_prompt(
    issue_key: str,
    new_comments: list[TrackerCommentDict],
) -> str:
    """Build prompt for resuming work after human responds to a needs-info request."""
    comments_text: list[str] = []
    for c in new_comments:
        author = c.get("createdBy", {}).get("display", "unknown")
        text = c.get("text", "")
        created = c.get("createdAt", "")
        comments_text.append(f"**{author}** ({created}):\n{text}")

    formatted = "\n\n---\n\n".join(comments_text)

    return (
        f"A human has responded to your information request on task {issue_key}.\n\n"
        f"## New Comments\n\n"
        f"{formatted}\n\n"
        f"## Instructions\n"
        f"1. Read the new comments carefully and use the provided information\n"
        f"2. Continue working on the task from where you left off\n"
        f"3. If you still need more information, use `tracker_request_info` again\n"
        f"4. Otherwise, complete the task following the standard workflow\n"
        f"\n"
        f"**IMPORTANT**: All public-facing text (PR title/body, Tracker comments, commit message descriptions) "
        f"MUST be written in Russian.\n"
    )


def build_merge_conflict_prompt(issue_key: str, pr_url: str) -> str:
    """Build prompt for notifying agent about merge conflicts on their PR."""
    return (
        f"Your PR {pr_url} for task {issue_key} has **merge conflicts** with the base branch.\n\n"
        f"## Instructions\n"
        f"1. **Fetch and rebase**: `git fetch origin && git rebase origin/main`\n"
        f"2. **Resolve conflicts**: Fix all merge conflicts in the affected files\n"
        f"3. **Run tests**: Ensure all tests still pass after resolving conflicts\n"
        f"4. **Force push**: `git push --force-with-lease` to update the PR\n"
        f"5. **Report**: Comment on the Tracker task with a summary using tracker_add_comment\n"
        f"\n"
        f"**IMPORTANT**: All public-facing text (Tracker comments, commit message descriptions) "
        f"MUST be written in Russian.\n"
    )


def _bundle_sub_agent_prompt(
    parent_dir: Path,
    filename: str,
    heading: str,
) -> str:
    """Read a sub-agent prompt file and wrap it with a heading.

    Returns empty string if the file does not exist.
    """
    path = parent_dir / filename
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    return f"\n\n---\n\n# {heading}\n\n{text}"


# Sub-agent prompts bundled into the worker system prompt.
# Each entry: (filename, heading shown to the worker agent).
_SUB_AGENT_PROMPTS: list[tuple[str, str]] = [
    (
        "plan_agent.md",
        "Planning Agent Prompt (use as-is when spawning the planning sub-agent)",
    ),
    (
        "critic_agent.md",
        "Critic Agent Prompt (use as-is when spawning the critic sub-agent)",
    ),
    (
        "simplify_agent.md",
        "Simplify Agent Prompt (use as-is when spawning the simplify sub-agents)",
    ),
]


def build_system_prompt_append(workflow_path: str | Path) -> str:
    """Load workflow and sub-agent prompts to append to the system prompt.

    Bundles plan_agent.md, critic_agent.md, and simplify_agent.md
    directly into the system prompt so the worker agent can reference
    them without reading from disk (it works in a different worktree
    where these files don't exist).
    """
    workflow = Path(workflow_path)
    if not workflow.exists():
        return ""

    content = workflow.read_text(encoding="utf-8")

    for filename, heading in _SUB_AGENT_PROMPTS:
        content += _bundle_sub_agent_prompt(
            workflow.parent,
            filename,
            heading,
        )

    return content


def build_pre_merge_rejection_prompt(
    issue_key: str,
    pr_url: str,
    verdict: ReviewVerdict,
) -> str:
    """Build prompt for addressing pre-merge review rejection.

    Sent to the worker agent so it can fix the issues found
    by the pre-merge review sub-agent.
    """
    issues_text: list[str] = []
    for i, issue in enumerate(verdict.issues, 1):
        parts = [f"### {i}. [{issue.severity}] {issue.description}"]
        if issue.file_path:
            parts.append(f"- **File**: `{issue.file_path}`")
        parts.append(f"- **Category**: {issue.category}")
        if issue.suggestion:
            parts.append(f"- **Suggestion**: {issue.suggestion}")
        issues_text.append("\n".join(parts))

    issues_section = ""
    if issues_text:
        issues_section = f"## Issues ({len(verdict.issues)})\n\n" + "\n\n---\n\n".join(issues_text) + "\n\n"

    return (
        f"Pre-merge code review has **rejected** PR {pr_url} "
        f"for task {issue_key}.\n\n"
        f"**Summary:** {verdict.summary}\n\n"
        f"{issues_section}"
        f"## Instructions\n"
        f"1. **Read** each issue carefully — understand "
        f"what the reviewer found\n"
        f"2. **Write a failing test (TDD)**: For each "
        f"issue, write a test that reproduces the problem\n"
        f"3. **Fix the code**: Make the minimal change to "
        f"address the review feedback\n"
        f"4. **Run tests**: Ensure new tests pass and no "
        f"existing tests break\n"
        f"5. **Commit**: `git commit` with message "
        f"`fix({issue_key}): address pre-merge review`\n"
        f"6. **Push**: `git push` to the existing branch\n"
        f"7. **Report**: Comment on the Tracker task with "
        f"a summary using tracker_add_comment\n"
        f"\n"
        f"**IMPORTANT**: All public-facing text (Tracker "
        f"comments, commit message descriptions) "
        f"MUST be written in Russian.\n"
    )


def build_task_continuation_prompt(
    issue_key: str,
    issue_summary: str,
    turn_number: int,
    max_turns: int,
) -> str:
    """Build prompt for multi-turn continuation.

    Sent when the agent completed without a PR and the
    task is still In Progress.
    """
    return (
        f"Your previous turn completed but task "
        f"{issue_key} ({issue_summary}) is still "
        f"In Progress without a PR.\n\n"
        f"**Continuation turn {turn_number}/{max_turns}.**"
        f"\n\n"
        f"Check your Workpad for progress notes, then "
        f"continue from the current state.\n\n"
        f"If this task genuinely does not need a PR "
        f"(research, docs, config, investigation), "
        f"call `tracker_mark_complete` to signal "
        f"legitimate completion."
    )


def build_fallback_context_prompt(
    issue: TrackerIssue,
    comments: list[TrackerCommentDict] | None = None,
    message_history: list[str] | None = None,
) -> str:
    """Build context prompt for fallback on-demand sessions.

    Provides the agent with task details, recent comments, and
    inter-agent message history so it can continue work effectively
    even without prior conversation history.

    This prompt is sent to fresh on-demand sessions when session
    resume fails, giving the agent the context it needs to continue.

    Args:
        issue: Tracker issue data (key, summary, description).
        comments: Recent task comments from Tracker. None if fetch
            timed out or failed. Only the last MAX_FALLBACK_COMMENTS
            will be included.
        message_history: Formatted inter-agent messages involving
            this task. None or empty if no messages exist.

    Returns:
        Context prompt string to prepend to the incoming message.
    """
    parts: list[str] = [
        f"## Task Context (Fallback Session)\n"
        f"You are resuming work on task {issue.key}: "
        f"{issue.summary}\n\n"
        f"Your previous session is no longer available. "
        f"Here is the context to help you continue.\n",
    ]

    parts.append(f"\n### Task Description\n{issue.description}\n")

    if comments:
        recent = comments[-MAX_FALLBACK_COMMENTS:]
        comment_lines: list[str] = []
        for c in recent:
            author = c.get("createdBy", {}).get("display", "unknown")
            text = c.get("text", "")
            created = c.get("createdAt", "")
            comment_lines.append(f"**{author}** ({created}):\n{text}")
        parts.append("\n### Recent Comments\n" + "\n\n---\n\n".join(comment_lines) + "\n")

    if message_history:
        parts.append("\n### Inter-Agent Message History\n" + "\n".join(message_history) + "\n")

    return "\n".join(parts)
