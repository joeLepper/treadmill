"""Deploy + staging-smoke event payloads.

The staging plan's companion vocabulary: CI emits these; coordinators
OBSERVE and escalate on failures (template §3.7) — never deploy control.
Audit-class: no dedup, one action per concept, ``sha`` discriminator
(deploys repeat per merge).

History: this module once also carried the ADR-0088 ``prod_promotion.*``
vocabulary, removed 2026-06-11 when the operator superseded the gate in
favor of GitHub environment protection (deploy approval is the repo's
own CI concern; Treadmill is team orchestration).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from treadmill_api.events.base import EventPayload


class _ServiceDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    digest: str


# ── deploy.* (staging-plan companion vocabulary) ─────────────────────────────


class DeployStarted(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "deploy"
    ACTION: ClassVar[str] = "started"

    repo: str
    env: str
    sha: str
    services: list[str]


class DeploySucceeded(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "deploy"
    ACTION: ClassVar[str] = "succeeded"

    repo: str
    env: str
    sha: str
    digests: list[_ServiceDigest]


class DeployFailed(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "deploy"
    ACTION: ClassVar[str] = "failed"

    repo: str
    env: str
    sha: str
    reason: str


# ── staging_smoke.* ──────────────────────────────────────────────────────────


class StagingSmokePassed(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "staging_smoke"
    ACTION: ClassVar[str] = "passed"

    repo: str
    sha: str
    run_url: str | None = None


class StagingSmokeFailed(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "staging_smoke"
    ACTION: ClassVar[str] = "failed"

    repo: str
    sha: str
    reason: str
    run_url: str | None = None
