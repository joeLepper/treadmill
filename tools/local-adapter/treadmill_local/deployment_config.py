"""Deployment-config helpers for ``treadmill-local init``.

Reads CloudFormation outputs from a deployed ``TreadmillCloudLite`` stack
and writes the per-deployment YAML config file at
``~/.treadmill/<deployment_id>.yaml`` per the schema in ADR-0016
§"Per-deployment config at ``~/.treadmill/<deployment_id>.yaml``".

The init command's purpose: bridge the gap between "CDK deployed
something to AWS" and "the local-adapter knows what got deployed." It's
idempotent (re-running overwrites the YAML from current stack state) so
it doubles as the operator's regenerate-from-stack lever after a
``cdk deploy`` that changes ARNs.

CDK appends an 8-char hash to ``CfnOutput`` logical ids (e.g.
``WebhookReceiverWebhookApiUrl51C59AB0``), so the matching against
output keys here uses a **suffix match on the documented contract name**
(``WebhookApiUrl``, ``EventsTopicArn``, etc.) rather than an exact-match
lookup. This is the right shape because the operator contract is the
named portion of the output id; the hash is a CDK implementation detail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3
import yaml


# ── Contract: the set of CFN output suffixes the init command requires ────────
#
# Each (yaml_path, output_suffix) entry pairs:
#   * the YAML schema field this output populates (dotted path inside the
#     emitted document)
#   * the named portion of the CFN output's logical id, matched by suffix
#     so CDK's 8-char hash doesn't break us
#
# The order here matches the order keys appear in ADR-0016's documented
# YAML schema so a reviewer can scan the contract top-to-bottom.

_AWS_OUTPUTS: tuple[tuple[str, str], ...] = (
    ("events_topic_arn", "EventsTopicArn"),
    ("events_queue_url", "EventsQueueUrl"),
    ("work_queue_url", "WorkQueueUrl"),
    ("webhook_inbox_queue_url", "WebhookInboxQueueUrl"),
    ("webhook_inbox_dlq_url", "WebhookInboxDlqUrl"),
    ("webhook_api_url", "WebhookApiUrl"),
    ("deploy_events_queue_url", "DeployEventsQueueUrl"),
    ("deploy_events_dlq_url", "DeployEventsDlqUrl"),
)
"""``aws.*`` block — resource ARNs/URLs from messaging + webhook receiver."""

_SECRETS_OUTPUTS: tuple[tuple[str, str], ...] = (
    ("github_webhook_secret_name", "GithubWebhookSecretName"),
    ("github_pat_secret_name", "GithubPatSecretName"),
    ("worker_aws_credentials_secret_name", "WorkerAwsCredentialsSecretName"),
    ("api_aws_credentials_secret_name", "ApiAwsCredentialsSecretName"),
)
"""``secrets.*`` block — Secrets Manager names from the secrets construct."""

_OBS_OUTPUTS: tuple[tuple[str, str], ...] = (
    ("observability_collector_endpoint", "ObservabilityCollectorEndpoint"),
    ("observability_grafana_host", "ObservabilityGrafanaHost"),
    ("observability_ec2_id", "ObservabilityEc2InstanceId"),
    # ARN → name conversion happens in ``build_deployment_config`` via
    # ``_extract_secret_name_from_arn``.
    ("observability_grafana_admin_secret_name", "ObservabilityGrafanaAdminSecretArn"),
)
"""``aws.*`` block — optional observability outputs from ``TreadmillObservabilityStack``.

