# ZvenoAI Coder

> **CRITICAL: Every PR review comment — write a test first, then fix. Never fix blindly. See "Bug Fix Process" section.**
>
> **CRITICAL: NEVER push to main or merge a PR until ALL quality checks pass (`task quality` — lint, format, typecheck, tests). Zero failures allowed. Pre-existing failures must be fixed first.**

Python-based async orchestrator that polls Yandex Tracker for tasks tagged `ai-task` and dispatches Claude Agent SDK agents to execute them. Includes a real-time web dashboard for monitoring agent output and task status.

## Stack
- Python 3.12+
- claude-agent-sdk (Claude Agent SDK for Python)
- FastAPI + uvicorn (web dashboard)
- requests (Tracker REST API)
- PyYAML (repos.yaml config)
- React 19 + Vite + TypeScript + TailwindCSS (frontend)
- xterm.js (agent terminal streaming)
- pytest + pytest-asyncio (testing)

## Architecture
- **Agent SDK** — in-process agent execution via `ClaudeSDKClient`
- **Orchestrator Agent** — `OrchestratorAgent` handles worker result decisions (track PR, complete, fail, epic child events)
- **Agent-driven completion** — agent decides when task is done; PR tracking is informational
- **In-process MCP tools** — Tracker tools scoped per-issue (no external MCP processes)
- **Git worktrees** — per-task workspace isolation
- **Error recovery** — typed error classification + exponential backoff retry
- **Async concurrency** — `asyncio` with semaphore-controlled parallel agents
- **Epic coordination** — supervisor-driven epic management: auto-discovery of children (with auto-decomposition when no children found), then supervisor sets dependency graph and activation order via MCP tools (`awaiting_plan` → `executing`); supervisor oversees lifecycle events (preflight skips, epic completion) and can reset falsely-terminated children via `epic_reset_child`
- **Workpad** — persistent structured comment on Tracker issue; idempotent creation via hidden HTML marker (`<!-- workpad:QR-XXX -->`); auto-discovery on session resume; agent updates progress after each milestone via `tracker_create_workpad` / `tracker_update_workpad` MCP tools
- **Tracker status reconciliation** — periodic check for externally closed/cancelled tasks; phase-aware cleanup (running → TASK_FAILED, PR-tracked → cancel only, needs-info → TASK_FAILED); `removed` flag on tracked objects prevents stale reference races; `record_pr_cancelled()` with `cancelled_at` column in `pr_lifecycle`
- **Multi-turn continuation** — retries agent up to `MAX_CONTINUATION_TURNS` (default 3) when it completes without PR and task is still open; `tracker_mark_complete` tool for explicit no-PR completion signal; `continuation_exhausted` flag triggers retry on 2nd+ attempt; cost cap guard (`MAX_CONTINUATION_COST`); Tracker status check detects external resolution during continuation
- **Merge conflict retry** — SHA-gated retry for merge conflicts (up to `MERGE_CONFLICT_MAX_RETRIES`, default 2); only re-prompts when agent pushes new commit but conflict persists; resets on resolution
- **Heartbeat monitor** — periodic health checks (every 5 min) detecting stuck agents, long-running tasks, and stale reviews; alerts supervisor with actionable diagnostics and cooldown-based deduplication; enriched with cost, tokens, output snippets, and Tracker status
- **PR auto-merge** — opt-in automatic merge when CI green + reviews approved + no conflicts; uses GitHub GraphQL `enablePullRequestAutoMerge` with REST fallback; supervisor can also merge manually via `github_merge_pr` tool
- **Pre-merge code review** — one-shot Sonnet sub-agent reviews PR diff against task requirements, project conventions, and OWASP security checklist before auto-merge; fail-close by default (rejects on error/timeout, configurable via `PRE_MERGE_REVIEW_FAIL_OPEN`); posts REQUEST_CHANGES on reject so worker agent self-corrects; sends rejection prompt directly to worker session; notifies supervisor on rejection; auto-resets on new commits (head SHA tracking) for fresh review cycles; on re-review approve, posts APPROVE to dismiss stale REQUEST_CHANGES so auto-merge can proceed; configurable timeout (`pre_merge_review_timeout_seconds`)
- **Human gate** — blocks auto-merge for PRs exceeding diff size threshold (`HUMAN_GATE_MAX_DIFF_LINES`, default 0 = disabled) or touching sensitive file paths (`HUMAN_GATE_SENSITIVE_PATHS`, comma-separated globs); posts PR comment explaining the block; publishes `HUMAN_GATE_TRIGGERED` event
- **Post-merge verification** — after PR merge, watches CI pipeline and K8s rollout, then spawns a one-shot verification sub-agent to test the deployed change on dev; on pass publishes `TASK_VERIFIED`; on fail auto-creates hotfix Bug task; fire-and-forget (doesn't block merge flow); configurable via `POST_MERGE_VERIFICATION_ENABLED`
- **Environment config** — SQLite-backed key-value store for per-environment connection details (API URLs, test credentials); supervisor writes via `env_set` MCP tool, worker agents read via `env_get`; used by verification sub-agent to connect to dev/staging
- **Event bus** — async pub/sub for real-time streaming to web dashboard
- **Web dashboard** — FastAPI REST + WebSocket, React frontend with xterm.js
- **Supervisor chat** — interactive + autonomous streaming chat with full authority (`bypassPermissions`); auto-sends epic planning requests via `auto_send()`
- **Supervisor memory** — SQLite + FTS5 hybrid search over markdown files (`data/memory/`); see "Supervisor Memory" section below
- **Inter-agent communication** — centralized mailbox with interrupt-based message delivery between concurrent worker agents; see "Inter-Agent Communication" section below
- **Task dependency management** — auto-defers tasks with unresolved dependencies detected via Tracker links (`depends on`, `is blocked by`) and LLM-based text extraction from descriptions (Haiku); rechecks on every poll; supervisor can override via MCP tools (`approve_task_dispatch`, `defer_task`); epic children excluded (use EpicCoordinator); fail-open on API/LLM errors
- **K8s diagnostics** — optional Kubernetes pod log/status inspection via in-cluster ServiceAccount auth; feature-gated via `K8S_LOGS_ENABLED` env var
- **Persistent stats** — SQLite-backed storage for task runs, errors, PR lifecycles via EventBus subscriber
- **Auto-compaction** — summarizes context via Haiku when approaching token limit, recreates session to continue work without losing progress
- **Session resumption** — captures `session_id` from Claude SDK, persists in SQLite; on restart, resumes PR monitoring and needs-info sessions with `resume` + `fork_session=True` for full conversation history restoration (graceful fallback to fresh session on failure); fresh fallback sessions receive context prompt with task description, recent comments, and inter-agent message history to enable effective continuation
- **Dead session recovery** — on-demand sessions that fail (crash, timeout, `success=False`) are automatically recreated with context preserved via session_id resume; triggers on next message delivery or interrupt attempt; publishes `SESSION_RECREATED` event for observability

### Supervisor Memory

Long-term memory for the supervisor agent, persisted across sessions.

**Storage:** Markdown files in `data/memory/` are the source of truth. An SQLite index provides fast retrieval via hybrid search (BM25 keyword + cosine vector similarity).

**How it works:**
1. Supervisor writes decisions, learnings, and context to markdown files via MCP tools (`memory_write`, `memory_read`, `memory_search`)
2. On startup, `SupervisorRunner` indexes all markdown files: splits into overlapping chunks (~400 tokens each), computes embeddings via Gemini API, stores in SQLite with FTS5
3. On search, queries run both BM25 (keyword) and vector similarity, results merged with configurable weights (0.3 BM25 / 0.7 vector)
4. Index auto-refreshes when file content changes (content hash check)

**Key modules:**
- `orchestrator/supervisor_memory.py` — `MemoryIndex` (indexing, search, CRUD), chunking, embedding, hybrid ranking
- `orchestrator/supervisor.py` — `SupervisorRunner` (initialization, index rebuild on startup)
- `orchestrator/supervisor_tools.py` — MCP tools (`memory_write`, `memory_read`, `memory_search`) exposed to supervisor agent

### Inter-Agent Communication

Enables concurrent worker agents to coordinate when working on related tasks (e.g., frontend + backend for the same feature).

**How it works:**
1. Agent X calls `list_running_agents` to discover peers (includes `component`/`repo` metadata for informed targeting)
2. Agent X calls `send_message_to_agent("QR-Y", "What API endpoint are you creating?")` (non-blocking, delivery status returned)
3. Message is delivered to Agent Y via `session.interrupt_with_message()` — Y sees it immediately
4. Agent Y calls `reply_to_message(msg_id, "POST /api/v1/auth/login")`
5. Reply is delivered back to Agent X via interrupt
6. For blocking coordination, Agent X can use `send_request_to_agent("QR-Y", "...", timeout_seconds=60)` which waits for a reply

**Message types:** `REQUEST` (expects reply), `RESPONSE` (reply to REQUEST), `NOTIFICATION` (informational), `ARTIFACT` (data transfer)

**Delivery statuses:** `DELIVERED` (interrupt reached live session), `QUEUED` (interrupt failed, message in inbox), `OVERFLOW_DROPPED` (evicted due to MAX_INBOX_SIZE=50; only set if message was never delivered)

**Architecture:**
- `AgentMailbox` — centralized singleton owned by `Orchestrator`, manages per-agent inboxes (deques with MAX_INBOX_SIZE=50), message lifecycle (pending→read→replied/expired), delivery tracking, stats, periodic cleanup
- `comm_tools.py` — 5 MCP tools (`list_running_agents`, `send_message_to_agent`, `send_request_to_agent`, `reply_to_message`, `check_messages`) scoped per-agent via closure
- Delivery uses existing `AgentSession.interrupt_with_message()` — no new transport
- Supervisor has read-only access via `view_agent_messages` and `get_comm_stats` tools
- **Mailbox lifecycle** — agents stay registered while their session is alive (including PR monitor, needs-info monitor, and on-demand session phases). Unregistration happens only at terminal paths: task completion, failure, PR merge/close, or graceful shutdown. This ensures agents are discoverable and messageable throughout their full lifecycle.
- **Periodic cleanup** — `_periodic_mailbox_cleanup()` in `main.py` removes terminal messages (REPLIED/EXPIRED) older than 1 hour every 30 minutes

**Key modules:**
- `orchestrator/agent_mailbox.py` — `AgentMailbox`, `AgentMessage` (with `msg_type`, `delivery_status`, `created_at`), `AgentInfo` (with `component`, `repo`), `MessageType`, `DeliveryStatus`, `request_and_wait()`, `get_stats()`, `cleanup_terminal_messages()`
- `orchestrator/comm_tools.py` — `build_comm_server()` returns per-agent MCP server with 5 tools
- `orchestrator/main.py` — wires mailbox callbacks (`_list_agent_info`, `_interrupt_agent_for_comm`)

## Structure
- `orchestrator/compaction.py` — context compaction: `should_compact()`, `summarize_output()`, `build_continuation_prompt()`
- `orchestrator/config.py` — env-based config with SDK fields
- `orchestrator/constants.py` — shared enums and type aliases (EventType, PRState, MAX_COMPACTION_CYCLES, MAX_CONTINUATION_TURNS)
- `orchestrator/tracker_client.py` — Yandex Tracker REST client
- `orchestrator/tracker_enums.py` — enums and helpers for Tracker status keyword matching
- `orchestrator/tracker_types.py` — TypedDict type definitions for Tracker API responses
- `orchestrator/tracker_tools.py` — in-process MCP @tool wrappers for worker agents
- `orchestrator/heartbeat.py` — periodic agent health monitoring (HeartbeatMonitor, AgentHealthReport, HeartbeatResult), stuck/long-running/stale detection, supervisor alerting with cooldown
- `orchestrator/github_client.py` — GitHub GraphQL client for PR review thread and CI monitoring, auto-merge (enablePullRequestAutoMerge), merge readiness checks (MergeReadiness)
- `orchestrator/orchestrator_agent.py` — Opus-level agent that decides on worker results (track PR, complete, fail, epic child events)
- `orchestrator/orchestrator_tools.py` — MCP tools for orchestrator agent (track_pr, retry_task, escalate, fail_task, complete_task, create_follow_up, get_task_history, get_recent_events)
- `orchestrator/supervisor_tools.py` — in-process MCP @tool wrappers + `build_supervisor_allowed_tools()` for supervisor chat (Tracker, GitHub, stats, memory, epic management, Bash, Write, Edit)
- `orchestrator/supervisor_chat.py` — interactive + autonomous streaming chat with supervisor (SupervisorChatManager, `auto_send()`, bypassPermissions)
- `orchestrator/supervisor_memory.py` — supervisor memory system (SQLite + FTS5 hybrid search, markdown files, chunking, embeddings)
- `orchestrator/supervisor.py` — supervisor memory system initialization (SupervisorRunner, memory_index, embedder)
- `orchestrator/dependency_manager.py` — task dependency deferral: `DependencyManager` (check/recheck/approve/defer), `DeferredTask` dataclass, Tracker link parsing, LLM-based text blocker extraction via Haiku (`extract_blocker_keys_from_text`)
- `orchestrator/supervisor_prompt_builder.py` — `build_supervisor_system_prompt` (re-export) + `build_epic_plan_prompt`, `build_preflight_skip_prompt`, `build_epic_completion_prompt`, `build_task_deferred_prompt`, `build_task_unblocked_prompt`, `build_heartbeat_prompt`, `build_epic_decompose_prompt`, `build_escalation_prompt` for autonomous supervisor notifications
- `orchestrator/alertmanager_webhook.py` — Alertmanager webhook dataclasses, parsing, prompt formatting, and auto-task creation helpers (AlertmanagerAlert, AlertmanagerPayload, parse_payload, format_alert_prompt, build_issue_summary, build_issue_description, map_component)
- `orchestrator/adr.py` — Architecture Decision Records: `create_adr`, `list_adrs`, `read_adr`, `slugify`
- `orchestrator/escalation.py` — supervisor uncertainty escalation: `escalate_to_human`, `build_escalation_comment`
- `orchestrator/repo_resolver.py` — git repo cloning/pulling
- `orchestrator/workspace.py` — git worktree management
- `orchestrator/workspace_tools.py` — MCP workspace tools (lazy worktree creation)
- `orchestrator/recovery.py` — error classification + retry logic + state persistence
- `orchestrator/prompt_builder.py` — task prompt construction (`build_task_prompt`, `build_review_prompt`, `build_needs_info_response_prompt`, `build_fallback_context_prompt` for fresh on-demand sessions, `build_merge_conflict_prompt`, `build_pipeline_failure_prompt`, `build_pre_merge_rejection_prompt`)
- `orchestrator/agent_mailbox.py` — inter-agent communication mailbox (message routing, inbox management, interrupt-based delivery)
- `orchestrator/comm_tools.py` — MCP tools for worker agent communication (list peers, send/reply/check messages)
- `orchestrator/k8s_client.py` — Kubernetes client for pod logs/status inspection (in-cluster ServiceAccount auth, graceful degradation)
- `orchestrator/agent_runner.py` — SDK client wrapper
- `orchestrator/event_bus.py` — async pub/sub event bus
- `orchestrator/stats_collector.py` — EventBus subscriber that persists statistics to storage
- `orchestrator/stats_models.py` — data models for persistent statistics (TaskRun, SupervisorRun, etc.)
- `orchestrator/metrics.py` — Prometheus metrics registry with text format serialization (Counter, Gauge, Histogram) for VictoriaMetrics export via /metrics endpoint
- `orchestrator/storage.py` — abstract Storage Protocol interface for persistence backends
- `orchestrator/sqlite_storage.py` — SQLite-backed storage implementation
- `orchestrator/_persistence.py` — mixin for background asyncio task-based persistence
- `orchestrator/task_dispatcher.py` — Tracker polling + agent dispatch
- `orchestrator/epic_coordinator.py` — epic child discovery (`discover_children` → `awaiting_plan` or `needs_decomposition`), supervisor-driven activation (`activate_child`, `set_child_dependencies`), dependency-aware child sequencing, `register_child`, `rediscover_children`
- `orchestrator/post_merge_verifier.py` — post-merge verification: CI watch, K8s rollout watch, one-shot verification sub-agent, hotfix task creation on failure (PostMergeVerifier, VerificationResult, VerificationIssue)
- `orchestrator/pr_monitor.py` — PR review/CI monitoring + merge conflict detection + auto-merge processing + pre-merge review gate + human gate for large/sensitive PRs + post-merge verification trigger
- `orchestrator/pre_merge_reviewer.py` — one-shot sub-agent for semantic code review before auto-merge (ReviewVerdict, ReviewIssue, context assembly, OWASP security checklist, configurable fail-open/fail-close)
- `orchestrator/needs_info_monitor.py` — needs-info status monitoring
- `orchestrator/proposal_manager.py` — improvement proposal lifecycle
- `orchestrator/web.py` — FastAPI REST + WebSocket server
- `orchestrator/main.py` — async orchestrator loop + web server + epic event watcher (auto-triggers supervisor for epic planning, decomposition, heartbeat)
- `prompts/workflow.md` — workflow instructions for worker agents
- `prompts/supervisor_workflow.md` — workflow instructions for supervisor agent
- `prompts/plan_agent.md` — planning sub-agent prompt (read-only codebase exploration)
- `prompts/critic_agent.md` — critic sub-agent prompt; reviews the plan for wrong abstractions, missing edge cases, side effects on related systems, and convention violations; iterates with plan_agent until approved (max 3 rounds)
- `prompts/simplify_agent.md` — simplify sub-agent prompt; 3 parallel instances review changes for code reuse, code quality, and efficiency before commit; directly fixes issues found
- `prompts/code_quality_gate.md` — code quality gate sub-agent prompt; read-only compliance check against CLAUDE.md Code Quality Rules; returns approve/revise verdict; worker iterates until approved (max 3 rounds)
- `frontend/` — React + Vite + TypeScript dashboard
- `tests/` — pytest tests
- `Taskfile.yml` — quality check tasks (Docker-based, synced with CI) + security tasks (Semgrep, Gitleaks)
- `.semgrep.yml` — custom Semgrep rules for OWASP security scanning (Python + TypeScript)
- `docs/decisions/` — Architecture Decision Records (ADRs)
- `ci/python.Dockerfile` — Python CI image (ruff, mypy, pytest, pip-audit)
- `ci/frontend.Dockerfile` — Frontend CI image (Node 22, npm deps)

## Python Style Guide (based on Google Python Style Guide)

All Python code in this project follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) with project-specific additions below.

