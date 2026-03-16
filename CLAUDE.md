# ZvenoAI Coder

> **CRITICAL: Every PR review comment — write a test first, then fix. Never fix blindly. See "Bug Fix Process" section.**
>
> **CRITICAL: NEVER push to main or merge a PR until ALL quality checks pass (`task quality` — lint, format, typecheck, tests). Zero failures allowed. Pre-existing failures must be fixed first.**

Python-based async orchestrator that polls Yandex Tracker for tasks tagged `ai-task` and dispatches Claude Agent SDK agents to execute them. Includes a real-time web dashboard for monitoring agent output and task status.

## Stack
- Python 3.12+, claude-agent-sdk, FastAPI + uvicorn, requests, PyYAML
- React 19 + Vite + TypeScript + TailwindCSS, xterm.js
- pytest + pytest-asyncio

## Architecture
- **Agent SDK** — in-process execution via `ClaudeSDKClient`
- **Orchestrator Agent** — `OrchestratorAgent` handles worker result decisions (track PR, complete, fail, epic child events)
- **Agent-driven completion** — agent decides when task is done; PR tracking is informational
- **In-process MCP tools** — Tracker tools scoped per-issue (no external MCP processes)
- **Git worktrees** — per-task workspace isolation
- **Error recovery** — typed error classification + exponential backoff retry
- **Async concurrency** — `asyncio` with semaphore-controlled parallel agents
- **Epic coordination** — supervisor-driven: auto-discovery of children (with auto-decomposition), dependency graph via MCP tools (`awaiting_plan` → `executing`), lifecycle events, `epic_reset_child`
- **Workpad** — persistent structured comment on Tracker issue; idempotent via hidden HTML marker; agent updates progress via MCP tools
- **Tracker status reconciliation** — periodic check for externally closed/cancelled tasks; phase-aware cleanup; `removed` flag prevents stale reference races
- **Multi-turn continuation** — retries agent up to `MAX_CONTINUATION_TURNS` (3) when it completes without PR and task is still open; `tracker_mark_complete` for explicit no-PR completion; cost cap guard (`MAX_CONTINUATION_COST`)
- **Merge conflict retry** — SHA-gated retry (up to `MERGE_CONFLICT_MAX_RETRIES` = 2); resets on resolution
- **Heartbeat monitor** — periodic health checks (every 5 min) detecting stuck agents, long-running tasks, stale reviews; cooldown-based deduplication
- **PR auto-merge** — opt-in when CI green + reviews approved + no conflicts; GitHub GraphQL `enablePullRequestAutoMerge` with REST fallback
- **Pre-merge code review** — one-shot Sonnet sub-agent reviews PR diff; fail-close by default (`PRE_MERGE_REVIEW_FAIL_OPEN`); posts REQUEST_CHANGES on reject; auto-resets on new commits for fresh review cycles
- **Human gate** — blocks auto-merge for large diffs (`HUMAN_GATE_MAX_DIFF_LINES`) or sensitive paths (`HUMAN_GATE_SENSITIVE_PATHS`)
- **Post-merge verification** — watches CI + K8s rollout, spawns verification sub-agent on dev; on fail auto-creates hotfix Bug task; configurable via `POST_MERGE_VERIFICATION_ENABLED`
- **Environment config** — SQLite key-value store for per-environment connection details; supervisor writes via `env_set`, workers read via `env_get`
- **Event bus** — async pub/sub for real-time streaming to web dashboard
- **Web dashboard** — FastAPI REST + WebSocket, React frontend with xterm.js
- **Supervisor chat** — interactive + autonomous streaming with `bypassPermissions`; `auto_send()` for epic planning
- **Supervisor memory** — SQLite + FTS5 hybrid search (BM25 0.3 / vector 0.7) over markdown files in `data/memory/`; Gemini embeddings; auto-refresh on content change
- **Inter-agent communication** — centralized `AgentMailbox` with interrupt-based message delivery; message types: REQUEST, RESPONSE, NOTIFICATION, ARTIFACT; delivery statuses: DELIVERED, QUEUED, OVERFLOW_DROPPED (MAX_INBOX_SIZE=50); 5 MCP tools per agent; supervisor has read-only access
- **Task dependency management** — auto-defers tasks with unresolved deps (Tracker links + LLM text extraction via Haiku); supervisor can override; fail-open on errors
- **K8s diagnostics** — optional pod log/status inspection; feature-gated via `K8S_LOGS_ENABLED`
- **Persistent stats** — SQLite-backed via EventBus subscriber
- **Auto-compaction** — Haiku summarization when approaching token limit, session recreation
- **Session resumption** — persists `session_id` in SQLite; resumes with `fork_session=True`; fresh fallback with context prompt on failure
- **Dead session recovery** — auto-recreates failed sessions with context preserved; publishes `SESSION_RECREATED` event