Present only when the operator deployed the observability stack with
``--context include_observability=true``. All four keys are omitted from
the YAML when the stack was not deployed (graceful no-op).
"""


# ── Dev-local observability defaults ──────────────────────────────────────────
#
# ADR-0043 commits dev_local to running the observability stack as a local
# docker compose unit (rather than CFN-managed EC2 like fully_remote). The
# OTel Collector container is named ``treadmill-otel-collector`` and joins
# the ``treadmill-local`` docker network (per ``docker-compose.local.yml``).
# Worker + API containers run on the same network, so they resolve the
# collector by container DNS — NOT by host loopback (``127.0.0.1``), which
# inside a container is the container itself, not the host.
#
# These defaults are written into the dev_local YAML at ``init`` time when
# the matching ``TreadmillObservabilityStack`` CFN outputs are absent (the
# normal dev_local case — that stack is not deployed). The operator-facing
# host-side URL (``http://localhost:4318`` for OTLP,
# ``http://localhost:3001`` for Grafana) is still served by the compose
# port mapping; that's a separate concern from what we inject into the
# container env.
#
# ``observability_grafana_host`` + ``observability_grafana_port`` are the
# single source of truth for the operator-facing Grafana URL. The port
# defaults to 3001 (not 3000) to side-step the common collision on a
# laptop already running a Grafana / dashboard / Next.js dev server on
# 3000 (observed 2026-05-19 against ``bunkhouse-dashboard``). Compose
# substitutes the host-side mapping from this value via
# ``GRAFANA_HOST_PORT`` (see ``LocalRuntime._start_observability_dev_local``);
# ``treadmill observe`` and the obs-status checks read the same field so
# the URL the operator browses matches the port compose actually bound.

_DEV_LOCAL_OBSERVABILITY_DEFAULTS: dict[str, Any] = {
    "observability_collector_endpoint": "http://treadmill-otel-collector:4318",
    "observability_grafana_host": "127.0.0.1",
    "observability_grafana_port": 3001,
}


# ── Local-side defaults (compute lives on the laptop) ────────────────────────
#
# ADR-0016 commits dev-local to running Postgres + Redis + API as
# containers on the laptop. These URLs are the canonical host-side
# spellings; the local-adapter's ``up`` command boots the containers
# such that these URLs resolve. They are NOT discovered from CFN — they
# are local-runtime constants, included in the YAML so the API + worker
# containers can read one config file for both AWS-side and local-side
# wiring.

_LOCAL_DEFAULTS: dict[str, str] = {
    "database_url": "postgresql://treadmill:treadmill@localhost:5432/treadmill",
    "redis_url": "redis://localhost:6379/0",
    # Host-side dev-local API URL. Port 8088 matches the runtime's
    # ``DEV_LOCAL_API_HOST_PORT`` (the container listens on 8088 and the
    # host publishes it 1:1, see ``_build_api_service_spec``). The
    # deploy-watcher reads this to compose its post-deploy health check
    # URL, so the value must track the actual port the API binds — a
    # stale ``:8000`` here makes the watcher's health probe time out
    # against the wrong port and report the deploy as failed.
    "api_url": "http://localhost:8088",
}


# ── Autoscaler defaults (ADR-0018) ───────────────────────────────────────────
#
# When the YAML's ``autoscaler:`` block is absent, ``load_deployment_yaml``
# fills these defaults so callers can read ``cfg["autoscaler"]`` without a
# nullable check. Values per ADR-0018 §"Config source: deployment YAML":
# ``min=0`` is "no idle workers" (scale to zero when queue is empty),
# ``max=1`` is "one worker at a time" (safe default; operators bump
# explicitly for parallelism), ``tick_seconds=5`` trades a little extra
# latency vs fully-local's 2s for lower real-AWS SQS API call volume.

_AUTOSCALER_DEFAULTS: dict[str, int] = {
    "min": 0,
    "max": 1,
    "tick_seconds": 5,
}


# ── Secrets Manager ARN → name ───────────────────────────────────────────────


def _extract_secret_name_from_arn(arn: str) -> str:
    """Parse a Secrets Manager ARN and return the secret name.

    ARN format: ``arn:aws:secretsmanager:REGION:ACCOUNT:secret:NAME-SUFFIX``
    where ``SUFFIX`` is a 6-character alphanumeric string appended by
    Secrets Manager (e.g., ``treadmill-personal-grafana-admin-password-AbCdEf``).

    Strips the ``-SUFFIX`` (7 chars: hyphen + 6 alphanum) when it matches
    the pattern. If the tail doesn't look like a SM suffix, returns the raw
    resource segment so callers are never silently truncated.
    """
    # Last colon-delimited segment is the resource: "NAME-SUFFIX"
    resource = arn.rsplit(":", 1)[-1]
    if len(resource) > 7:
        suffix = resource[-7:]
        if suffix[0] == "-" and suffix[1:].isalnum():
            return resource[:-7]
    return resource


# ── boto3 + CloudFormation ────────────────────────────────────────────────────


def read_stack_outputs(
    stack_name: str,
    *,
    profile: str | None,
    region: str | None,
) -> dict[str, str]:
    """Return ``{OutputKey: OutputValue}`` for the named CloudFormation stack.

    Uses ``boto3.Session(profile_name=...)`` so the operator's AWS profile
    (typically ``treadmill-<deployment_id>``) resolves the credentials.

    Raises:
        ValueError: if the stack does not exist (ClientError
            ``ValidationError`` is unwrapped into a clear message naming
            the missing stack).
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    cfn = session.client("cloudformation")
    try:
        response = cfn.describe_stacks(StackName=stack_name)
    except cfn.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        if "does not exist" in message or code == "ValidationError":
            raise ValueError(
                f"CloudFormation stack {stack_name!r} not found in "
                f"region {region!r} for profile {profile!r}: {message}"
            ) from exc
        raise

    stacks = response.get("Stacks", [])
    if not stacks:
        raise ValueError(
            f"CloudFormation stack {stack_name!r} returned no stack record"
        )
    outputs = stacks[0].get("Outputs", []) or []
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