### Imports

**Ordering** (separated by blank lines, each group sorted lexicographically by full package path):
1. `from __future__` imports
2. Standard library (`import os`, `import asyncio`)
3. Third-party packages (`import fastapi`, `import yaml`)
4. Local project imports (`from orchestrator.config import Settings`)

**Rules:**
- `import x` for packages and modules only — never for individual classes or functions
- `from x import y` where `x` is the package prefix and `y` is the module name
- `from x import y as z` when names conflict or are too long
- Never use relative imports (`from . import foo`) — always use the full package path
- Each import on its own line (exception: `typing` and `collections.abc` imports may be combined)
- Symbols from `typing`, `collections.abc`, `typing_extensions` may be imported directly

### Naming

| Type | Convention | Example |
|------|-----------|---------|
| Packages/Modules | `lower_with_under` | `agent_runner.py` |
| Classes | `CapWords` | `TaskDispatcher` |
| Exceptions | `CapWords` + `Error` | `TrackerApiError` |
| Functions/Methods | `lower_with_under` | `dispatch_task()` |
| Constants (module-level) | `ALL_CAPS` | `MAX_COMPACTION_CYCLES` |
| Instance variables | `lower_with_under` | `self.issue_key` |
| Protected/internal | `_single_leading_underscore` | `_parse_response()` |
| Type variables | `CapWords` | `T`, `EventT` |