## Python Style Guide (based on Google Python Style Guide)

Follows [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) with additions below.

### Imports
**Ordering** (blank-line separated, lexicographic within group):
1. `from __future__` 2. stdlib 3. third-party 4. local

- `import x` for packages/modules only — `from x import y` for submodules
- Never use relative imports — always full package path
- `typing`, `collections.abc`, `typing_extensions` symbols may be imported directly

### Naming
| Type | Convention | Example |
|------|-----------|---------|
| Modules | `lower_with_under` | `agent_runner.py` |
| Classes/Exceptions | `CapWords` (`Error` suffix) | `TrackerApiError` |
| Functions/Methods/Vars | `lower_with_under` | `dispatch_task()` |
| Constants | `ALL_CAPS` | `MAX_COMPACTION_CYCLES` |
| Protected | `_single_underscore` | `_parse_response()` |

- Never `__double_underscore` for class members
- Test methods: `test_<method>_<state>`

### Type Annotations
- Required on all public functions/methods
- Shared type aliases in `constants.py`
- Conditional imports: `if typing.TYPE_CHECKING:` with `from __future__ import annotations`

### Docstrings
- `"""` always. Required for modules, public API, classes, nontrivial private functions
- Sections in order: summary, description, Args, Returns, Raises
- Class docstring: what an instance represents + `Attributes:` section

### Exception Handling
- **Never bare `except:`** or catch `Exception` unless re-raising or at isolation point
- **Never `assert` for runtime logic** — only in tests
- Every module defines structured error types with context fields (status codes, entity IDs)
```python
class TrackerApiError(OrchestratorError):
    def __init__(self, status_code: int, message: str, issue_key: str | None = None):
        self.status_code = status_code
        self.issue_key = issue_key
        super().__init__(f"Tracker API error {status_code}: {message}")
```

### Formatting
- **80 chars** max (except long imports, URLs, string constants)
- 4 spaces indent, no tabs, no backslash continuation, no semicolons
- Trailing commas when closing token is on separate line

### String Formatting
- f-strings for general use
- **Logging: always `%`-style** — `logger.info("Processing %d items for %s", count, uid)`
- Never `+`/`+=` for string accumulation — use `"".join()`

### Structured Logging
- Task-scoped: include `task_key`. PR-scoped: `task_key` + `pr_url`. Session-scoped: `session_id`
- `DEBUG` = routine, `INFO` = business events, `WARNING` = recoverable anomalies, `ERROR` = unrecoverable failures

### Size Limits
- **Module: ~500 lines.** Split into sub-modules if exceeded
- **Class: ~15 instance vars.** Split into collaborators via constructor injection
- **Function: ~40 lines.** Prefer small, focused functions
- No nested function factories with 5+ inner functions — convert to class
- One responsibility per module

### Concurrency Patterns
- **Lock cleanup on error** — `self._locks.pop(key, None)` in `except` block
- **Atomic multi-record persistence** — wrap related writes in single transaction
- **Side effects after commit** — persist first, then publish events/notifications

### HTTP Resilience
- Timeouts on every request: `(connect, read)` tuple
- Retry with exponential backoff + jitter on 429, 500, 502, 503, 504
- Never retry non-idempotent operations without idempotency keys

### Input Validation
All FastAPI endpoints: validate with `Query`, `Path`, `Body` + type constraints.

### Other Conventions
- Never use mutable default arguments — use `None` + `if X is None: X = []`
- Use implicit false: `if not items:` not `if len(items) == 0:`
- Never `staticmethod` — use module-level function
- Always use `with` for files/sockets/connections
- `if __name__ == "__main__": main()` guard

## Code Quality Rules

Derived from recurring PR review issues. Follow strictly.

### No cross-module private imports
Never import `_private` names from other modules. Make it public or create a shared module.

### No function duplication
If two modules need the same utility, define once and import.

### Types belong in the module that owns the concept
Shared types → `constants.py`. Core modules must NEVER import from feature modules.

### Propagate data through all paths
When adding a dataclass field, trace ALL creation sites, callback signatures, and consumers.