# ── Building the YAML-shape dict ──────────────────────────────────────────────


def _find_output(outputs: dict[str, str], suffix: str) -> str:
    """Return the value of the output whose key endswith *suffix*.

    Suffix matching tolerates CDK's 8-char hash appended to the logical
    id (e.g. ``WebhookReceiverWebhookApiUrl51C59AB0`` ends with
    ``WebhookApiUrl`` only if we use a wider match — see below).

    The match is "key contains the suffix as a sub-token" via
    ``endswith`` after stripping a trailing hex hash. CDK's hash is
    8 hex chars, but we can't always strip exactly 8 because the user
    might rename the construct logical id. So we use the more robust
    rule: a match is an output key that either *ends with* the suffix
    OR ends with the suffix followed by 1–12 alphanumerics (the hash).

    Raises:
        KeyError: if no output key matches the suffix.
    """
    # Direct endswith first — covers operator-renamed outputs.
    direct = [k for k in outputs if k.endswith(suffix)]
    if direct:
        return outputs[direct[0]]

    # Hash-tolerant: the suffix appears followed by a CDK hash.
    # CDK hashes are 8 chars but we tolerate 1-12 to be defensive against
    # any future CDK change to the hash length.
    for k in outputs:
        idx = k.rfind(suffix)
        if idx < 0:
            continue
        tail = k[idx + len(suffix):]
        if tail and all(c.isalnum() for c in tail) and len(tail) <= 12:
            return outputs[k]

    raise KeyError(
        f"CloudFormation output ending in {suffix!r} not found; "
        f"available keys: {sorted(outputs.keys())}"
    )


def build_deployment_config(
    deployment_id: str,
    *,
    aws_profile: str,
    aws_region: str,
    aws_account_id: str,
    outputs: dict[str, str],
) -> dict[str, Any]:
    """Produce the YAML-shape dict that ``write_deployment_yaml`` serializes.

    The shape follows ADR-0016 §"Per-deployment config at
    ``~/.treadmill/<deployment_id>.yaml``" verbatim. Top-level keys:

    - ``deployment_id``      — operator-supplied slug
    - ``deployment_mode``    — always ``dev_local`` (this command's contract)
    - ``aws_profile``        — the AWS profile that owns the stack
    - ``aws_region``         — region the stack was deployed to
    - ``aws_account_id``     — preflight-assertion value (str, quoted on write)
    - ``aws``                — sub-dict of CFN-resolved ARNs/URLs
    - ``secrets``            — sub-dict of Secrets Manager names
    - ``local``              — sub-dict of local-runtime URLs (constants)

    Raises:
        KeyError: if a required CFN output suffix is absent from
            ``outputs``, naming the missing suffix.
    """
    aws_block = {
        yaml_key: _find_output(outputs, suffix)
        for yaml_key, suffix in _AWS_OUTPUTS
    }
    secrets_block = {
        yaml_key: _find_output(outputs, suffix)
        for yaml_key, suffix in _SECRETS_OUTPUTS
    }

    # ── Optional: observability stack outputs ─────────────────────────────
    # Gracefully skipped when TreadmillObservabilityStack was not deployed.
    # The ``observability_grafana_admin_secret_name`` entry receives the
    # *name* extracted from the ARN that CDK outputs.
    for yaml_key, suffix in _OBS_OUTPUTS:
        try:
            raw_value = _find_output(outputs, suffix)
        except KeyError:
            continue
        # The ARN output needs to be converted to a secret name.
        if yaml_key == "observability_grafana_admin_secret_name":
            raw_value = _extract_secret_name_from_arn(raw_value)
        aws_block[yaml_key] = raw_value

    # ── Optional: context-docs bucket (ADR-0054) ──────────────────────────
    # Absent on stacks deployed before the bucket was added; populated once
    # cloud_lite is redeployed. The API reads it as CONTEXT_DOCS_BUCKET.
    try:
        aws_block["context_docs_bucket"] = _find_output(
            outputs, "ContextDocsBucketName",
        )
    except KeyError:
        pass

    # ── Dev-local observability defaults ──────────────────────────────────
    # When the CFN output is absent (the normal dev_local case —
    # TreadmillObservabilityStack is fully_remote-only), fall back to the
    # local container-DNS endpoint so worker + API containers reach the
    # ``treadmill-otel-collector`` sibling on the ``treadmill-local``
    # docker network. ``http://127.0.0.1:4318`` would resolve to the
    # container itself, not the host; the compose port mapping serves the
    # host-side URL but is irrelevant for container-to-container traffic.
    for yaml_key, default_value in _DEV_LOCAL_OBSERVABILITY_DEFAULTS.items():
        aws_block.setdefault(yaml_key, default_value)

    return {
        "deployment_id": deployment_id,
        "deployment_mode": "dev_local",
        "aws_profile": aws_profile,
        "aws_region": aws_region,
        # str() coerces ints (account ids are 12 digits, sometimes
        # mis-passed as int by accident); PyYAML's str representer quotes
        # all-digit strings only with explicit style, so we set the
        # default_flow_style + default_style in ``write_deployment_yaml``
        # to ensure the account id round-trips as a string, never as an
        # int (Bash leading zeros, etc.).
        "aws_account_id": str(aws_account_id),
        "aws": aws_block,
        "secrets": secrets_block,
        "local": dict(_LOCAL_DEFAULTS),
        # ADR-0018: stamp the autoscaler defaults at init time so the
        # operator sees them in the file and can edit by hand.
        "autoscaler": dict(_AUTOSCALER_DEFAULTS),
    }


