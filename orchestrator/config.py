"""Configuration loaded from environment variables."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepoInfo:
    url: str
    path: str
    description: str = ""


@dataclass(frozen=True)
class ReposConfig:
    all_repos: list[RepoInfo] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    tracker_token: str
    tracker_org_id: str
    tracker_queue: str = "QR"
    tracker_tag: str = "ai-task"
    poll_interval_seconds: int = 60
    agent_max_budget_usd: float | None = None
    workspace_dir: str = "/workspace"
    repos_config: ReposConfig = field(default_factory=ReposConfig)
    workflow_prompt_path: str = "prompts/workflow.md"
    # SDK authentication — OAuth token uses Claude Code subscription quota,
    # API key bills to API credits. Prefer OAuth.
    claude_oauth_token: str = ""
    anthropic_api_key: str = ""
    github_token: str = ""
    agent_model: str = "claude-opus-4-6"
    max_concurrent_agents: int = 2
    worktree_base_dir: str = "/workspace/worktrees"
    agent_permission_mode: str = "acceptEdits"
    review_check_delay_seconds: int = 120
    needs_info_check_delay_seconds: int = 120
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    tracker_project_id: int = 0
    tracker_boards: list[int] = field(default_factory=list)
    # Component → (Tracker component name, assignee login) mapping
    component_assignee_map: dict[str, tuple[str, str]] = field(default_factory=dict)
    supervisor_enabled: bool = True
    supervisor_model: str = "claude-opus-4-6"
    supervisor_max_budget_usd: float | None = None
    db_path: str = "data/coder.db"
    # Embeddings API (any OpenAI-compatible endpoint)
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.zveno.ai/v1"
    supervisor_memory_dir: str = "data/memory"
    supervisor_memory_index_path: str = "data/memory/.index.sqlite"
    supervisor_memory_auto_recall: bool = True
    # Compaction settings
    compaction_enabled: bool = True
    compaction_buffer_tokens: int = 20000
    compaction_model: str = "claude-haiku-4-5-20251001"
    # Kubernetes access for supervisor
    k8s_logs_enabled: bool = True
    k8s_namespace: str = "dev"
    # Heartbeat monitoring
    heartbeat_interval_seconds: int = 300
    heartbeat_stuck_threshold_seconds: int = 600
    heartbeat_long_running_threshold_seconds: int = 3600
    heartbeat_review_stale_threshold_seconds: int = 1800
    heartbeat_full_report_every_n: int = 3
    heartbeat_cooldown_seconds: int = 900
    # PR auto-merge
    auto_merge_enabled: bool = False
    auto_merge_method: str = "squash"
    auto_merge_require_approval: bool = True
    # Pre-merge code review (requires auto_merge_enabled=True to take effect)
    pre_merge_review_enabled: bool = False
    pre_merge_review_model: str = "claude-sonnet-4-20250514"
    pre_merge_review_max_budget_usd: float = 0.50
    pre_merge_review_timeout_seconds: int = 300
    pre_merge_review_fail_open: bool = False
    max_concurrent_reviews: int = 5
    # Alertmanager webhook
    alertmanager_webhook_enabled: bool = False
    alertmanager_auto_create_task: bool = False
    alertmanager_auto_task_queue: str = "QR"
    alertmanager_auto_task_tag: str = "ai-task"
    # Supervisor escalation
    supervisor_escalation_tag: str = "needs-human-review"
    # Post-merge verification
    post_merge_verification_enabled: bool = False
    post_merge_wait_for_ci: bool = True
    post_merge_wait_for_k8s: bool = True
    post_merge_ci_timeout_seconds: int = 600
    post_merge_k8s_timeout_seconds: int = 300
    post_merge_k8s_deployment: str = ""
    post_merge_verification_model: str = "claude-sonnet-4-20250514"
    post_merge_verification_max_budget_usd: float = 1.0
    post_merge_verification_timeout_seconds: int = 300
    post_merge_verification_environment: str = "dev"
    # Total task cost cap (initial send + all continuation
    # turns). None = unlimited.
    max_continuation_cost: float | None = None
    # Merge conflict retry
    merge_conflict_max_retries: int = 2
    # Human gate — block auto-merge for large/sensitive PRs
    human_gate_max_diff_lines: int = 0
    human_gate_sensitive_paths: str = ""
    human_gate_notify_comment: bool = True

    @property
    def human_gate_sensitive_path_list(self) -> list[str]:
        """Parse comma-separated glob patterns into a list."""
        if not self.human_gate_sensitive_paths:
            return []
        return [p.strip() for p in self.human_gate_sensitive_paths.split(",") if p.strip()]

    @property
    def agent_env(self) -> dict[str, str]:
        """Build env dict for Claude Agent SDK.

        Prefers OAuth token (uses subscription quota) over API key
        (bills to API credits). Only passes one auth method to avoid
        ambiguity — SDK gives OAuth priority when both are present.
        """
        env: dict[str, str] = {
            # Allow bypassPermissions when running as root in Docker.
            # Claude Code CLI rejects --dangerously-skip-permissions under
            # root unless IS_SANDBOX=1 signals a sandboxed environment.
            "IS_SANDBOX": "1",
        }
        if self.claude_oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.claude_oauth_token
        elif self.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_api_key
        if self.github_token:
            env["GITHUB_TOKEN"] = self.github_token
        return env


def parse_repos_config(raw_json: str) -> ReposConfig:
    """Parse REPOS_CONFIG JSON string into a ReposConfig.

    Expected format: JSON array of objects with url, path,
    and optional description fields.

    Example::

        [
            {"url": "https://github.com/org/api.git",
             "path": "/workspace/api",
             "description": "Backend API"}
        ]
    """
    entries = json.loads(raw_json)
    if not isinstance(entries, list):
        raise ValueError("REPOS_CONFIG must be a JSON array")

    all_repos = [
        RepoInfo(
            url=r["url"],
            path=r["path"],
            description=r.get("description", ""),
        )
        for r in entries
    ]

    if not all_repos:
        logging.getLogger(__name__).warning("REPOS_CONFIG is empty — no repos configured")

    return ReposConfig(all_repos=all_repos)


def _parse_assignee_map(
    raw_json: str,
) -> dict[str, tuple[str, str]]:
    """Parse COMPONENT_ASSIGNEE_MAP JSON into a dict.

    Expected format::

        {"backend": ["Бекенд", "user.login"],
         "frontend": ["Фронтенд", "other.login"]}
    """
    data = json.loads(raw_json)
    if not isinstance(data, dict):
        raise ValueError("COMPONENT_ASSIGNEE_MAP must be a JSON object")
    return {k: (v[0], v[1]) for k, v in data.items()}


def load_config() -> Config:
    """Load config from environment variables."""
    repos_json = os.environ.get("REPOS_CONFIG", "[]")
    repos_config = parse_repos_config(repos_json)

    return Config(
        tracker_token=os.environ["YANDEX_TRACKER_TOKEN"],
        tracker_org_id=os.environ["YANDEX_TRACKER_ORG_ID"],
        tracker_queue=os.environ.get("TRACKER_QUEUE", "QR"),
        tracker_tag=os.environ.get("TRACKER_TAG", "ai-task"),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        agent_max_budget_usd=float(os.environ["AGENT_MAX_BUDGET_USD"])
        if os.environ.get("AGENT_MAX_BUDGET_USD")
        else None,
        workspace_dir=os.environ.get("WORKSPACE_DIR", "/workspace"),
        repos_config=repos_config,
        workflow_prompt_path=os.environ.get("WORKFLOW_PROMPT_PATH", "prompts/workflow.md"),
        claude_oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        agent_model=os.environ.get("AGENT_MODEL", "claude-opus-4-6"),
        max_concurrent_agents=int(os.environ.get("MAX_CONCURRENT_AGENTS", "2")),
        worktree_base_dir=os.environ.get("WORKTREE_BASE_DIR", "/workspace/worktrees"),
        agent_permission_mode=os.environ.get("AGENT_PERMISSION_MODE", "acceptEdits"),
        review_check_delay_seconds=int(os.environ.get("REVIEW_CHECK_DELAY_SECONDS", "120")),
        needs_info_check_delay_seconds=int(os.environ.get("NEEDS_INFO_CHECK_DELAY_SECONDS", "120")),
        web_host=os.environ.get("WEB_HOST", "0.0.0.0"),
        web_port=int(os.environ.get("WEB_PORT", "8080")),
        tracker_project_id=int(os.environ.get("TRACKER_PROJECT_ID", "0")),
        tracker_boards=[int(b) for b in os.environ.get("TRACKER_BOARDS", "").split(",") if b.strip()],
        component_assignee_map=_parse_assignee_map(os.environ.get("COMPONENT_ASSIGNEE_MAP", "{}")),
        supervisor_enabled=os.environ.get("SUPERVISOR_ENABLED", "true").lower() == "true",
        supervisor_model=os.environ.get("SUPERVISOR_MODEL", "claude-opus-4-6"),
        supervisor_max_budget_usd=float(os.environ["SUPERVISOR_MAX_BUDGET_USD"])
        if os.environ.get("SUPERVISOR_MAX_BUDGET_USD")
        else None,
        db_path=os.environ.get("DB_PATH", "data/coder.db"),
        embedding_api_key=os.environ.get("EMBEDDING_API_KEY", os.environ.get("ZVENO_API_KEY", "")),
        embedding_base_url=os.environ.get("EMBEDDING_BASE_URL", "https://api.zveno.ai/v1"),
        supervisor_memory_dir=os.environ.get("SUPERVISOR_MEMORY_DIR", "data/memory"),
        supervisor_memory_index_path=os.environ.get("SUPERVISOR_MEMORY_INDEX_PATH", "data/memory/.index.sqlite"),
        supervisor_memory_auto_recall=os.environ.get("SUPERVISOR_MEMORY_AUTO_RECALL", "true").lower() == "true",
        compaction_enabled=os.environ.get("COMPACTION_ENABLED", "true").lower() == "true",
        compaction_buffer_tokens=int(os.environ.get("COMPACTION_BUFFER_TOKENS", "20000")),
        compaction_model=os.environ.get("COMPACTION_MODEL", "claude-haiku-4-5-20251001"),
        k8s_logs_enabled=os.environ.get("K8S_LOGS_ENABLED", "true").lower() == "true",
        k8s_namespace=os.environ.get("K8S_NAMESPACE", "dev"),
        heartbeat_interval_seconds=int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "300")),
        heartbeat_stuck_threshold_seconds=int(os.environ.get("HEARTBEAT_STUCK_THRESHOLD_SECONDS", "600")),
        heartbeat_long_running_threshold_seconds=int(
            os.environ.get("HEARTBEAT_LONG_RUNNING_THRESHOLD_SECONDS", "3600")
        ),
        heartbeat_review_stale_threshold_seconds=int(
            os.environ.get("HEARTBEAT_REVIEW_STALE_THRESHOLD_SECONDS", "1800")
        ),
        heartbeat_full_report_every_n=int(os.environ.get("HEARTBEAT_FULL_REPORT_EVERY_N", "3")),
        heartbeat_cooldown_seconds=int(os.environ.get("HEARTBEAT_COOLDOWN_SECONDS", "900")),
        auto_merge_enabled=os.environ.get("AUTO_MERGE_ENABLED", "false").lower() == "true",
        auto_merge_method=os.environ.get("AUTO_MERGE_METHOD", "squash"),
        auto_merge_require_approval=os.environ.get("AUTO_MERGE_REQUIRE_APPROVAL", "true").lower() == "true",
        pre_merge_review_enabled=os.environ.get("PRE_MERGE_REVIEW_ENABLED", "false").lower() == "true",
        pre_merge_review_model=os.environ.get("PRE_MERGE_REVIEW_MODEL", "claude-sonnet-4-20250514"),
        pre_merge_review_max_budget_usd=float(os.environ.get("PRE_MERGE_REVIEW_MAX_BUDGET_USD", "0.50")),
        pre_merge_review_timeout_seconds=int(os.environ.get("PRE_MERGE_REVIEW_TIMEOUT_SECONDS", "300")),
        pre_merge_review_fail_open=os.environ.get("PRE_MERGE_REVIEW_FAIL_OPEN", "false").lower() == "true",
        max_concurrent_reviews=int(os.environ.get("MAX_CONCURRENT_REVIEWS", "5")),
        alertmanager_webhook_enabled=os.environ.get("ALERTMANAGER_WEBHOOK_ENABLED", "false").lower() == "true",
        alertmanager_auto_create_task=os.environ.get("ALERTMANAGER_AUTO_CREATE_TASK", "false").lower() == "true",
        alertmanager_auto_task_queue=os.environ.get("ALERTMANAGER_AUTO_TASK_QUEUE", "QR"),
        alertmanager_auto_task_tag=os.environ.get("ALERTMANAGER_AUTO_TASK_TAG", "ai-task"),
        supervisor_escalation_tag=os.environ.get("SUPERVISOR_ESCALATION_TAG", "needs-human-review"),
        post_merge_verification_enabled=os.environ.get("POST_MERGE_VERIFICATION_ENABLED", "false").lower() == "true",
        post_merge_wait_for_ci=os.environ.get("POST_MERGE_WAIT_FOR_CI", "true").lower() == "true",
        post_merge_wait_for_k8s=os.environ.get("POST_MERGE_WAIT_FOR_K8S", "true").lower() == "true",
        post_merge_ci_timeout_seconds=int(os.environ.get("POST_MERGE_CI_TIMEOUT_SECONDS", "600")),
        post_merge_k8s_timeout_seconds=int(os.environ.get("POST_MERGE_K8S_TIMEOUT_SECONDS", "300")),
        post_merge_k8s_deployment=os.environ.get("POST_MERGE_K8S_DEPLOYMENT", ""),
        post_merge_verification_model=os.environ.get("POST_MERGE_VERIFICATION_MODEL", "claude-sonnet-4-20250514"),
        post_merge_verification_max_budget_usd=float(os.environ.get("POST_MERGE_VERIFICATION_MAX_BUDGET_USD", "1.0")),
        post_merge_verification_timeout_seconds=int(os.environ.get("POST_MERGE_VERIFICATION_TIMEOUT_SECONDS", "300")),
        post_merge_verification_environment=os.environ.get("POST_MERGE_VERIFICATION_ENVIRONMENT", "dev"),
        max_continuation_cost=(
            float(os.environ["MAX_CONTINUATION_COST"]) if os.environ.get("MAX_CONTINUATION_COST") else None
        ),
        merge_conflict_max_retries=int(os.environ.get("MERGE_CONFLICT_MAX_RETRIES", "2")),
        human_gate_max_diff_lines=int(os.environ.get("HUMAN_GATE_MAX_DIFF_LINES", "0")),
        human_gate_sensitive_paths=os.environ.get("HUMAN_GATE_SENSITIVE_PATHS", ""),
        human_gate_notify_comment=os.environ.get("HUMAN_GATE_NOTIFY_COMMENT", "true").lower() == "true",
    )