- Never use `__double_leading_underscore` for class members — prefer single `_`
- Never encode type in variable name (`id_to_name_dict` → `id_to_name`)
- Test methods: `test_<method>_<state>` pattern

### Type Annotations

- Required on all public functions and methods
- Update annotations when modifying code
- Do not annotate `self` or `cls` unless necessary
- Use `is None` / `is not None` — never `== None`
- Define type aliases for complex types (shared aliases go in `constants.py`)
- Use `# type: ignore` sparingly with an explanatory comment
- For conditional imports (circular deps):
  ```python
  from __future__ import annotations
  import typing
  if typing.TYPE_CHECKING:
      from orchestrator.expensive_module import HeavyClass
  ```

### Docstrings

- Triple double-quotes `"""` always
- Required for: all modules, all public functions/methods, all classes, nontrivial private functions
- Style: pick descriptive (`"""Fetches rows."""`) or imperative (`"""Fetch rows."""`) — be consistent within a file

**Function/method docstring sections** (in order):
```python
def dispatch_task(issue_key: str, priority: int = 0) -> TaskRun:
    """Dispatches a task to the next available agent.

    Validates the issue, creates a worktree, and starts an agent session.

    Args:
        issue_key: Yandex Tracker issue key (e.g., 'QR-123').
        priority: Dispatch priority. Higher values run first.

    Returns:
        A TaskRun instance with the agent session result.

    Raises:
        TrackerApiError: If the Tracker API is unreachable.
        WorkspaceError: If worktree creation fails.
    """
```

