# Supervisor Memory — Long-term Knowledge

## Project Overview

ZvenoAI Coder — async orchestrator that polls Yandex Tracker for tasks tagged `ai-task` and dispatches Claude Agent SDK agents. Supervisor is a meta-agent that reviews worker results and creates improvement tasks.

## Architecture Decisions

### Memory System (2026-02-16)
Replaced LanceDB with SQLite + FTS5 for supervisor memory. Source of truth = markdown files in `data/memory/`. Hybrid search: 70% vector cosine similarity + 30% BM25 (FTS5). Embeddings via Zveno API (google/gemini-embedding-001, 768 dims). Incremental reindex via file content hashing.

### Task Creation
Queue, project ID, board, and component-to-assignee mapping are configured via environment variables (TRACKER_QUEUE, TRACKER_PROJECT_ID, TRACKER_BOARDS, COMPONENT_ASSIGNEE_MAP). Components: Backend, Frontend, UX/UI, DevOps.

### Code Quality
TDD approach: write failing test first, then implement. No cross-module private imports. No function duplication. Types in constants.py. Every PR review comment — test first, then fix.

## Patterns & Anti-patterns

### Good Patterns
- Always search for existing tasks before creating new ones (deduplication)
- Maximum 2 tasks per supervisor session
- Task summary and description in Russian
- Quality over quantity: no task is better than a hack task

### Anti-patterns to Avoid
- Creating vague tasks without clear scope
- Proposing workarounds instead of root-cause fixes
- Creating duplicate tasks for the same issue