### Consistent locking in paired operations
If `approve()` uses a lock, `reject()` must also use a lock.

### Wall-clock vs monotonic time
- `time.time()` — external-facing timestamps (API, events, display)
- `time.monotonic()` — internal cooldowns, rate limiting, elapsed checks

### Comments must match behavior
Verify queue/eviction semantics comments match actual code behavior.

### Never discard async results silently
Every `await session.send()` result must be handled. When draining — merge results:
- **Sticky flags** (`success`) — `base.X or update.X`
- **Latest-wins** (`output`, `pr_url`) — `update.X or base.X`
- **Accumulators** (`cost_usd`) — sum

### Event lifecycle contract
Every TASK_STARTED must end with exactly one of: TASK_COMPLETED, TASK_FAILED, or PR_TRACKED.

| Outcome | Events | Records |
|---------|--------|---------|
| Success with PR | STARTED → PR_TRACKED | task_run + pr_lifecycle |
| Success without PR | STARTED → COMPLETED | task_run |
| Failure | STARTED → FAILED | task_run + error_log |
| PR merged + verified | PR_TRACKED → MERGED → VERIFIED | task_run + pr_lifecycle (verified_at) |

**QR-247:** TASK_COMPLETED from `complete_task_impl` includes `has_pr: False`. Suspicious output → `no_pr_warning: True`.

### Event data contracts
Document required `event.data` fields at `EventType` definition. Verify ALL publishers provide new fields.

### Fail-open vs fail-closed must be explicit
Always add a comment explaining the reasoning when changing between fail-open and fail-closed behavior.

### Self-review before commit
Re-read full diff: no duplicated code, updated helpers/fixtures, parametrized similar tests, no dead mocks.

### Testing Rules

**Mock at boundaries only.** External boundaries: HTTP clients, Claude SDK, filesystem, clock. Use real instances/fakes for EventBus, Storage, etc.

**Never test private methods.** Test the public method that calls them.

**Never assert mock call sequences** unless testing the external boundary itself. Assert observable behavior instead.

**Mock specs mandatory.** Always `MagicMock(spec=ClassName)`.

**Fixture factories in conftest.py.** 3+ lines of setup → use a factory fixture.

**Table-driven tests.** Multiple tests differing only in inputs → `@pytest.mark.parametrize`.

**Lifecycle tests.** Every stateful multi-step flow must have an end-to-end scenario test.

**Time-dependent tests.** Use `freezegun`, never `asyncio.sleep(0)`.

**Test async consumers.** Use `queue.join()` + `task_done()`, not `get_nowait()`.

## Development Process (TDD)

1. **Write a failing test** — define expected behavior first
2. **Make it pass** — minimum code
3. **Refactor** — clean up, keep tests green

## Documentation

After significant changes — update README and CLAUDE.md to keep docs in sync.

## Bug Fix Process (Review Comments)

1. **Write a test** that reproduces the reported bug
2. **Run it** — if it fails, bug confirmed
3. **Fix** — make the test pass
4. **If test passes without changes** — bug report is incorrect; delete the test
5. **Search for similar issues** across the entire codebase; fix all in same commit

## Quality Checks (Taskfile + Docker)

All checks run in Docker containers via [Task](https://taskfile.dev). Same checks in CI.

**Prerequisites:** Docker + [Task](https://taskfile.dev) (`brew install go-task`)

```bash
task quality              # ALL checks (Python + Frontend parallel)
task python:quality       # lint -> format:check -> typecheck -> test
task frontend:quality     # typecheck -> lint -> test -> build
task lint                 # ruff check
task format:check         # ruff format --check
task typecheck            # mypy orchestrator/
task test                 # pytest with 75% coverage
task audit                # pip-audit (non-blocking)
task format               # ruff format (auto-fix)
task lint:fix             # ruff check --fix (auto-fix)
```

CI uses same Taskfile (`.github/workflows/quality.yml`). Edit `Taskfile.yml` to change checks.

## Running

```bash
source .venv/bin/activate && pip install -e ".[dev]"
pytest tests/ -v              # or `task test` for Docker run
python -m orchestrator.main   # run orchestrator
```

## Frontend
```bash
cd frontend && npm install
npm run dev    # dev server with proxy to :8080
npm run build  # production build to frontend/dist/
```

## Docker
```bash
docker compose build && docker compose up -d
# Dashboard at http://localhost:8080
```