**Class docstring:** describe what an instance represents + `Attributes:` section for public attributes.

### Comments

- Explain *why*, not *what* — assume the reader knows Python
- Inline comments: at least 2 spaces from code, `# ` followed by text
- TODO format: `# TODO: link/context - Description.` — never reference individuals

### Exception Handling

- Use built-in exceptions when appropriate (`ValueError`, `TypeError`, etc.)
- Custom exceptions: inherit from existing exception class, name ends with `Error`
- **Never use bare `except:`** or catch `Exception` unless re-raising or at an isolation point
- Minimize `try` block size
- Use `finally` for cleanup
- **Never use `assert` for runtime logic** — asserts can be stripped with `-O`. Only use in tests

### Line Length and Formatting

- Maximum **80 characters** per line
- Exceptions: long imports, URLs in comments, long string constants
- **No backslash continuation** — use implicit joining inside `()`, `[]`, `{}`
- **4 spaces** per indent level, never tabs
- **Two blank lines** between top-level definitions
- **One blank line** between methods within a class
- **Trailing commas** when closing token is on a separate line
- **No semicolons** — never terminate lines with `;`, never two statements on one line

### True/False Evaluations

- Use implicit false: `if not items:` instead of `if len(items) == 0:`
- None checks: always `if x is None:` / `if x is not None:`
- Never compare booleans: `if flag:` not `if flag == True:`

