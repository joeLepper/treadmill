# ADR-0045: Alembic head-multiplicity is a pre-merge CI gate

- **Status:** proposed
- **Date:** 2026-05-17
- **Related:** ADR-0044 (datetime-keyed migration revision IDs), incident captured in PR #139 (scheduler-migration-collision)

## Context

ADR-0044 adopts datetime-keyed Alembic revision IDs (`YYYYMMDD_HHMM`) to make migration revision collisions structurally unlikely. The format is unique by construction across concurrent PR authoring — two agents incrementing from the same baseline cannot land on the same ID — but it does not eliminate one residual class of failure: an operator (human or LLM) copy-pastes an existing revision ID into a new file. Datetime is no defense against deliberate or accidental duplication.

The deeper observation: each PR's CI today runs against its own branch in isolation. PR A and PR B can each pass `alembic upgrade head` against a fresh DB because, in isolation, each sees a clean single-head chain. The collision only manifests when both PRs sit in `main` together. Author-side validation cannot detect this class.

GitHub Actions, when triggered on a `pull_request` event, exposes a `refs/pull/N/merge` ref that represents the would-be-merged tree (PR HEAD merged onto current main). CI run against this ref sees the *post-merge* state of the migrations directory, including any new file the PR adds. Running `alembic heads` against that state catches the collision before merge.

## Decision

Every PR's CI runs `alembic heads` against the merge-ref tree. The job fails when `alembic heads` returns more than one line, blocking merge. The check lives in `.github/workflows/ci.yml` as a new step under the `services/api` job, after `uv sync` and before `pytest`.

The check is a one-liner: `test $(uv run alembic heads | wc -l) -eq 1`. Output on failure surfaces the conflicting heads so the PR author sees which revision IDs collide.

## Alternatives considered

- **Trust ADR-0044's datetime format alone** — Rejected: defends against concurrent-author collisions but not against copy-paste of an existing revision ID. The cost of the CI gate is ~2 seconds; the cost of another 24h scheduler-style outage is much higher.
- **Post-merge gate on `main`** — Rejected: would alarm after collisions land. Useful as a monitor of last resort but does not prevent the failure. The pre-merge gate dominates.
- **A linter / pre-commit hook on the developer's machine** — Rejected: bypassable, not enforceable across agents and operators. Hooks help authors but cannot be a gate.
- **Author-side validation in the plan's validation script** — Rejected: the script runs against the author's local branch, not the merge ref. Doesn't see other in-flight PRs.
- **Run `alembic upgrade head` rather than `alembic heads`** — Considered, kept as additional check: upgrade catches the same class of failures plus runtime errors in migration code. We will run both — `heads` for fast head-count diagnosis, `upgrade head` against a clean DB for full execution. (Migration tests already do this; ensure `alembic heads` is the named gate so error messages point clearly at the head-multiplicity case.)

## Consequences

### Good
- Closes the last residual failure class for migration revision IDs.
- Fast: `alembic heads` runs in ~1 second; total CI overhead is negligible.
- Surfaces the failure with a clear error message that names the conflicting revision IDs.

### Bad / trade-offs
- One more CI step to maintain. Trivial.
- A PR that's been sitting open while main accumulates new migrations may suddenly fail CI when it's about to merge — same UX as any other rebase-required check. Acceptable.

### Risks
- The check only runs in CI; an operator with branch-protection bypass could still land a colliding migration. Mitigation: branch protection requires the check.
- If `alembic heads` itself is slow on a corpus of 10,000+ migrations, the gate could become noticeable. Mitigation: Alembic's revision graph is in-memory; head computation is O(N) but N=18 today; doesn't matter for years.

## References

- ADR-0044 §Risks (the residual failure class this ADR closes)
- PR #139 — the incident that motivated both ADRs
