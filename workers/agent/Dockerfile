FROM python:3.12-slim

# Build context: the workspace root (run ``docker build`` with
# ``-f workers/agent/Dockerfile .`` from the repo root). The agent's
# pyproject declares ``treadmill-api`` as a workspace source, so the
# API package must be present at build time for pip to resolve the
# editable install.

# git + gh + nodejs (for Claude Code CLI install via npm) + make so plan
# validation scripts that use `make <target>` work in the worker. gh
# provides the GitHub mode PR creation; git is used by both modes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        gnupg \
        make \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends gh nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI provides the agent's code-author capability. Pinned
# to a specific semver to avoid surprise upgrades silently changing
# argv parsing or output format; the agent passes --model to select
# per-role (Opus / Sonnet / Haiku). Pinned 2026-05-11.
RUN npm install -g @anthropic-ai/claude-code@2.1.138

# uv — needed for author-side validation scripts that use ``uv run pytest``
# against the cloned workspace (per task #121 + the 2026-05-14 learning).
# Without uv on PATH inside the worker, every validation script that uses
# the standard ``cd <pkg> && uv run pytest`` pattern exits 127 and the
# code disposition refuses to push.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

WORKDIR /app

# Copy the API package first because the agent's pyproject declares
# it as a workspace source (treadmill-api = { workspace = true }).
# pip can't resolve the editable workspace install without the source
# tree present at install time.
COPY services/api/pyproject.toml ./services/api/pyproject.toml
COPY services/api/treadmill_api ./services/api/treadmill_api

COPY workers/agent/pyproject.toml ./pyproject.toml
COPY workers/agent/treadmill_agent ./treadmill_agent

# Install the API package first so the agent's pip install sees it as
# already-installed (its ``treadmill-api`` workspace dep is then
# satisfied from PyPI metadata, which finds the local package).
RUN pip install --no-cache-dir ./services/api
RUN pip install --no-cache-dir .

# Default claude OAuth credentials path. The local-adapter mounts the
# host's ~/.claude/.credentials.json here (read-write so Claude Code
# can refresh the OAuth token in place); production deploys mount a
# per-instance secret.
ENV CLAUDE_CREDENTIALS_PATH=/root/.claude/.credentials.json

# Mounted bare repos come from the host's user — git's dubious-ownership
# check rejects them inside the container otherwise. Trust everything
# under the bare-repos volume + workspace path; production workers do
# not clone from host-mounted repos so this scope is local-mode-only.
RUN git config --global --add safe.directory '*'

CMD ["python", "-m", "treadmill_agent"]