### Default Arguments

- **Never use mutable defaults** (lists, dicts, sets):
  ```python
  # BAD
  def foo(items: list[str] = []) -> None: ...

  # GOOD
  def foo(items: list[str] | None = None) -> None:
      if items is None:
          items = []
  ```

### String Formatting

- Prefer f-strings for general formatting
- **Logging: always use `%`-style**, never f-strings:
  ```python
  # GOOD
  logger.info("Processing %d items for %s", count, user_id)

  # BAD
  logger.info(f"Processing {count} items for {user_id}")
  ```
- Never use `+`/`+=` for string accumulation in loops — use `"".join()`

### Functions and Methods

- Prefer small, focused functions (~40 lines soft limit)
- **Never use `staticmethod`** — use a module-level function instead
- Use `classmethod` only for named constructors or class-specific routines

### Properties

- Use `@property` for trivial computed access that feels like attribute access
- Must be cheap and unsurprising — no I/O, no expensive computation

### Comprehensions

- Use for simple cases only
- No multiple `for` clauses or complex filter expressions — use a regular loop instead

### Lambda

- Only for one-liners (< 60-80 chars). Otherwise use a named `def`
- Prefer `operator` module over lambdas for common operations

### Global State

- Avoid mutable global state
- Module-level constants are fine: `ALL_CAPS` naming, `_` prefix if internal

