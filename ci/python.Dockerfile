FROM python:3.12-slim

WORKDIR /app

# Install deps only — source code is mounted at runtime via volume
COPY pyproject.toml .
RUN mkdir -p orchestrator && touch orchestrator/__init__.py \
    && pip install --no-cache-dir -e ".[dev]" pip-audit \
    && rm orchestrator/__init__.py
