"""Prod-promotion + deploy + staging-smoke event payloads.

Per the gate contract (docs/plans/2026-06-10-prod-promotion-gate-contract.md)
and ADR-0088. All three entity types are audit-class: no dedup, one action
per concept, the discriminator lives in the payload (``proposal_id`` for
prod_promotion; ``sha`` for deploy / staging_smoke, which repeat per merge).

The deploy + staging_smoke vocabulary is the staging plan's companion
surface — the coordinator subscribes to these (observe + escalate), and
green ``deploy.succeeded`` + ``staging_smoke.passed`` pairs are the
evidence a prod-promotion propose bundle links to.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from treadmill_api.events.base import EventPayload


class _ServiceDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    digest: str


class _StagingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deploy_event_id: uuid.UUID
    smoke_event_id: uuid.UUID
    sha: str
    smoke_passed_at: datetime


# ── prod_promotion.* ─────────────────────────────────────────────────────────


class ProdPromotionProposed(EventPayload):
    """Coordinator assembled a bundle from green staging evidence."""

    ENTITY_TYPE: ClassVar[str] = "prod_promotion"
    ACTION: ClassVar[str] = "proposed"

    proposal_id: uuid.UUID
    repo: str
    env_from: str
    env_to: str
    digests: list[_ServiceDigest]
    staging_evidence: _StagingEvidence
    diff_summary: list[str]
    # "genesis:<sha>" for a repo's first promotion (no prior promotion to
    # diff against); the sha of the last prod_promotion.succeeded after.
    diff_anchor: str
    expires_at: datetime
    proposed_by: str


class ProdPromotionApproved(EventPayload):
    """Operator decision recorded (keyed endpoint, ADR-0088 §2)."""

    ENTITY_TYPE: ClassVar[str] = "prod_promotion"
    ACTION: ClassVar[str] = "approved"

    proposal_id: uuid.UUID
    repo: str
    decided_by: str
    note: str | None = None


class ProdPromotionRejected(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "prod_promotion"
    ACTION: ClassVar[str] = "rejected"

    proposal_id: uuid.UUID
    repo: str
    decided_by: str
    reason: str


class ProdPromotionExpired(EventPayload):
    """Proposal aged out undecided — emitted lazily on first read past
    ``expires_at`` (ADR-0088 §1)."""

    ENTITY_TYPE: ClassVar[str] = "prod_promotion"
    ACTION: ClassVar[str] = "expired"

    proposal_id: uuid.UUID
    repo: str


class ProdPromotionStarted(EventPayload):
    """promote-to-prod workflow began executing an approved proposal."""

    ENTITY_TYPE: ClassVar[str] = "prod_promotion"
    ACTION: ClassVar[str] = "started"

    proposal_id: uuid.UUID
    repo: str
    workflow_run_id: str | None = None


class ProdPromotionSucceeded(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "prod_promotion"
    ACTION: ClassVar[str] = "succeeded"

    proposal_id: uuid.UUID
    repo: str
    sha: str
    digests: list[_ServiceDigest]


class ProdPromotionFailed(EventPayload):
    """Terminal deploy failure — coordinator escalates, never auto-retries
    (contract invariant 5). ``reason`` carries the abort class, e.g.
    ``digest_mismatch`` from the workflow's re-verification step."""

    ENTITY_TYPE: ClassVar[str] = "prod_promotion"
    ACTION: ClassVar[str] = "failed"

    proposal_id: uuid.UUID
    repo: str
    reason: str


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