### Files and Resources

- Always explicitly close files/sockets/connections
- Use `with` statements (or `contextlib.closing()`)

### Threading and Async

- Don't rely on atomicity of built-in types
- Use `asyncio` primitives for concurrency (locks, queues, semaphores)
- Use `queue.Queue` for inter-thread communication

### General Conventions

- Tests use pytest with mocking (`unittest.mock`)
- Async tests use pytest-asyncio (auto mode)
- Config from environment variables with sensible defaults
- `if __name__ == "__main__": main()` guard for executable modules

## Code Quality Rules

These rules are derived from recurring PR review issues. Follow them strictly.

### No cross-module private imports
Never import `_private` names from other modules. If you need a function/constant from another module, make it public or create a shared module.
```python
# BAD: fragile coupling to internal details
from orchestrator.needs_info_monitor import _NEEDS_INFO_STATUSES

# GOOD: import public API
from orchestrator.needs_info_monitor import is_needs_info_status
```

### No function duplication across modules
If two modules need the same utility, define it once and import it. Never copy-paste a function.
```python
# BAD: copy-paste of build_system_prompt_append
def build_supervisor_system_prompt(path): ...  # same logic

# GOOD: re-export or direct import
from orchestrator.prompt_builder import build_system_prompt_append as build_supervisor_system_prompt
```

### Types belong in the module that owns the concept
Shared type aliases (Literal types, type unions) used by multiple modules go in `constants.py`. Feature-specific types stay in their module. Core modules must NEVER import from feature-specific modules.
```python
# BAD: core module imports from feature module
# pr_monitor.py
from orchestrator.epic_coordinator import ChildStatus

# GOOD: shared type lives in constants.py
from orchestrator.constants import EventType
```

### Propagate data through all paths
When adding a field to a dataclass (e.g. `issue_summary`), trace ALL paths where the dataclass is created or passed via callbacks. Update every callback type signature, every creation site, and every consumer. Common miss: the needs-info → PR monitor path vs the direct dispatch → PR monitor path.

### Consistent locking in paired operations
If `approve()` uses a lock, `reject()` must also use a lock. Symmetric operations require symmetric concurrency protection.

### Wall-clock vs monotonic time
- **`time.time()`** — for external-facing timestamps (API responses, event history, user display)
- **`time.monotonic()`** — for internal cooldowns, rate limiting, elapsed time checks

Never mix them. If a property is exposed via API or stored for later display, use wall-clock.

### Comments must match behavior
When commenting queue/eviction semantics ("drops oldest", "evicts newest"), verify the actual behavior matches. `asyncio.Queue` is FIFO — `get_nowait()` removes the oldest, `put_nowait()` on a full queue raises `QueueFull` (drops newest).

### Consistent overflow strategies
Use the same overflow handling pattern within a subsystem. If supervisor trigger queue evicts oldest with a warning, document why other queues (event bus) silently drop instead.

### Never discard async results silently
Every `await session.send()` returns `AgentResult`. Never discard the result without handling. If the result is not needed — document why explicitly. When draining/resuming — merge results (accumulate costs, prefer latest pr_url/needs_info).
```python
# BAD: result silently discarded
await session.send(msg)

# GOOD: merge into base result
drain_result = await session.send(msg)
result = _merge_results(result, drain_result)
```

