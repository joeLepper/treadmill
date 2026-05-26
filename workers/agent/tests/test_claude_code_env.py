"""Unit tests for ``claude_code.build_claude_env`` (ADR-0055).

These pin the env-construction rules so a future regression in the worker's
credential routing — e.g. a forgotten ``pop`` of an inherited auth env, a
typo in the env-var name Claude Code looks up, or the wrong selection
between OAuth and API-key — trips immediately.
"""

from __future__ import annotations

import pytest

from treadmill_agent.claude_code import build_claude_env
from treadmill_agent.startup_auth import ClaudeCreds


def test_none_creds_returns_parent_env_unchanged():
    parent = {"PATH": "/usr/bin", "CLAUDE_CREDENTIALS_PATH": "/x/.creds"}
    env = build_claude_env(parent, None)
    assert env == parent
    # And it's a *copy* — the caller can mutate ``env`` without touching parent.
    env["X"] = "Y"
    assert "X" not in parent


def test_oauth_sets_only_oauth_token_and_clears_inherited_auth():
    parent = {
        "PATH": "/usr/bin",
        # Every credential env Claude Code recognises must be cleared.
        "CLAUDE_CREDENTIALS_PATH": "/x/.creds",
        "ANTHROPIC_API_KEY": "stale-key",
        "ANTHROPIC_AUTH_TOKEN": "stale-bearer",
    }
    env = build_claude_env(
        parent, ClaudeCreds(account="a", type="oauth", token="tkn-oauth"),
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tkn-oauth"
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CREDENTIALS_PATH" not in env
    # Non-auth env passes through untouched.
    assert env["PATH"] == "/usr/bin"


def test_api_key_sets_only_anthropic_api_key_and_clears_inherited_auth():
    parent = {
        "PATH": "/usr/bin",
        "CLAUDE_CREDENTIALS_PATH": "/x/.creds",
        "CLAUDE_CODE_OAUTH_TOKEN": "stale-oauth",
        "ANTHROPIC_AUTH_TOKEN": "stale-bearer",
    }
    env = build_claude_env(
        parent, ClaudeCreds(account="b", type="api_key", token="sk-ant-key"),
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-key"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CREDENTIALS_PATH" not in env


def test_unknown_type_raises():
    parent = {"PATH": "/usr/bin"}
    bogus = ClaudeCreds.__new__(ClaudeCreds)
    object.__setattr__(bogus, "account", "x")
    object.__setattr__(bogus, "type", "bogus")  # bypass Literal — simulate drift
    object.__setattr__(bogus, "token", "t")
    with pytest.raises(ValueError, match="unknown claude_account type"):
        build_claude_env(parent, bogus)
