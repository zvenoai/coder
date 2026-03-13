# Contributing to ZvenoAI Coder

Thank you for your interest in contributing! This document covers the process for contributing to this project.

## Getting Started

1. Fork the repository
2. Clone your fork and create a branch:
   ```bash
   git clone https://github.com/<your-username>/coder.git
   cd coder
   git checkout -b my-feature
   ```
3. Set up the development environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

## Development Process

This project follows **Test-Driven Development (TDD)**:

1. **Write a failing test** that defines expected behavior
2. **Make it pass** with the minimum code needed
3. **Refactor** while keeping tests green

This applies to new features, bug fixes, and refactors alike.

## Quality Checks

All checks run in Docker containers via [Task](https://taskfile.dev):

```bash
# Run ALL checks (Python + Frontend)
task quality

# Individual checks
task lint              # ruff check
task format:check      # ruff format --check
task typecheck         # mypy
task test              # pytest (75% coverage threshold)
```

**All checks must pass before submitting a PR.** The same checks run in CI.

## Code Style

See `CLAUDE.md` for the full style guide. Key points:

- Follow the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
- 80 character line limit
- Type annotations on all public functions
- `%`-style for logging, f-strings elsewhere
- No mutable default arguments

## Submitting Changes

1. Run `task quality` and ensure all checks pass
2. Commit with a clear message describing **why**, not just what
3. Push to your fork and open a Pull Request
4. Fill in the PR description with a summary and test plan

## Bug Reports

When fixing a bug reported in a review comment:

1. Write a test that reproduces the bug
2. Verify the test fails
3. Fix the bug and verify the test passes
4. Search the codebase for similar patterns and fix them too

## Frontend

```bash
cd frontend
npm install
npm run dev    # dev server
npm run build  # production build
```

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