### Sticky flags in merge/reduce functions
When merging dataclass instances (e.g. `_merge_results`), classify each field's merge semantics:
- **Sticky flags** (`success`) — once `True`, must stay `True`. Use `base.X or update.X`.
- **Latest-wins** (`output`, `pr_url`, `needs_info`) — prefer update, fallback to base. Use `update.X or base.X`.
- **Accumulators** (`cost_usd`, `duration_seconds`, `proposals`) — sum or concatenate.

Never apply "latest-wins" to a sticky flag — a failed drain must not downgrade a successful base result.

### Event lifecycle contract
Every task dispatch path must produce exactly one terminal event. Invariant: **every TASK_STARTED must end with exactly one of**: TASK_COMPLETED, TASK_FAILED, or PR_TRACKED.

| Outcome | Required events | Records |
|---------|----------------|---------|
| Success with PR | TASK_STARTED → PR_TRACKED | 1 task_run + 1 pr_lifecycle |
| Success without PR | TASK_STARTED → TASK_COMPLETED | 1 task_run |
| Failure | TASK_STARTED → TASK_FAILED | 1 task_run + 1 error_log |
| Needs-info → PR | TASK_STARTED → NEEDS_INFO → PR_TRACKED | 1 task_run + 1 pr_lifecycle |
| PR merged + verified | PR_TRACKED → PR_MERGED → TASK_VERIFIED | 1 task_run + 1 pr_lifecycle (verified_at set) |
| PR merged + verification failed | PR_TRACKED → PR_MERGED → VERIFICATION_FAILED | 1 task_run + 1 pr_lifecycle + 1 error_log |

**Note (QR-247):** TASK_COMPLETED events from `complete_task_impl` always include `has_pr: False`. If the agent's output appears suspicious (too short or contains waiting/blocked patterns), the event also includes `no_pr_warning: True` to flag potential false completions.

### Test async consumers with queue.join()
Don't compete with `_run()` for queue items. Instead of `get_nowait()` in tests, use `queue.join()` + `task_done()` to wait for processing completion.

### Test hygiene: parametrize and deduplicate
Periodically review tests for bloat. Apply these rules:

- **Table-driven tests** — when multiple test methods differ only in inputs/expected values, collapse them into one `@pytest.mark.parametrize`. This is the Python equivalent of Go's table-driven tests:
  ```python
  @pytest.mark.parametrize(
      ("input_val", "expected"),
      [
          ("rate limit exceeded", ErrorCategory.RATE_LIMIT),
          ("401 Unauthorized", ErrorCategory.AUTH),
          ("some unknown error", ErrorCategory.PERMANENT),
      ],
  )
  def test_classify(self, input_val, expected) -> None:
      assert classify_error(input_val) == expected
  ```
- **Remove redundant assertions** — if `test_initial_state` checks `backoff == 0.0`, don't repeat that assertion at the start of `test_backoff_increases`.
- **Merge overlapping tests** — two tests that both verify "non-retryable category → `should_retry is False`" for different categories should be one parametrized test.
- **Hoist repeated imports** — `import time` or `from module import X` scattered inside test methods should be at module level.
- **Delete useless tests** — trivial constant-membership checks (`X in SET`) can be folded into behavioral tests that already exercise that logic.

### Fail-open vs fail-closed must be explicit
When changing error handling from fail-closed (reject/block on error) to fail-open (allow/proceed on error) or vice versa — add a comment at the point of change explaining the reasoning. This is a safety-critical decision that must be documented in code, not just in the PR description.
```python
# BAD: silent inversion of safety behavior
except Exception:
    logger.warning("Error checking PR for %s", task_key)
    # (previously returned error to caller — now silently continues)

# GOOD: explicit reasoning
except Exception:
    # Fail-open: supervisor explicitly decided to skip this task,
    # so infrastructure errors should not block their intent.
    # The old fail-closed behavior caused QR-266 where stale cache
    # blocked legitimate skips for 5+ retries.
    logger.warning("Error checking PR for %s - allowing skip (fail-open)", task_key)
```

### Event data contracts
When an event handler requires specific fields in `event.data`, document the required fields next to the `EventType` definition or in the handler's docstring. When adding a new required field to `event.data`, search for ALL publishers of that event type and verify they provide it.
```python
# BAD: handler silently requires pr_url but contract is undocumented
async def _on_pr_merged(self, event: Event) -> None:
    pr_url = event.data.get("pr_url")  # where is this documented?

# GOOD: contract documented at the event type
class EventType(str, Enum):
    PR_MERGED = "pr_merged"  # data: {"pr_url": str}
```

### Self-review before commit
Before creating a commit, re-read the full diff and verify:
- No duplicated code blocks (including SQL queries, mock setup, test boilerplate)
- All helper functions and fixtures updated to match new logic (no dead parameters)
- Tests that differ only in inputs collapsed via `@pytest.mark.parametrize`
- No mocks/parameters that no longer affect the code path under test

