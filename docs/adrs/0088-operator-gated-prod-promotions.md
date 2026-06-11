# ADR-0088: Operator-gated prod promotions — API-enforced approval

- **Status:** superseded (2026-06-11, operator directive) — deploy
  approval uses **GitHub environment protection with required
  reviewers** (medicoder's incumbent system, which this ADR failed to
  evaluate as an alternative); Treadmill is team orchestration, not a
  deploy control plane. See
  `docs/learnings/2026-06-11-check-the-incumbent-before-designing.md`.
  The implementation (PRs #304/#306/#307-§3.8) was reverted the same
  night; the deploy/staging_smoke observe vocabulary (§3.7) survives as
  team telemetry.
- **Date:** 2026-06-10
- **Related:** ADR-0086 (coordinator owns task lifecycle), ADR-0087 (team
  execution model), `docs/plans/2026-06-10-prod-promotion-gate-contract.md`
  (the contract of record this ADR implements — PR #302), medicoder
  `docs/plans/2026-06-10-gcp-staging-standup.md` (deploy/smoke vocabulary),
  medicoder `docs/roadmaps/2026-06-10-aws-to-gcp-migration.md` (step B)

## Context

The AWS→GCP migration roadmap requires production deploys to pass a human
gate: the coordinator proposes a promotion with evidence, the operator
approves, and only then does anything deploy. The contract doc (PR #302)
fixes the interface — event vocabulary, propose-bundle shape, operator
command shape, and five safety invariants. This ADR decides the three
substrate questions the contract left open, plus the coordinator-template
mechanics.

The governing principle: **the gate lives in the state machine, not in
prose.** A template instruction saying "do not approve promotions" is
guidance; an API that structurally cannot record an approval from a
coordinator session is enforcement. We build the second and keep the first
as documentation.

## Decision

### 1. Storage: dedicated `prod_promotions` table, events emitted alongside

A `prod_promotions` row holds current state (`proposal_id` PK, `repo`,
`status`, `bundle` JSONB, `expires_at`, `decided_by`, `decided_at`,
`decision_note`). Status transitions are enforced by guarded `UPDATE ...
WHERE status = '<expected>' AND expires_at > now()` (single-use invariant 2
falls out of row-state compare-and-swap, and the expiry predicate on the
same guard closes the expired-but-undecided hole — a status-only check
would approve a stale proposal exactly where invariant 3 matters most;
Carla's contract-author review, #303). Reads apply the same treatment
lazily: the GET reports `expired` for an undecided row past `expires_at`,
emitting `prod_promotion.expired` on first such read. Every transition
also emits the
contract's `prod_promotion.*` event (audit-class, `proposal_id`
discriminator) — the events table stays the audit trail; the row is the
current-status read the workflow re-verifies against
(`GET /api/v1/prod_promotions/{id}`).

Rationale: single-use and expiry need a compare-and-swap surface; deriving
"current status" from an append-only event log re-creates the projection
machinery ADR-0087 just deleted. One table, one projection, events for audit.

### 2. Approval authz: operator key, v1

`POST /api/v1/prod_promotions/{id}/approve` (and `/reject`) require an
`X-Operator-Key` header matching `TREADMILL_OPERATOR_KEY` from the API
environment. The key lives in exactly one place: the operator's shell
profile on the operator's machine. Coordinator and worker sessions never
hold it — their `treadmill` CLI physically lacks the credential, so a
confused or prompt-injected session cannot approve regardless of what its
template says. The coordinator template's "you do not approve promotions"
line remains as documentation of intent, not as the mechanism.

Propose stays unauthenticated-but-attributed (same trust level as every
other coordinator API write today); only the decision edge is keyed. A
later authenticated-principal system can replace the shared key without
touching the contract.

### 3. Dispatch firer: the CLI, not the API

`treadmill promote approve` does two things in order: records the approval
(keyed API call), then fires `gh workflow run promote-to-prod.yml -f
proposal_id=<id>`. The API never holds GitHub write credentials. Contract
invariant 1 already makes the dispatcher untrusted — the workflow's first
step re-verifies the proposal against the API (status=approved, digest set
byte-identical) and aborts on mismatch — so trusting the operator's
machine to fire the dispatch adds no risk beyond what the operator already
holds. If the dispatch fails after the approval is recorded, re-running the
command is safe: approve is idempotent on an already-approved proposal
(returns current state), and the workflow's single-use check (invariant 2,
`started` transition) prevents double-deploys.

### 4. Genesis anchor for `diff_summary`

The first proposal for a repo has no prior promotion to diff against. The
coordinator anchors on the staging baseline: `diff_summary` covers merges
since the sha of the first green `staging_smoke.passed` event for that
repo, and the bundle says so explicitly (`"diff_anchor": "genesis:
<sha>"`). Subsequent proposals anchor on the sha of the last
`prod_promotion.succeeded`.

### 5. Coordinator template mechanics (§3 additions)

- On `deploy.succeeded` + `staging_smoke.passed` for the same sha, the
  coordinator MAY assemble and POST a propose bundle (it is a proposal,
  not a duty — cadence is operator-tunable).
- On `prod_promotion.failed`: escalate to the submitting orchestrator with
  the event payload and offer the rollback registered-task shape. Never
  retry a prod deploy automatically; never freeze the merge queue.
- Explicit line: the coordinator does not approve, reject, or dispatch
  promotions, and does not hold the operator key.

## Consequences

- Joe's approval surface is one CLI command with the diff_summary in front
  of him; Telegram remains a lens (the orchestrator relays `promote list`
  and types the command on Joe's instruction — the key stays on the
  operator machine).
- The events table gains one audit-class entity (`prod_promotion`); the
  schema gains one table; the CLI gains a `promote` subcommand group; the
  coordinator template gains one §3 block. No worker surface changes.
- The shared-key model is deliberately minimal: it gates the single most
  dangerous write in the system and nothing else. Broadening authn is a
  separate, later decision.
- The workflow re-verification step is the load-bearing safety property;
  its absence in any future promote workflow is a contract violation, and
  the prod-promotion plan (Carla's G3) must pin it with a test.

## Out of scope

- Multi-operator approval / quorum (one operator today).
- Automatic proposal on every staging green (cadence stays coordinator
  judgment + operator tuning).
- Rollback automation beyond the registered-task shape.
- Replacing the operator key with authenticated principals.
