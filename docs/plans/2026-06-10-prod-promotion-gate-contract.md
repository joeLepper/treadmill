# Contract: prod-promotion human gate — interface spec

- **Status:** superseded (2026-06-11, operator directive) — prod
  approval is GitHub environment protection with required reviewers.
  Preserved as the historical record; the Evidence semantic floor
  section's REQUIREMENTS survive as workflow preconditions in the
  medicoder promote pipeline (the smoke gates the environment-protected
  deploy job), not as a Treadmill bundle.
- **Date:** 2026-06-10
- **Consumers:** Alan's human-gate ADR (API + coordinator-template mechanics) and
  Carla's prod-promotion plan (medicoder `promote-to-prod.yml` + pipeline)
- **Related:** medicoder `docs/plans/2026-06-10-gcp-staging-standup.md`
  (Treadmill-side companion section defines the deploy/staging_smoke vocabulary
  this contract extends); #301 event-vocabulary discipline

## Flow (v1)

```
sequenceDiagram
    participant STG as staging pipeline (medicoder CI)
    participant COORD as coordinator-medicoderhq-medicoder
    participant API as Treadmill API / events table
    participant JOE as operator (Joe)
    participant GH as promote-to-prod.yml

    STG->>API: deploy.succeeded + staging_smoke.passed (sha, digests)
    COORD->>API: POST prod_promotion.proposed (bundle, proposal_id)
    API-->>JOE: notification (Telegram relay / treadmill promote list)
    JOE->>API: treadmill promote approve <proposal_id>
    API->>API: validate (status=proposed, not expired, digest set intact)
    API->>GH: workflow_dispatch promote-to-prod (proposal_id) — firer is an ADR call, see Open items
    GH->>API: GET proposal — re-verify approved + digests match
    GH->>GH: deploy EXACTLY the approved digest set
    GH->>API: prod_promotion.succeeded | failed (sha, digests)
    API-->>COORD: terminal event — escalate on failed
```

## Event vocabulary

`entity_type='prod_promotion'`, audit-class (no dedup; discriminator =
`proposal_id` in payload). One action per concept:

| action | emitter | meaning |
|---|---|---|
| `proposed` | coordinator | bundle assembled from green staging evidence |
| `approved` | API (on operator command) | operator decision recorded |
| `rejected` | API (on operator command) | operator decision + reason |
| `expired` | API (sweep or lazy on read) | proposal aged out undecided |
| `started` | promote-to-prod.yml | workflow began executing an approved proposal |
| `succeeded` / `failed` | promote-to-prod.yml | terminal deploy result |

## Propose bundle (payload of `proposed` — what the coordinator must carry)

```json
{
  "proposal_id": "<uuid — correlation key for every later action>",
  "repo": "MediCoderHQ/medicoder",
  "env_from": "staging",
  "env_to": "prod",
  "digests": [{"service": "<name>", "digest": "sha256:<...>"}],
  "staging_evidence": {
    "deploy_event_id": "<events.id of deploy.succeeded>",
    "smoke_event_id": "<events.id of staging_smoke.passed>",
    "sha": "<main sha the staging deploy ran>",
    "smoke_passed_at": "<iso8601>"
  },
  "diff_summary": ["<PR numbers / shas included since the last prod promotion>"],
  "expires_at": "<iso8601 — default proposed_at + 48h>",
  "proposed_by": "coordinator-medicoderhq-medicoder"
}
```

`diff_summary` is load-bearing: it is **what Joe is actually approving** —
the human gate is only as good as the summary in front of the human.

**Genesis anchor:** the FIRST promotion has no prior prod promotion to diff
against. Its `diff_summary` anchors to the staging-stand-up baseline: every
merge to main since the staging plan's first green `staging_smoke.passed`
(the moment the digest set became evidence-bearing), with the bundle's
`staging_evidence.sha` as the upper bound. Subsequent promotions diff from
the last `prod_promotion.succeeded` sha.

## Operator command shape (what Joe types)

```bash
treadmill promote list                      # pending proposals, newest first
treadmill promote show <proposal_id>        # full bundle incl. diff_summary
treadmill promote approve <proposal_id> [--note "..."]
treadmill promote reject <proposal_id> --reason "..."
```

Telegram convenience (optional, later): the orchestrator relays `promote list`
output and runs the approve command on Joe's typed instruction — the CLI
remains the single write path; Telegram is a lens, not a second surface.

## Evidence semantic floor (2026-06-11 amendment)

`staging_smoke.passed` may only be emitted by a smoke that attests, against
REAL backends (no stubs, no emulators):

1. **Real inference** — the LLM-calling services (MAR, NTA) made live
   model calls and produced output (call count > 0 asserted, not inferred
   from health).
2. **Per-service contribution** — every chain stage's output present
   (the dead-MAR lesson: health endpoints lie).
3. **Consumer idle-wake** — a message published to an IDLE chain is
   processed within the smoke's settle budget. Outcome-based, not
   instance-count-based: under always-on consumers (medicoder #1348,
   minInstances=1) idle means warm-but-quiescent; under backlog
   autoscaling (medicoder #143) zero instances on an empty queue is
   CORRECT, and the evidence must distinguish empty-queue-zero from
   dead-consumer-zero by publishing and observing processing — never by
   counting instances. (The min-instances=0 stall class: a warm-only
   smoke proves nothing about idle prod.)

`Ready=True` alone is never promotion evidence. Basis: the 2026-06-11
staging e2e caught three prod-breaking configs (medicoder #141 dead
Bedrock default, #142 zero-pull idle stall, SQL bootstrap mismatches)
on a chain that was 8/8 green by readiness — a bundle assembled from
readiness-only evidence would have promoted a system producing zero
inference and zero idle processing.

## Safety properties (the contract's invariants)

1. **Digest-pinned approval (TOCTOU-proof).** Approval binds to the exact
   digest set in the bundle. The workflow re-verifies the proposal against the
   API before deploying and deploys exactly those digests — never `latest`,
   never re-resolved tags. A digest mismatch aborts with `failed` +
   `reason=digest_mismatch`.
2. **Single-use.** A proposal transitions `proposed → approved → started →
   succeeded|failed` exactly once. Re-running the workflow with a consumed
   proposal_id aborts.
3. **Expiry.** Default 48 h. Expired proposals cannot be approved — staging
   evidence goes stale; re-propose with fresh evidence. (Gates ship with
   expiry; precedent: 2026-06-10 merge-hold discipline.)
4. **Role separation.** Only the coordinator proposes (the bundle is its §3
   observation product). Only the operator approves — the coordinator template
   carries an explicit "you do not approve promotions" line (Alan's side);
   workers have no path to the command at all.
5. **Failure routes, never freezes.** `prod_promotion.failed` → coordinator
   escalation (same decoupled-gating discipline as staging) + the rollback
   registered-task shape. No automatic retry of prod deploys.

## Open items (for Alan's ADR, not this contract)

- Storage: dedicated `prod_promotions` table vs events-table projection — the
  contract only requires that `GET /api/v1/prod_promotions/{id}` returns
  current status + bundle; Alan picks the substrate.
- **Who fires `workflow_dispatch`**: CLI-fires (operator machine, operator
  GitHub creds) vs API-fires (App identity, post-approve hook). Invariant 1
  already makes the dispatcher untrusted — the workflow re-verifies the
  proposal against the API regardless — so the contract survives either
  answer; the ADR picks.
- Operator authz enforcement: v1 convention (CLI on operator's machine) vs a
  later authenticated-principal check.
- The expiry sweep mechanism (lazy-on-read suffices for v1).
