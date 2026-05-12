"""Worker config tests.

The startup env parsing has to fail loudly on a typo — a silent
boolean coercion (e.g. ``bool("False") -> True``) caused the original
loop bug we're closing here. We exercise every accepted value, the
default, and a representative malformed case.
"""

from __future__ import annotations

import pytest

from treadmill_agent import config
from treadmill_agent.config import _parse_bool


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "  true  "])
def test_parse_bool_accepts_true(raw: str) -> None:
    assert _parse_bool(raw, default=False, var_name="X") is True


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", "  false  "])
def test_parse_bool_accepts_false(raw: str) -> None:
    assert _parse_bool(raw, default=True, var_name="X") is False


def test_parse_bool_accepts_one_and_zero() -> None:
    assert _parse_bool("1", default=False, var_name="X") is True
    assert _parse_bool("0", default=True, var_name="X") is False


def test_parse_bool_accepts_yes_and_no() -> None:
    assert _parse_bool("yes", default=False, var_name="X") is True
    assert _parse_bool("Yes", default=False, var_name="X") is True
    assert _parse_bool("no", default=True, var_name="X") is False
    assert _parse_bool("NO", default=True, var_name="X") is False


def test_parse_bool_returns_default_when_unset() -> None:
    assert _parse_bool(None, default=True, var_name="X") is True
    assert _parse_bool(None, default=False, var_name="X") is False


@pytest.mark.parametrize("raw", ["maybe", "TRUEISH", "yes please", "", " "])
def test_parse_bool_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid boolean for X"):
        _parse_bool(raw, default=True, var_name="X")


# ── load() integration ────────────────────────────────────────────────────────


def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the env vars ``config.load()`` requires before each test."""
    monkeypatch.setenv("WORK_QUEUE_URL", "http://sqs/q")
    # Strip anything that might be in the test process's env so we
    # always start from a clean slate.
    monkeypatch.delenv("EXIT_AFTER_STEP", raising=False)


def test_load_defaults_exit_after_step_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _required_env(monkeypatch)
    settings = config.load()
    assert settings.exit_after_step is True


def test_load_honors_explicit_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _required_env(monkeypatch)
    monkeypatch.setenv("EXIT_AFTER_STEP", "false")
    settings = config.load()
    assert settings.exit_after_step is False


def test_load_honors_explicit_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _required_env(monkeypatch)
    # The CDK env block passes the literal "true" string — confirm that
    # parses the way Agent 3's infra change expects.
    monkeypatch.setenv("EXIT_AFTER_STEP", "true")
    settings = config.load()
    assert settings.exit_after_step is True


def test_load_raises_on_malformed_exit_after_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _required_env(monkeypatch)
    monkeypatch.setenv("EXIT_AFTER_STEP", "perhaps")
    with pytest.raises(ValueError, match="EXIT_AFTER_STEP"):
        config.load()


# ── D.3 github-mode secret settings ──────────────────────────────────────────


def test_load_github_pat_secret_name_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fully-local mode never sets ``GITHUB_PAT_SECRET_NAME``; the
    setting must default to ``None`` so the worker startup short-circuits
    the github-auth branch."""
    _required_env(monkeypatch)
    monkeypatch.delenv("GITHUB_PAT_SECRET_NAME", raising=False)
    settings = config.load()
    assert settings.github_pat_secret_name is None


def test_load_reads_github_pat_secret_name_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dev-local / fully-remote workers set ``GITHUB_PAT_SECRET_NAME``
    to the secret holding the PAT (the YAML key in ADR-0016 is
    ``secrets.github_pat_secret_name`` — same canonical name, different
    surface). Names — not ARNs — per the ADR; Secrets Manager resolves
    name-or-ARN automatically."""
    _required_env(monkeypatch)
    monkeypatch.setenv(
        "GITHUB_PAT_SECRET_NAME", "treadmill-personal/github-pat",
    )
    settings = config.load()
    assert settings.github_pat_secret_name == "treadmill-personal/github-pat"
