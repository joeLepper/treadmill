# ADR-0059 — Per-repo worker-dep registration

- **Status:** proposed
- **Date:** 2026-05-27
- **Supersedes:** none
- **Related:** ADR-0050 (onboarding persistence), ADR-0051 (onboarding HTTP), ADR-0054 (mode + repo context), ADR-0055 (per-account Claude credentials), ADR-0058 (gate-broken verdict)

## Context

When Treadmill onboards a non-Treadmill repo, that repo's tasks may
need tooling the base agent image doesn't carry. The 2026-05-27
RAMJAC incident is the canonical example: the agent image got the
`cdk` CLI binary (PRs #28/#30/#31 earlier the same day) but not the
`aws-cdk-lib` Python package. Five tasks wedged at
`import aws_cdk` → `ModuleNotFoundError` → `Script exited 1` →
architect amend loop → cap → operator.

The naive fix — add `aws-cdk-lib` to the agent image — works for
RAMJAC but doesn't generalize. The next onboarded repo will bring its
own dep list: Node packages, terraform CLI, kubectl, language
toolchains, custom internal libraries. Coupling every onboarded repo
to a single shared image means:

- The image bloats unboundedly as the fleet grows.
- A dep upgrade for repo A can break repo B (shared site-packages
  collisions, version pinning conflicts).
- The image rebuild cycle becomes the bottleneck for onboarding a new
  repo, defeating the ADR-0050 onboarding goal (point Treadmill at an
  unfamiliar repo, have it work).
- Operator papercuts: every "make sure dep X is in the image" PR is
  toil that doesn't compose.

We need a **per-repo dep registration** seam where each onboarded
repo's worker requirements are part of the repo's persistent config,
not the worker image.

## Decision

Extend the ADR-0050 onboarding model with a typed `worker_deps` field
on `RepoConfig`. Workers materialize the deps before invoking any task
for that repo. The base image stays narrow and stable; per-repo
specificity lives in the registration.

### Shape

`RepoConfig.worker_deps: WorkerDeps` (nullable; `None` = no extra deps).

```python
class WorkerDeps(BaseModel):
    """ADR-0059: per-repo extras the worker installs before task work."""
    python: list[str] = []          # pip-style pkg specs, exact-pinned
    apt: list[str] = []             # OS packages (debian/ubuntu names)
    node: list[str] = []            # npm package specs
    binaries: list[BinarySpec] = [] # downloaded CLI binaries
```

Each list is opt-in; an empty list means "no deps of this kind." A
repo that needs only Python deps doesn't carry empty apt / node /
binaries arrays in its serialized form (Pydantic default-empty).

### Materialization

Workers materialize per-repo deps in a **per-repo overlay**, not the
shared worker site-packages:

- Python: `python -m venv /var/treadmill/repo-venvs/<repo>` once per
  repo; `<venv>/bin/pip install <pinned-specs>`; activate when the
  worker runs the task's gate.
- Node: `/var/treadmill/repo-node/<repo>/node_modules` via
  `npm install --prefix`.
- Apt: out-of-scope for v1 (privilege boundary; see Out-of-scope).
- Binaries: download via signed URL or known mirror to
  `/var/treadmill/repo-bin/<repo>/<name>`; verify checksum.

Materialization happens **once per repo per worker container**, cached
by `(repo, deps-hash)`. A worker handling the next task for the same
repo skips reinstall when the hash matches; a hash change triggers a
re-materialization.

### Registration paths

Two ways to register deps, mirroring the ADR-0050 / ADR-0051 split:

1. **Discovery** (the common case) — `wf-discover` reads
   `requirements.txt` / `pyproject.toml` / `package.json` etc. and
   proposes a `WorkerDeps`. The operator reviews + accepts on
   onboarding.
2. **Operator update** — `POST /api/v1/onboarding/repos/{repo:path}`
   accepts a `worker_deps` field (already accepts `claude_account`
   per ADR-0055; same shape). Operators correct mis-detected deps or
   add ones the discovery missed (the RAMJAC case — `aws-cdk-lib` is
   in `requirements.txt` but discovery may not be wired to install
   for the CDK case specifically).

### Sandbox + security boundary

The agent container today has no network egress by default (per the
plan-skill SKILL.md). Dep materialization requires egress to the
package index. We make that exception **scoped** by phase:

- During dep materialization: egress allowed to a closed set of
  registries (PyPI, npm, a wheelhouse proxy we host, signed-URL
  bucket for binaries). Outbound to anything else is still blocked.
- During task work: no egress, as today. Materialized deps are
  installed at this point; the workspace is hermetic.

Treadmill is owner-trusted today (the operator vets onboarded repos);
this preserves that boundary. A malicious dep in a vetted repo would
already be a problem at code-execution time, not just at install
time — registering deps doesn't expand the trust surface.

### Versioning + reproducibility

All package specs MUST be exact-pinned (`aws-cdk-lib==2.214.0`, not
`aws-cdk-lib>=2`). The same deps-hash produces the same install
across workers, across rebuilds, and across replays of historical
tasks. A repo that wants version flexibility expresses it by updating
the registration, not by floating specs.

## Consequences

**Positive:**

- The 5 currently-wedged RAMJAC tasks become unblockable with one
  registration update (`worker_deps.python: ["aws-cdk-lib==X.Y.Z",
  "constructs==Z.W"]`), no agent-image rebuild required.
- The base agent image stays small and stable across repo onboarding.
- The onboarding bar drops: new repos don't need an agent-image PR
  cycle to be useful.
- The pattern composes with ADR-0055 (per-account credentials) and
  ADR-0054 (per-repo context docs) — same shape, same surface, same
  persistence layer.

**Negative:**

- Worker startup time grows by the dep-install duration on first task
  per repo. Mitigation: cache per `(repo, deps-hash)`; warm cache
  amortizes the cost; the operator can pre-warm by triggering a
  no-op task on a fresh worker.
- A bad registration (typo in a package name, conflicting versions)
  becomes a new failure mode. Mitigation: surface this clearly —
  install failures emit a distinct event (`task.worker_deps_failed`)
  rather than a generic step failure, and the ADR-0058 `gate-broken`
  verdict's classifier should treat them as gate-broken, not amend.
- Network egress during install widens the trust surface compared to
  the current "no egress" stance. Mitigation: registries-only egress;
  network policy enforced at the container boundary, not by trusting
  the install command.

**Neutral:**

- Apt / OS-package support is deferred. v1 covers Python + Node +
  binary downloads, which is what the observed RAMJAC failure
  category needs. Apt requires a more careful design (privilege
  escalation, image immutability) and a real demand signal before
  we commit.

## Sequence (high-level — full step list in the plan)

1. Schema: `RepoConfig.worker_deps` + migration; `WorkerDeps` +
   `BinarySpec` Pydantic models.
2. Onboarding HTTP: accept + return `worker_deps` (mirror ADR-0055's
   `claude_account` path).
3. Worker side: per-repo overlay materialization (venv / node_modules
   / bin) with `(repo, deps-hash)` cache.
4. Network egress scoping: registries-only allowlist during install
   phase; restore no-egress for task work.
5. Failure event: `task.worker_deps_failed` event; ADR-0058 classifier
   handles it as gate-broken.
6. wf-discover extension: detect Python/Node deps from the
   repo's standard files; propose a `WorkerDeps`.
7. Operator CLI: `treadmill onboarding update <repo>
   --worker-deps-python aws-cdk-lib==2.214.0 ...` for direct
   registration updates.

## Alternatives considered

**Bake everything into the agent image** (status quo). Rejected for
the reasons in Context — unbounded image growth, version-pin
collisions, slow onboarding.

**Per-task dep specs** (each task carries its own deps). Rejected
because deps are a property of the *repo*, not the *task* — a repo's
test infrastructure is stable across its tasks, and per-task specs
would force every plan to re-state the same dep list.

**Pre-build per-repo agent images** (one image per onboarded repo).
Rejected because it shifts the bottleneck from "agent-image PR" to
"per-repo image build pipeline" — still gates onboarding on infra
work; doesn't compose with ADR-0050's "point Treadmill at an
unfamiliar repo" goal.

**Audit step (bunkhouse precedent):** per memory
[[feedback_bunkhouse_precedent_shapes]], we should check what
bunkhouse landed on for per-repo deps before finalizing this ADR. The
shape proposed above is derived from first principles + ADR-0050/
ADR-0055 patterns; if bunkhouse has a different precedent we should
adopt it. This is the **one open question** before moving the ADR
from `proposed` to `accepted`.
