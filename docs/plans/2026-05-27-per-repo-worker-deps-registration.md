# Plan: Per-repo worker-dep registration

- **Status:** completed
- **Date:** 2026-05-27
- **Related ADRs:** ADR-0059, ADR-0050 (onboarding persistence), ADR-0055 (per-account Claude credentials), ADR-0058 (gate-broken verdict)

## Goal

Build the per-repo `worker_deps` seam described in ADR-0059 so the
worker materializes a repo's tooling (Python/Node/binaries) at task
dispatch time without touching the shared agent image. Unblock the
5 wedged RAMJAC tasks (canonical `aws-cdk-lib` missing-dep case)
with a registration update instead of a new image build.

## Success criteria

- A new `worker_deps` field on `RepoConfig` is round-trippable via
  `POST /api/v1/onboarding/repos` and `GET /api/v1/onboarding/repos/{repo:path}`,
  end-to-end with a Pydantic model + migration.
- A worker handling a task for a repo with `worker_deps.python` set
  materializes a venv with those exact pinned packages, BEFORE the
  task's first step runs.
- The same worker handling a second task for the same repo skips
  re-materialization when the deps-hash is unchanged.
- A bad registration (typo, conflicting versions) emits a
  `task.worker_deps_failed` event; the architect's ADR-0058
  `gate-broken` classifier (when that lands) recognizes it as
  gate-broken, not amend.
- The RAMJAC `worker_deps` registration (`["aws-cdk-lib==X.Y.Z",
  "constructs==Z.W"]`) unblocks the 5 wedged tasks on next
  re-dispatch.

## Constraints / scope

### In scope

- `WorkerDeps` + `BinarySpec` Pydantic models in
  `services/api/treadmill_api/onboarding_store.py` (or sibling).
- Migration: `repo_configs.worker_deps` (JSONB or typed columns —
  see decision-during-execution below).
- Onboarding HTTP: PUT/GET accept + return `worker_deps`.
- Worker overlay materialization for Python (venv) + Node
  (node_modules); binary downloads via signed-URL + checksum verify.
- Network egress allowlist during the install phase only; restore
  hermetic networking for task work. Implementation may need a sidecar
  proxy or a per-phase iptables rule — open question for execution.
- `(repo, deps-hash)` cache check on every task claim so warm workers
  skip reinstall.
- `task.worker_deps_failed` event + ADR-0030 doc updates in
  `workers/agent/AGENT.md` and `services/api/AGENT.md`.
- Tests: unit (model round-trip, hash determinism) +
  integration (a synthetic task with a Python dep, verify it lands).
- CLI surface: `treadmill onboarding update <repo>
  --worker-deps-python <spec> ...`.

### Out of scope

- **Apt / OS-package support.** No observed demand and the privilege
  surface needs separate design (image immutability, sudo discipline,
  rollback semantics). Defer to v2 if a real demand signal emerges.
- **Automatic dep discovery via wf-discover.** The ADR mentions
  discovery as a sibling registration path; this plan ships the
  *manual operator update* path first, because that's what unblocks
  RAMJAC tonight. Discovery is a follow-up.
- **Cross-repo dep sharing / dedup.** Each repo gets its own overlay
  even if two repos pin the same package version. Storage is cheap;
  dedup adds complexity without observed benefit.
- **Live RAMJAC unblock as part of this plan.** The fix this plan
  enables; the operator runs the registration update once the seam
  ships.

### Budget

Two focused sessions, ~3 PRs (schema + HTTP; worker materialization;
discovery wiring). If session 3 ends without a working end-to-end
("a synthetic task with a Python dep materializes + runs"), abort
and post-mortem — the design may need more carving (especially the
egress-scoping piece, which has the most unknowns).

## Sequence of work

1. **Schema + HTTP** (~1 day) — `WorkerDeps` + `BinarySpec` Pydantic
   models; migration; `OnboardingStore.upsert_repo_config` round-trips
   the new field; HTTP accept + return. Mirror ADR-0055's
   `claude_account` shape. **Files:**
   `services/api/treadmill_api/onboarding_store.py`,
   `services/api/treadmill_api/models/onboarding.py`,
   `services/api/treadmill_api/routers/onboarding.py`,
   `services/api/alembic/versions/<new>.py`, `services/api/tests/test_onboarding_store.py`,
   `services/api/AGENT.md`.
2. **Worker materialization** (~1 day) — given a repo's `WorkerDeps`,
   build the overlay (venv for python, node_modules for node, signed
   binaries to a per-repo bin dir). Cache by `(repo, deps-hash)`.
   Activate the overlay before invoking task work. **Files:**
   `workers/agent/treadmill_agent/repo_deps.py` (new),
   `workers/agent/treadmill_agent/runtime.py`,
   `workers/agent/tests/test_repo_deps.py`, `workers/agent/AGENT.md`.