# ── YAML write ────────────────────────────────────────────────────────────────


def _yaml_dumper() -> type[yaml.SafeDumper]:
    """Return a YAML SafeDumper that quotes all-digit strings.

    The 12-digit AWS account id is a string, but PyYAML's default
    SafeDumper emits all-digit strings as bare scalars (``111111111111``)
    that ``yaml.safe_load`` then deserializes as ``int``. Bash leading-
    zero accounts (``012345678901``) would also be misinterpreted. We
    register a string representer on a subclass that forces
    single-quoted style for any all-digit string, preserving str
    round-trip for account ids and any future digit-only values.

    Registered on a subclass (not the global SafeDumper) so other
    ``yaml.safe_dump`` callers in the process aren't affected.
    """

    class _Dumper(yaml.SafeDumper):
        pass

    def _str_representer(dumper: yaml.SafeDumper, value: str):
        if value.isdigit():
            # Quote any all-digit string so it round-trips as str, not int.
            return dumper.represent_scalar(
                "tag:yaml.org,2002:str", value, style="'",
            )
        return dumper.represent_scalar("tag:yaml.org,2002:str", value)

    _Dumper.add_representer(str, _str_representer)
    return _Dumper


# ── YAML read (for ``treadmill-local up --deployment <id>``) ─────────────────


# Top-level keys that ``load_deployment_yaml`` requires. Mirrors the schema
# emitted by ``build_deployment_config`` + ADR-0016. We keep this as a single
# canonical tuple so the read-side and write-side don't drift; if a new key
# is added in C.4-extension or elsewhere, this tuple is the regression net.
_REQUIRED_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "deployment_id",
    "deployment_mode",
    "aws_profile",
    "aws_region",
    "aws_account_id",
    "aws",
    "secrets",
    "local",
)

_REQUIRED_AWS_KEYS: tuple[str, ...] = tuple(k for k, _ in _AWS_OUTPUTS)
_REQUIRED_SECRETS_KEYS: tuple[str, ...] = tuple(k for k, _ in _SECRETS_OUTPUTS)
_REQUIRED_LOCAL_KEYS: tuple[str, ...] = tuple(_LOCAL_DEFAULTS.keys())


