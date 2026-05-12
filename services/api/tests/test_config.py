"""Unit tests for treadmill_api.config — Settings + DeploymentMode.

Covers the Phase A.1 surface (ADR-0016):

* ``DeploymentMode`` enum values match the canonical lower_snake spellings.
* ``TREADMILL_DEPLOYMENT_MODE`` env-var maps onto ``Settings.deployment_mode``
  for every enum value.
* The backward-compat shim collapses ``TREADMILL_LOCAL`` (true/false) onto
  the corresponding ``DeploymentMode`` value.
* The two new optional fields ``aws_account_id`` and
  ``webhook_inbox_queue_url`` are settable via env + default to ``None``.
* The ``is_fully_local`` convenience property tracks ``deployment_mode``.

These tests construct ``Settings()`` directly (no module-level caching);
each case fully controls its env via ``monkeypatch``.
"""

from __future__ import annotations

import pytest

from treadmill_api.config import DeploymentMode, Settings


# ── DeploymentMode enum shape ─────────────────────────────────────────────────


def test_deployment_mode_values_are_canonical_lower_snake():
    """ADR-0016 canonical-spellings table: enum *values* are lower_snake."""
    assert DeploymentMode.FULLY_LOCAL.value == "fully_local"
    assert DeploymentMode.DEV_LOCAL.value == "dev_local"
    assert DeploymentMode.FULLY_REMOTE.value == "fully_remote"


def test_deployment_mode_is_str_enum():
    """StrEnum membership lets the enum compare equal to its string value —
    convenient for code that reads YAML strings and feeds them into Settings."""
    assert DeploymentMode.FULLY_LOCAL == "fully_local"
    assert DeploymentMode("dev_local") is DeploymentMode.DEV_LOCAL


# ── Defaults + explicit kwargs ────────────────────────────────────────────────