### Lifecycle tests for stateful multi-cycle flows
Unit tests that verify individual steps (reject, approve, merge) catch isolated bugs but miss state-transition failures. Any feature with a multi-step lifecycle (reject → fix → re-review → merge, dispatch → CI fail → fix → CI pass → merge, etc.) MUST have at least one end-to-end scenario test that walks through the full cycle.

Common pattern: a flag set in step N blocks step M, but no test ever runs steps N and M sequentially.

**Rule:** For every stateful flow, write a scenario test that exercises the full lifecycle — not just each step in isolation.
```python
# BAD: only tests reject and approve separately
def test_reject_posts_request_changes(self): ...
def test_approve_enables_auto_merge(self): ...

# GOOD: tests the full reject → fix → approve → merge lifecycle
async def test_reject_then_fix_then_approve_merges(self):
    # 1. Review rejects — posts REQUEST_CHANGES
    # 2. Worker fixes — new commit resets flags
    # 3. Re-review approves — dismisses stale review
    # 4. Auto-merge succeeds
```

## Development Process (TDD)

Follow test-driven development:

1. **Write a failing test** — define the expected behavior before writing implementation
2. **Make it pass** — write the minimum code to pass the test
3. **Refactor** — clean up while keeping tests green

This applies to new features, bug fixes, and refactors alike.

## Documentation

After any significant changes (logic, architecture, new modules, changed interfaces) — update the README and relevant sections of this CLAUDE.md (Structure, Architecture, etc.) to keep documentation in sync with the codebase.

## Bug Fix Process (Review Comments)

When handling review comments (from Cursor Bugbot, human reviewers, etc.):

1. **Create a test case first** — write a test that reproduces the reported bug
2. **Run the test** — if the test fails, the bug is confirmed
3. **Fix the bug** — make the test pass
4. **If the test passes without changes** — the bug report is incorrect; delete the test

This applies to ALL review comments — any comment may be wrong. Never fix blindly.

5. **Search for similar issues across the project** — every bug or review comment is a signal that the same pattern may exist elsewhere. After fixing the reported instance, search the entire codebase for analogous problems and fix them all in the same commit.

## Quality Checks (Taskfile + Docker)

All code quality checks run in disposable Docker containers via [Task](https://taskfile.dev). This ensures reproducibility regardless of the local environment. The same checks run in GitHub Actions CI.

### Prerequisites
- Docker
- [Task](https://taskfile.dev) (`brew install go-task` on macOS)

### Commands
```bash
# Run ALL quality checks (Python + Frontend in parallel)
task quality

# Python only
task python:quality    # lint -> format:check -> typecheck -> test

# Frontend only
task frontend:quality  # typecheck -> lint -> test -> build

# Individual checks
task lint              # ruff check
task format:check      # ruff format --check
task typecheck         # mypy orchestrator/
task test              # pytest with 75% coverage threshold
task audit             # pip audit (non-blocking)
task frontend:typecheck
task frontend:lint
task frontend:test
task frontend:build

# Dev convenience (modifies files on host via volume mount)
task format            # ruff format (auto-fix)
task lint:fix          # ruff check --fix (auto-fix)
```

### How it works
- `ci/python.Dockerfile` -- Python 3.12 image with dev deps (ruff, mypy, pytest, pip-audit)
- `ci/frontend.Dockerfile` -- Node 22 image with npm deps
- Docker images are rebuilt only when source files change (checksum-based)
- `task quality` builds images once, then runs all checks in `--rm` containers
- `task format` / `task lint:fix` mount `orchestrator/` and `tests/` back to host for write-back

### Sync with CI
GitHub Actions workflow (`.github/workflows/quality.yml`) uses the same Taskfile:
- `task python:quality` + `task audit` (continue-on-error)
- `task frontend:quality`

To add or change a check -- edit `Taskfile.yml`. Both local and CI pick it up automatically.

## Virtual Environment

For running the orchestrator locally (not quality checks), use the virtual environment:

```bash
# Create venv (once)
python3 -m venv .venv

# Activate
source .venv/bin/activate
```

## Running
```bash
# Activate venv first
source .venv/bin/activate

# Install
pip install -e ".[dev]"

# Tests (prefer `task test` for Docker-isolated run)
pytest tests/ -v

# Run orchestrator
python -m orchestrator.main
```

## Frontend
```bash
cd frontend
npm install
npm run dev    # dev server with proxy to :8080
npm run build  # production build to frontend/dist/
```

## Docker
```bash
docker compose build
docker compose up -d
# Dashboard at http://localhost:8080
```
