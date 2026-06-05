"""Treadmill API configuration loaded from environment variables.

Per ADR-0011, the API service is event-driven and immutable; configuration
is read from the environment. Locally, the adapter (per ADR-0002) wires
``TREADMILL_DEPLOYMENT_MODE=fully_local`` and points the AWS endpoint at
moto. In dev-local (ADR-0016) ``TREADMILL_DEPLOYMENT_MODE=dev_local`` runs
the API on the laptop against real AWS queues + topics + secrets. In a
future ``fully_remote`` deployment, ECS task-definition env vars set
everything.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentMode(StrEnum):
    """Treadmill deployment topology (ADR-0016).

    Canonical lower_snake string values per ADR-0016's "Canonical spellings"
    table. The enum *member* names are UPPER_SNAKE; the enum *values* are
    lower_snake and match the env-var literal, the CDK context flag, and
    the YAML field value.
    """

    FULLY_LOCAL = "fully_local"
    DEV_LOCAL = "dev_local"
    FULLY_REMOTE = "fully_remote"


class Settings(BaseSettings):
    """Resolved configuration. Read once at process start; pydantic-validated."""

    model_config = SettingsConfigDict(
        env_prefix="TREADMILL_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    service_name: str = Field(default="treadmill-api")
    version: str = Field(default="0.0.0")

    # ── Deployment mode (ADR-0016) ────────────────────────────────────────────
    # Replaces the legacy ``local: bool`` flag. The TREADMILL_ env_prefix on
    # this model auto-maps ``TREADMILL_DEPLOYMENT_MODE`` to this field; no
    # explicit alias needed. Backward-compat with ``TREADMILL_LOCAL`` is
    # handled in the pre-validator below (migration path; remove once all
    # callers set ``TREADMILL_DEPLOYMENT_MODE`` directly).
    deployment_mode: DeploymentMode = Field(default=DeploymentMode.FULLY_LOCAL)

    # ── Database ──────────────────────────────────────────────────────────────
    # Async URL (asyncpg). Example: postgresql+asyncpg://user:pass@host:5432/db
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str | None = Field(default=None, alias="REDIS_URL")

    # ── AWS endpoint (moto in fully_local mode; unset in dev_local / fully_remote)
    aws_endpoint_url: str | None = Field(default=None, alias="AWS_ENDPOINT_URL")
    aws_region: str = Field(default="us-east-1", alias="AWS_DEFAULT_REGION")

    # ── AWS account / per-deployment identity (ADR-0016 dev_local + fully_remote)
    # ``aws_account_id`` is used for the preflight assertion that operator
    # commands target the right account (``sts get-caller-identity`` ==
    # ``aws_account_id``). Unset in fully_local; required in dev_local /
    # fully_remote (validated at the call sites that need it, not here —
    # the field stays Optional so fully_local tests don't have to provide it).
    aws_account_id: str | None = Field(default=None, alias="AWS_ACCOUNT_ID")

    # ── HTTP server ───────────────────────────────────────────────────────────
    # 8088 default rather than 8080 because 8080 is commonly squatted on dev
    # machines. The local adapter sets TREADMILL_PORT explicitly per deploy.
    port: int = Field(default=8088)

    # ── Startup: alembic migrations ───────────────────────────────────────────
    # The CLI entrypoint runs ``alembic upgrade head`` before launching uvicorn
    # so a fresh Postgres comes up schema-ready (Week 4 friction point #1).
    # Set ``TREADMILL_SKIP_MIGRATIONS=true`` to opt out — tests that manage
    # their own schema, and future deployments that run migrations as a
    # separate step, use this. Default behavior runs migrations.
    skip_migrations: bool = Field(default=False)

    # ── Startup: auto-seed starters (ADR-0028 Q28.a) ──────────────────────────
    # After ``alembic upgrade head`` succeeds, the entrypoint calls
    # ``seed_starters_if_empty`` to bulk-INSERT canonical roles +
    # workflows + event_triggers when the DB is empty. Set
    # ``TREADMILL_SKIP_AUTO_SEED=true`` to opt out — test fixtures that
    # seed their own schema state use this so the auto-seed doesn't
    # collide with their setup.
    skip_auto_seed: bool = Field(default=False)

    # ── Logging ───────────────────────────────────────────────────────────────
    # The CLI entrypoint calls ``logging.basicConfig`` at this level so
    # ``treadmill_api.*`` INFO logs surface in container stdout (Week 4
    # friction point #2). Uvicorn's own log_config is untouched — its
    # access/error loggers configure themselves separately.
    log_level: str = Field(default="INFO")

    # ── GitHub webhook secret ─────────────────────────────────────────────────
    # When unset (None or empty), webhook signature verification is skipped —
    # local dev only. Production sets this via the deployment's secrets layer
    # and rejects webhooks with missing/invalid signatures.
    github_webhook_secret: str | None = Field(default=None, alias="GITHUB_WEBHOOK_SECRET")

    # ── GitHub webhook secret name in Secrets Manager (ADR-0017) ──────────────
    # In dev_local / fully_remote the webhook secret lives in Secrets Manager
    # at e.g. ``treadmill-<deployment_id>/github-webhook-secret``. The
    # webhook-inbox poller fetches the value via boto3 at startup and caches
    # it for its lifetime (rotation requires an API restart, per ADR-0017's
    # "operator-visible rotation = better than imperceptible-rotation"
    # trade-off). Unset in fully_local; the in-process HTTP route there uses
    # ``github_webhook_secret`` instead (env-var path).
    github_webhook_secret_name: str | None = Field(
        default=None, alias="GITHUB_WEBHOOK_SECRET_NAME",
    )

    # ── GitHub App webhook secret (App-secret cutover prep) ───────────────────
    # The GitHub App has its own webhook secret, distinct from the legacy
    # ``github_webhook_secret`` configured on the pre-App webhook. During the
    # cutover the verifier accepts a payload signed by either secret; once
    # all traffic is on the App's webhook, the legacy field can be retired.
    github_app_webhook_secret: str | None = Field(
        default=None, alias="GITHUB_APP_WEBHOOK_SECRET",
    )
    github_app_webhook_secret_name: str | None = Field(
        default=None, alias="GITHUB_APP_WEBHOOK_SECRET_NAME",
    )

    # ── GitHub App identity (ADR-0049) ────────────────────────────────────────
    # Treadmill authenticates as a GitHub App: short-lived, per-installation
    # tokens minted from the App id + RS256 private key, replacing the single
    # PAT. ``github_app_id`` is non-secret. The private key (PEM) may be
    # supplied directly via ``GITHUB_APP_PRIVATE_KEY`` (adapter-injected env,
    # mirroring GITHUB_TOKEN) or fetched from Secrets Manager by name in
    # dev_local / fully_remote. When neither the id nor a key is set, the App
    # path is inactive and the existing PAT path remains in force (parallel
    # operation through the migration; see ADR-0049 + the migration plan).
    github_app_id: str | None = Field(default=None, alias="GITHUB_APP_ID")
    github_app_private_key: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY",
    )
    github_app_private_key_secret_name: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_SECRET_NAME",
    )

    # ── SNS topic for runtime events ──────────────────────────────────────────
    # The API publishes typed events here per ADR-0011. When unset, the
    # publisher logs to stderr instead — useful for local dev / test
    # subprocesses that aren't wired to AWS.
    events_topic_arn: str | None = Field(default=None, alias="EVENTS_TOPIC_ARN")

    # ── SQS coordination queue ────────────────────────────────────────────────
    # The API consumer reads step lifecycle events from this queue and
    # advances ``workflow_run_steps.status`` (the single mutable column per
    # ADR-0011). When unset, the consumer is not started — the API still
    # serves HTTP traffic but won't react to events.
    events_queue_url: str | None = Field(default=None, alias="EVENTS_QUEUE_URL")

    # ── SQS work queue ────────────────────────────────────────────────────────
    # The dispatch path sends thin claim messages here so the autoscaler
    # scales workers up. When unset, dispatch publishes the step.ready event
    # to SNS only — workers won't run, but events are still recorded.
    work_queue_url: str | None = Field(default=None, alias="WORK_QUEUE_URL")

    # ── SQS webhook inbox queue (ADR-0017) ────────────────────────────────────
    # The webhook-inbox poller drains envelopes the Lambda webhook receiver
    # enqueued. Set in dev_local + fully_remote modes; unset in fully_local
    # (which uses the in-process HTTP route at /api/v1/webhooks/github).
    webhook_inbox_queue_url: str | None = Field(
        default=None, alias="WEBHOOK_INBOX_QUEUE_URL"
    )

    # ── Claude account routing (ADR-0055) ─────────────────────────────────────
    # JSON map of named Claude accounts → ``{type, secret_name}`` consumed by
    # the ``/api/v1/claude/credentials`` resolver. ``type`` is ``oauth`` (long-
    # lived token from ``claude setup-token``, injected as
    # ``CLAUDE_CODE_OAUTH_TOKEN``) or ``api_key`` (Anthropic Console key,
    # injected as ``ANTHROPIC_API_KEY``). ``secret_name`` is the AWS Secrets
    # Manager id whose ``SecretString`` is the raw token / key value.
    # When unset the resolver returns 503 and workers fall back to the
    # existing ``CLAUDE_CREDENTIALS_PATH`` bind-mount.
    claude_accounts_json: str | None = Field(
        default=None, alias="CLAUDE_ACCOUNTS_JSON",
    )
    # Name of the account that's used when ``RepoConfig.claude_account`` is NULL
    # (or the repo isn't onboarded). Must be a key in ``claude_accounts_json``.
    claude_default_account: str | None = Field(
        default=None, alias="CLAUDE_DEFAULT_ACCOUNT",
    )

    # ── S3 bucket for the context-doc store (ADR-0050 d.4) ────────────────────
    # The context-doc REST API (ADR-0054) writes per-repo context docs to
    # this bucket via ``ContextStore``. When unset, the
    # ``/api/v1/repos/{repo}/docs*`` endpoints return 503 — mirrors the
    # GitHub App "service not configured" pattern in routers/github.py.
    context_docs_bucket: str | None = Field(
        default=None, alias="CONTEXT_DOCS_BUCKET",
    )

    # ── GitHub token for the conflict-detection sweep ─────────────────────────
    # The consumer's pr_merged handler polls GitHub's mergeable API for
    # open PRs in the repo (per Week 3 B.3 / ADR-0013). When unset, the
    # sweep is skipped — local dev / tests / API instances without GitHub
    # credentials still run, just without conflict detection. Production
    # sets this via the deployment's secrets layer.
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")

    # ── Per-repo allow-list for the plan-merge trigger (ADR-0021) ─────────────
    # The merge-to-main plan-doc trigger only fires for repos whose slug
    # appears in this comma-separated allow-list. Empty (the default)
    # means *all repos allowed* — appropriate for v0 where a single
    # deployment serves a single repo. Future task #95 (bootstrap
    # non-Treadmilled repos) replaces this with a per-repo config row.
    plan_merge_repo_allowlist: str = Field(
        default="", alias="TREADMILL_PLAN_MERGE_REPO_ALLOWLIST",
    )

    # ── ADR-0062 Step 4: escalation notification fan-out ─────────────────────
    # ``slack_webhook_url`` — when set, the notification-fanout subscriber
    # POSTs a Slack-formatted JSON body (text + emoji + task-id snippet +
    # reason + MTTR for close events) to this incoming-webhook URL on
    # every ``task.escalated_to_operator`` and ``task.escalation_closed``
    # event. Unset = no Slack hop.
    #
    # ``notification_webhooks`` — comma-separated list of raw-event-JSON
    # webhook targets. Each URL receives a POST with the typed-event
    # record (the same shape ``eventbus._build_record`` produces) on every
    # escalation open / close. Empty (the default) = no generic-webhook
    # fan-out. Per-target POST failures log + continue; one bad URL never
    # blocks the others (ADR-0062 Step 4 invariant).
    slack_webhook_url: str | None = Field(
        default=None, alias="TREADMILL_SLACK_WEBHOOK_URL",
    )
    notification_webhooks: str = Field(
        default="", alias="TREADMILL_NOTIFICATION_WEBHOOKS",
    )

    # ── ADR-0071: Telegram escalation target ────────────────────────────────
    # ``telegram_bot_token`` + ``telegram_chat_id`` — when BOTH are set, the
    # notification-fanout subscriber POSTs every escalation open / close to
    # ``https://api.telegram.org/bot<token>/sendMessage`` with a plain
    # one-line summary (the same content as the Slack body, minus Slack's
    # emoji syntax). Either field unset = no Telegram hop. The bot token is
    # a secret — read from env, never logged. Telegram is a sibling target
    # alongside Slack (ADR-0071 Decision part 2); Slack is retained, not
    # replaced.
    telegram_bot_token: str | None = Field(
        default=None, alias="TREADMILL_TELEGRAM_BOT_TOKEN",
    )
    telegram_chat_id: str | None = Field(
        default=None, alias="TREADMILL_TELEGRAM_CHAT_ID",
    )

    @property
    def notification_webhook_urls(self) -> list[str]:
        """Parsed list of generic-webhook URLs. Empty list = no fan-out."""
        return [
            u.strip()
            for u in self.notification_webhooks.split(",")
            if u.strip()
        ]

    @property
    def plan_merge_allowed_repos(self) -> set[str]:
        """Parsed allow-list. Empty set means "allow all repos"."""
        return {
            r.strip()
            for r in self.plan_merge_repo_allowlist.split(",")
            if r.strip()
        }

    def plan_merge_repo_is_allowed(self, repo: str) -> bool:
        """``True`` iff ``repo`` is allowed to trigger plan-merge dispatch.

        Empty allow-list = all repos allowed (v0 default). Non-empty
        allow-list = strict membership check.
        """
        allowed = self.plan_merge_allowed_repos
        return not allowed or repo in allowed

    # ── Backward-compatibility: TREADMILL_LOCAL → deployment_mode ─────────────
    # Migration path from the binary ``local: bool`` flag. If callers set
    # ``TREADMILL_LOCAL=true`` (and ``TREADMILL_DEPLOYMENT_MODE`` is not
    # set), map to FULLY_LOCAL. ``TREADMILL_LOCAL=false`` maps to
    # FULLY_REMOTE (the historical meaning — "not local" — even though
    # FULLY_LOCAL is now the default).
    #
    # Implementation: a transitional ``legacy_local`` field captures the env
    # var via its alias so pydantic-settings's env source picks it up; a
    # ``model_validator(mode="before")`` collapses it into ``deployment_mode``
    # before validation runs. Remove the field and the validator after all
    # callers migrate to ``TREADMILL_DEPLOYMENT_MODE``.
    legacy_local: bool | None = Field(default=None, alias="TREADMILL_LOCAL", exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _legacy_treadmill_local_to_deployment_mode(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # If the caller explicitly set deployment_mode (via field name, alias,
        # or auto-prefixed env), don't override it.
        explicit_keys = ("deployment_mode", "DEPLOYMENT_MODE", "TREADMILL_DEPLOYMENT_MODE")
        if any(k in data for k in explicit_keys):
            data.pop("legacy_local", None)
            data.pop("TREADMILL_LOCAL", None)
            data.pop("local", None)
            return data
        # Look for the legacy TREADMILL_LOCAL key. Pydantic Settings's env
        # source surfaces it under either the field name (``legacy_local``)
        # or the alias (``TREADMILL_LOCAL``); explicit-kwarg construction
        # (``Settings(TREADMILL_LOCAL=...)``) also lands here under the alias.
        # ``local`` is a fallback for completeness (the old field name).
        legacy: Any = None
        for key in ("TREADMILL_LOCAL", "legacy_local", "local"):
            if key in data:
                legacy = data.pop(key)
                break
        if legacy is None:
            return data
        if isinstance(legacy, str):
            truthy = legacy.strip().lower() in {"1", "true", "yes", "on"}
        else:
            truthy = bool(legacy)
        data["deployment_mode"] = (
            DeploymentMode.FULLY_LOCAL if truthy else DeploymentMode.FULLY_REMOTE
        )
        return data

    # ── Convenience accessor ──────────────────────────────────────────────────
    @property
    def is_fully_local(self) -> bool:
        """True when the deployment is the moto-backed fully-local mode.

        Used by features (e.g., the ``--dev`` plan-submission fast-path) that
        gate on "running against the moto substrate." Both ``dev_local`` and
        ``fully_remote`` talk to real AWS; only ``fully_local`` does not.
        """
        return self.deployment_mode == DeploymentMode.FULLY_LOCAL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor; safe for import-time use."""
    return Settings()


def reset_settings_cache() -> None:
    """Reset the cached settings (test-only)."""
    get_settings.cache_clear()