def test_default_deployment_mode_is_fully_local(monkeypatch: pytest.MonkeyPatch):
    """Without any env var, Settings defaults to FULLY_LOCAL — matches the
    historical default of ``local=True`` for laptop dev environments."""
    monkeypatch.delenv("TREADMILL_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("TREADMILL_LOCAL", raising=False)
    settings = Settings()
    assert settings.deployment_mode is DeploymentMode.FULLY_LOCAL
    assert settings.is_fully_local is True


def test_explicit_kwarg_deployment_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TREADMILL_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("TREADMILL_LOCAL", raising=False)
    settings = Settings(deployment_mode=DeploymentMode.DEV_LOCAL)
    assert settings.deployment_mode is DeploymentMode.DEV_LOCAL
    assert settings.is_fully_local is False


# ── TREADMILL_DEPLOYMENT_MODE env-var parsing (one case per enum value) ───────


@pytest.mark.parametrize(
    "env_value, expected",
    [
        ("fully_local", DeploymentMode.FULLY_LOCAL),
        ("dev_local", DeploymentMode.DEV_LOCAL),
        ("fully_remote", DeploymentMode.FULLY_REMOTE),
    ],
)
def test_env_var_maps_to_each_deployment_mode(
    env_value: str,
    expected: DeploymentMode,
    monkeypatch: pytest.MonkeyPatch,
):
    """``TREADMILL_DEPLOYMENT_MODE=<value>`` resolves to the matching enum
    member via pydantic-settings's ``env_prefix=TREADMILL_`` auto-mapping."""
    monkeypatch.delenv("TREADMILL_LOCAL", raising=False)
    monkeypatch.setenv("TREADMILL_DEPLOYMENT_MODE", env_value)
    settings = Settings()
    assert settings.deployment_mode is expected


def test_is_fully_local_tracks_deployment_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TREADMILL_LOCAL", raising=False)
    for env_value, expected_is_local in (
        ("fully_local", True),
        ("dev_local", False),
        ("fully_remote", False),
    ):
        monkeypatch.setenv("TREADMILL_DEPLOYMENT_MODE", env_value)
        settings = Settings()
        assert settings.is_fully_local is expected_is_local, env_value


# ── Backward-compat: TREADMILL_LOCAL → deployment_mode ────────────────────────


def test_legacy_treadmill_local_true_maps_to_fully_local(
    monkeypatch: pytest.MonkeyPatch,
):
    """The compat shim: ``TREADMILL_LOCAL=true`` (the historical "local mode"
    spelling) maps to ``FULLY_LOCAL`` to preserve behavior for callers that
    haven't migrated yet."""
    monkeypatch.delenv("TREADMILL_DEPLOYMENT_MODE", raising=False)
    monkeypatch.setenv("TREADMILL_LOCAL", "true")
    settings = Settings()
    assert settings.deployment_mode is DeploymentMode.FULLY_LOCAL


def test_legacy_treadmill_local_false_maps_to_fully_remote(
    monkeypatch: pytest.MonkeyPatch,
):
    """The compat shim: ``TREADMILL_LOCAL=false`` historically meant "not
    local" (i.e., real AWS production). Map to ``FULLY_REMOTE`` — the only
    real-AWS mode that matches the binary's old semantics. Operators who
    want ``dev_local`` set ``TREADMILL_DEPLOYMENT_MODE`` explicitly."""
    monkeypatch.delenv("TREADMILL_DEPLOYMENT_MODE", raising=False)
    monkeypatch.setenv("TREADMILL_LOCAL", "false")
    settings = Settings()
    assert settings.deployment_mode is DeploymentMode.FULLY_REMOTE


def test_explicit_deployment_mode_overrides_legacy_local(
    monkeypatch: pytest.MonkeyPatch,
):
    """When both env vars are set, ``TREADMILL_DEPLOYMENT_MODE`` wins.
    Guards against migration surprises: operators who set the new var get
    the new behavior even if their shell still exports the old one."""
    monkeypatch.setenv("TREADMILL_LOCAL", "true")
    monkeypatch.setenv("TREADMILL_DEPLOYMENT_MODE", "dev_local")
    settings = Settings()
    assert settings.deployment_mode is DeploymentMode.DEV_LOCAL


def test_legacy_kwarg_treadmill_local(monkeypatch: pytest.MonkeyPatch):
    """The compat path also fires for explicit-kwarg construction —
    important because the test suite previously passed
    ``Settings(TREADMILL_LOCAL=...)`` directly."""
    monkeypatch.delenv("TREADMILL_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("TREADMILL_LOCAL", raising=False)
    assert (
        Settings(TREADMILL_LOCAL=True).deployment_mode is DeploymentMode.FULLY_LOCAL
    )
    assert (
        Settings(TREADMILL_LOCAL=False).deployment_mode is DeploymentMode.FULLY_REMOTE
    )


# ── New fields: aws_account_id + webhook_inbox_queue_url ──────────────────────


def test_aws_account_id_defaults_to_none(monkeypatch: pytest.MonkeyPatch):
    """``aws_account_id`` is optional — unset in fully_local mode where it
    has no meaning. Required at the call sites that use it (the operator
    preflight assertion), not at Settings construction."""
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    settings = Settings()
    assert settings.aws_account_id is None


def test_aws_account_id_from_env(monkeypatch: pytest.MonkeyPatch):
    """ADR-0016 aliases ``aws_account_id`` to the bare ``AWS_ACCOUNT_ID``
    env var (no ``TREADMILL_`` prefix) to align with AWS conventions and
    with how operators name CloudFormation outputs."""
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    settings = Settings()
    assert settings.aws_account_id == "123456789012"


def test_webhook_inbox_queue_url_defaults_to_none(monkeypatch: pytest.MonkeyPatch):
    """``webhook_inbox_queue_url`` is optional — unset in fully_local mode
    (which uses the in-process HTTP webhook route)."""
    monkeypatch.delenv("WEBHOOK_INBOX_QUEUE_URL", raising=False)
    settings = Settings()
    assert settings.webhook_inbox_queue_url is None


def test_webhook_inbox_queue_url_from_env(monkeypatch: pytest.MonkeyPatch):
    """ADR-0017 aliases ``webhook_inbox_queue_url`` to the bare
    ``WEBHOOK_INBOX_QUEUE_URL`` env var so it can be injected verbatim
    from the per-deployment YAML."""
    url = "https://sqs.us-east-1.amazonaws.com/111111111111/treadmill-personal-webhook-inbox"
    monkeypatch.setenv("WEBHOOK_INBOX_QUEUE_URL", url)
    settings = Settings()
    assert settings.webhook_inbox_queue_url == url


def test_new_fields_via_explicit_kwargs(monkeypatch: pytest.MonkeyPatch):
    """Both fields settable via constructor kwargs — used by tests that
    bypass the environment."""
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("WEBHOOK_INBOX_QUEUE_URL", raising=False)
    settings = Settings(
        aws_account_id="111111111111",
        webhook_inbox_queue_url="https://sqs.us-east-1.amazonaws.com/111/q",
    )
    assert settings.aws_account_id == "111111111111"
    assert (
        settings.webhook_inbox_queue_url
        == "https://sqs.us-east-1.amazonaws.com/111/q"
    )


# ── Week 4 fields: skip_migrations + log_level ───────────────────────────────


def test_skip_migrations_defaults_to_false(monkeypatch: pytest.MonkeyPatch):
    """Default behavior: CLI entrypoint runs alembic upgrade head. The
    flag exists for tests + future deployments that run migrations as a
    separate step."""
    monkeypatch.delenv("TREADMILL_SKIP_MIGRATIONS", raising=False)
    settings = Settings()
    assert settings.skip_migrations is False


def test_skip_migrations_from_env(monkeypatch: pytest.MonkeyPatch):
    """``TREADMILL_SKIP_MIGRATIONS=true`` opts out of the CLI's startup
    migration pass. pydantic-settings parses the standard truthy strings."""
    monkeypatch.setenv("TREADMILL_SKIP_MIGRATIONS", "true")
    settings = Settings()
    assert settings.skip_migrations is True


def test_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch):
    """Default INFO matches Week 4 friction-point #2's fix: ``treadmill_api.*``
    INFO logs need to surface in container stdout."""
    monkeypatch.delenv("TREADMILL_LOG_LEVEL", raising=False)
    settings = Settings()
    assert settings.log_level == "INFO"


def test_log_level_from_env(monkeypatch: pytest.MonkeyPatch):
    """``TREADMILL_LOG_LEVEL`` feeds straight into ``logging.basicConfig``
    via ``getattr(logging, settings.log_level.upper())``; the raw string is
    preserved here and the upper-casing happens at the call site."""
    monkeypatch.setenv("TREADMILL_LOG_LEVEL", "DEBUG")
    settings = Settings()
    assert settings.log_level == "DEBUG"


def test_log_level_lowercase_from_env_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
):
    """The CLI upper-cases at use; Settings does not — operator-facing
    case insensitivity is the caller's job."""
    monkeypatch.setenv("TREADMILL_LOG_LEVEL", "debug")
    settings = Settings()
    assert settings.log_level == "debug"