3. **Egress scoping** (~½ day) — during install phase, allow
   registries-only egress; revert to no-egress for task work. Likely
   a per-phase iptables rule in the worker container's entrypoint, or
   a small forwarding proxy. Decide between in-container iptables
   (simpler) vs. sidecar proxy (more auditable). **Files:** worker
   container entrypoint script(s), possibly the agent image's
   Dockerfile.
4. **Failure event + classifier hook** (~½ day) —
   `task.worker_deps_failed` event; emit on install failure with the
   stderr captured. ADR-0058 classifier (when it lands) reads this
   event as a `gate-broken` signal. **Files:**
   `services/api/treadmill_api/events/task.py`,
   `workers/agent/treadmill_agent/repo_deps.py`,
   `services/api/treadmill_api/coordination/triggers.py` (if needed).
5. **CLI surface** (~½ day) — `treadmill onboarding update <repo>
   --worker-deps-python <spec>` with a multi-value flag pattern.
   **Files:** `cli/treadmill_cli/onboarding.py` (or sibling),
   `cli/treadmill_cli/api_client.py`,
   `cli/tests/test_onboarding_update.py`.
6. **Integration smoke** (~½ day) — synthetic task in a dev-local
   onboarded repo with `worker_deps.python` pinned to a small unique
   package; verify the worker installs it, the gate runs against the
   overlay, and a second task on the same worker skips re-install.

Tasks 1, 2 are sequential. 3 can parallelize with 2 once the overlay
surface is stable. 4 + 5 + 6 can parallelize once 1–3 are in.

Discovery wiring (`wf-discover` proposes a `WorkerDeps`) is a
**separate plan**, not in this plan's scope — manual registration
ships first.

## Decisions captured during execution

- **JSONB vs. typed columns for `worker_deps`.** ADR-0011 prefers
  typed columns, but worker_deps is a structured-but-nested shape
  (Python list, apt list, node list, binaries-with-checksum list).
  Three columns + a separate `repo_worker_binaries` table is the
  ADR-0011-consistent path; JSONB is faster to ship. Decide on
  authoring; this plan defers to first PR.
- **Bunkhouse precedent.** Per
  [[feedback_bunkhouse_precedent_shapes]], we should check what
  bunkhouse landed on for per-repo deps before finalizing the shape.
  Open question; ADR-0059 acknowledges it. Resolve before PR 1.
- **Egress mechanism.** In-container iptables (simpler, lives in the
  agent image) vs. sidecar proxy (more auditable, separable). Decide
  in step 3.

## Risks / unknowns

- **Bunkhouse precedent unknown.** If bunkhouse has a precedent for
  per-repo deps that's different from this ADR's shape, we should
  adopt it. **Abort condition:** we discover bunkhouse precedent that
  conflicts with this design; revisit ADR-0059 before continuing
  implementation.
- **Egress scoping fragility.** Per-phase network policy is the
  riskiest piece. A misconfigured allowlist could either fail open
  (task can reach arbitrary endpoints during install — security
  regression) or fail closed (legitimate registry access blocked —
  the wedge moves from missing-dep to install-failure). Mitigation:
  start with a known-good allowlist (pypi.org, files.pythonhosted.org,
  registry.npmjs.org), test extensively, surface failures as
  `task.worker_deps_failed` so they don't masquerade as code defects.
- **Bad-registration UX.** Operators registering deps need fast
  feedback when they typo a package name. A registration update that
  silently breaks the next dispatch is worse than today's missing-dep
  failure. Mitigation: validate `WorkerDeps` on registration —
  dry-run a `pip install --dry-run` (or equivalent) before persisting
  the config; reject the update with a useful 400 if the resolver
  fails.
- **We'll abort if** bunkhouse precedent is incompatible AND
  redesigning around it pushes us past the 2-session budget.

## Diagram

```mermaid
flowchart LR
    Operator -- "PUT worker_deps" --> OnboardingAPI
    OnboardingAPI -- "persist" --> RepoConfig[(repo_configs)]
    Worker -- "claim task" --> WorkQueue
    Worker -- "load repo config" --> OnboardingAPI
    Worker -- "check (repo, hash) cache" --> Cache[(repo-overlay cache)]
    Worker -- "materialize overlay (venv / node_modules / bin)" --> Overlay[(/var/treadmill/repo-{venvs,node,bin}/<repo>)]
    Worker -- "activate overlay; run task gates" --> Gate
    Gate -- "fail" --> WorkerDepsFailed[task.worker_deps_failed]
    Gate -- "pass" --> TaskWork[(normal task execution)]
```

## Post-mortem

(filled in on completion / abandonment)
