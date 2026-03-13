# Stage 1: Build frontend
FROM node:22-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python app
FROM python:3.12-slim

# Install git, make, Docker CLI, Go, and Node.js (for Claude Code CLI used by SDK)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    make \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI (for make gen, docker compose in agent worktrees)
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
    > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Install Go (for make gen-swagger, go test in agent worktrees)
RUN curl -fsSL https://go.dev/dl/go1.24.4.linux-$(dpkg --print-architecture).tar.gz \
    | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"

# Install Node.js 22.x
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (required by claude-agent-sdk)
RUN npm install -g @anthropic-ai/claude-code

# Install agent-browser for browser automation in agent tasks
# AGENT_BROWSER_ARGS passes --no-sandbox to Chromium (required for root in container)
ENV AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage
RUN npm install -g agent-browser \
    && agent-browser install --with-deps \
    && rm -rf /var/lib/apt/lists/*

# Install Task (go-task) for quality checks
RUN sh -c "$(curl -fsSL https://taskfile.dev/install.sh)" -- -d -b /usr/local/bin

# Install gh CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy everything needed for install
COPY pyproject.toml .
COPY orchestrator/ orchestrator/

# Install Python dependencies
RUN pip install --no-cache-dir ".[memory,k8s]"

# Copy runtime files
COPY prompts/ prompts/

# Copy built frontend
COPY --from=frontend-build /frontend/dist frontend/dist/

# Workspace for cloned repos and worktrees
RUN mkdir -p /workspace /workspace/worktrees

# Data directory for SQLite stats
RUN mkdir -p /app/data

# Configure git at runtime via entrypoint
COPY <<'EOF' /usr/local/bin/docker-entrypoint.sh
#!/bin/bash
set -e
if [ -n "$GITHUB_TOKEN" ]; then
    git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
    echo "${GITHUB_TOKEN}" | gh auth login --with-token 2>/dev/null || true
fi
git config --global user.email "${GIT_AUTHOR_EMAIL:-noreply@zveno.ai}"
git config --global user.name "${GIT_AUTHOR_NAME:-ZvenoAI Coder}"
exec "$@"
EOF
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "orchestrator.main"]