def load_deployment_yaml(
    deployment_id: str,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Read ``~/.treadmill/<deployment_id>.yaml`` and return the parsed dict.

    Used by ``treadmill-local up --deployment <id>`` (and any sibling
    subcommand that takes ``--deployment``) to load the per-deployment
    config the ``init`` subcommand produced.

    Validates the top-level shape against ADR-0016's schema: the required
    top-level keys are all present, the ``aws`` / ``secrets`` / ``local``
    sub-dicts are dicts with the expected inner keys, and
    ``deployment_mode`` is the literal ``"dev_local"`` (the only mode
    this loader currently serves — fully-remote will eventually share
    the loader but isn't wired through to local-adapter callers yet).

    Args:
        deployment_id: The slug used to derive the default path
            (``~/.treadmill/<deployment_id>.yaml``).
        path: Optional override path. Mostly for tests.

    Returns:
        The parsed dict, structurally validated.

    Raises:
        FileNotFoundError: if the YAML file does not exist. The error
            message names the path and suggests ``treadmill-local init``
            as the remediation.
        ValueError: if the YAML is malformed (PyYAML parse error) or
            missing required top-level keys / inner-block keys. The
            message names exactly what's missing so the operator can
            fix the file (or re-run ``init``).
    """
    if path is None:
        path = Path.home() / ".treadmill" / f"{deployment_id}.yaml"
    path = Path(path).expanduser()

    if not path.exists():
        raise FileNotFoundError(
            f"deployment config not found at {path}; run "
            f"`treadmill-local init {deployment_id} --profile <profile>` "
            f"to create it."
        )

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"deployment config at {path} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"deployment config at {path} must be a YAML mapping at the "
            f"top level (got {type(raw).__name__})."
        )

    missing_top = [k for k in _REQUIRED_TOP_LEVEL_KEYS if k not in raw]
    if missing_top:
        raise ValueError(
            f"deployment config at {path} is missing required top-level "
            f"keys: {missing_top}. Re-run `treadmill-local init "
            f"{deployment_id}` to regenerate."
        )

    # The deployment-id field in the file must match the requested slug —
    # otherwise the operator is reading the wrong file (typo in the CLI
    # arg, or a stale symlink). Cheap to catch here.
    if raw["deployment_id"] != deployment_id:
        raise ValueError(
            f"deployment config at {path} has deployment_id "
            f"{raw['deployment_id']!r}, but was loaded for {deployment_id!r}. "
            f"Check the path or re-run `treadmill-local init`."
        )

    if raw["deployment_mode"] != "dev_local":
        raise ValueError(
            f"deployment config at {path} has deployment_mode "
            f"{raw['deployment_mode']!r}; only 'dev_local' is supported "
            f"by the local-adapter."
        )

    for block_key, required in (
        ("aws", _REQUIRED_AWS_KEYS),
        ("secrets", _REQUIRED_SECRETS_KEYS),
        ("local", _REQUIRED_LOCAL_KEYS),
    ):
        block = raw[block_key]
        if not isinstance(block, dict):
            raise ValueError(
                f"deployment config at {path} has '{block_key}' that is "
                f"not a mapping (got {type(block).__name__})."
            )
        missing = [k for k in required if k not in block]
        if missing:
            raise ValueError(
                f"deployment config at {path} is missing required "
                f"{block_key!r} keys: {missing}."
            )

    # ADR-0018: optional ``autoscaler:`` block. Defaults fill in when
    # absent, so downstream callers can always read
    # ``cfg["autoscaler"]["min" | "max" | "tick_seconds"]`` without a
    # nullable check. Validate ``0 <= min <= max`` and ``tick_seconds > 0``.
    autoscaler = raw.get("autoscaler")
    if autoscaler is None:
        raw["autoscaler"] = dict(_AUTOSCALER_DEFAULTS)
    else:
        if not isinstance(autoscaler, dict):
            raise ValueError(
                f"deployment config at {path} has 'autoscaler' that is "
                f"not a mapping (got {type(autoscaler).__name__})."
            )
        merged = dict(_AUTOSCALER_DEFAULTS)
        merged.update(autoscaler)
        # Validate types + bounds. Booleans are ints in Python; reject
        # them explicitly so an operator who wrote ``min: true`` doesn't
        # silently get min=1.
        for key in ("min", "max", "tick_seconds"):
            value = merged[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"deployment config at {path} has autoscaler.{key} = "
                    f"{value!r}; expected an int."
                )
        if merged["min"] < 0:
            raise ValueError(
                f"deployment config at {path} has autoscaler.min = "
                f"{merged['min']}; expected >= 0."
            )
        if merged["max"] < merged["min"]:
            raise ValueError(
                f"deployment config at {path} has autoscaler.max "
                f"({merged['max']}) < autoscaler.min ({merged['min']})."
            )
        if merged["tick_seconds"] <= 0:
            raise ValueError(
                f"deployment config at {path} has autoscaler.tick_seconds "
                f"= {merged['tick_seconds']}; expected > 0."
            )
        raw["autoscaler"] = merged

    return raw


def write_deployment_yaml(
    deployment_id: str,
    config: dict[str, Any],
    *,
    path: Path | None = None,
) -> Path:
    """Write *config* to ``~/.treadmill/<deployment_id>.yaml`` (or *path*).

    Creates the parent directory (mkdir -p) if needed. Overwrites any
    existing file at the path — the init command is the operator's
    "regenerate from current stack state" lever, so idempotency on
    re-run is part of the contract.

    Returns the resolved path actually written to.
    """
    if path is None:
        path = Path.home() / ".treadmill" / f"{deployment_id}.yaml"
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    text = yaml.dump(
        config,
        Dumper=_yaml_dumper(),
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    path.write_text(text)
    return path
