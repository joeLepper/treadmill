# ADR-0028 — Workflow / role / version configs live in the DB; code is bootstrap-only

* Status: Proposed (drafted 2026-05-12)
* Trigger: Joe, 2026-05-12 — "we ran into this all the damn time in
  bunkhouse. There's this double-source of truth for role, workflow,
  etc configs. Ideally we get this into the DB and then take the
  seed prompts out of the repo so that we stop thinking we can
  change the code and see it get updated and stop forgetting to
  push what's in code into the db."
* Related: ADR-0015 (multi-step workflows + role reuse, the spec
  that `starters.py` enforces today), ADR-0027 (structured JSON for
  review — depends on the role-reviewer prompt edit, which under
  this ADR is no longer a code change)

## Context

`services/api/treadmill_api/starters.py` is a Python module that
holds the canonical roles + workflows + versions as plain dicts.
`treadmill workflows seed-starters` POSTs them to the API's CRUD
endpoints with 409-on-conflict as the idempotency mechanism. The
DB is the runtime source of truth — the consumer + worker read
prompts from `roles.system_prompt`, not from `starters.py` — but
the **operator's edit workflow** has the source-of-truth in the
code:

1. Edit a prompt in `starters.py`.
2. Push the code.
3. Forget to re-run `seed-starters`.
4. Runtime keeps using the previous prompt; behavior doesn't change.
5. Spend 30+ minutes debugging "why is the model not following the
   instruction I just added."

This is the Bunkhouse failure mode the operator has explicitly
flagged as the pattern to invert. The 409-swallow in `seed()` makes
the bug *worse*: re-running the seed command does **not** update an
existing role's prompt, so even when the operator remembers to
re-seed, the change still doesn't land — they have to do a manual
PUT or delete-and-recreate. (Confirmed via reading `starters.py:489`
and the comment "swallowing 409s so re-runs are idempotent.")

## Decision

**The DB is authoritative for roles, workflows, and workflow
versions. The code-side definitions exist only to seed a fresh
deployment.**

Concretely:

1. **`starters.py` becomes a bootstrap fixture, not a spec.** It
   keeps its canonical dicts as the seed content for a brand-new
   deployment (so `treadmill workflows seed-starters` against an
   empty DB still works). After first seed, code edits to
   `starters.py` are **inert** with respect to running deployments
   — they only affect the next greenfield bootstrap.

2. **Operator edits happen via API/CLI, against the DB.** A
   `treadmill role update <id>` subcommand reads a prompt file
   from disk and PUTs it to the role endpoint, bumping a new
   `role_version` row. The CLI subcommand is the supported edit
   path; the API endpoint is the supported integration path. No
   one should ever edit `starters.py` post-bootstrap to change
   runtime behavior.

3. **Audit trail comes from `role_versions` row history.** Today's
   schema already versions roles; this ADR codifies that
   `role_versions.created_at` + `created_by` are the
   what-changed-when ledger. No separate audit log needed.

4. **`seed-starters` becomes idempotent for the *missing* case
   only.** Today it 409-swallows. The new behavior:
   - Missing roles/workflows are seeded.
   - Present roles/workflows are **untouched** (existing behavior).
   - Optional `--reset-prompts-from-code` flag explicitly opts into
     "overwrite DB with code values" — only for the recovery case
     where the operator has fubarred the DB and wants the
     bootstrap shape back. Off by default; loud confirmation on.

## Consequences

* The "edit code, forget to re-seed" failure mode disappears
  because editing code no longer changes runtime behavior, and the
  operator's reflex moves to the CLI command instead.
* Prompt edits are reviewable via DB-row history rather than git
  diffs. This is a tradeoff: git diffs are richer (commit messages,
  PR comments, ADR links), DB rows are authoritative. For high-stakes
  prompt changes, the operator can still link to a PR in the
  `role_versions.notes` column (if we add one).
* CI / fresh-deployment tests need to seed against the canonical
  bootstrap shape — which is still `starters.py`'s content. The
  shape invariants in `test_starters.py` keep their value as a
  spec for the bootstrap content, even after `starters.py` itself
  goes inert for prod.
* Prompt rollback is a CLI operation (`treadmill role rollback <id>
  --to-version <n>`) rather than a `git revert`. Probably wanted as
  a v1 affordance; punted for now.
* ADR-0027's prompt rewrite stops being a code change → seed
  dance — it becomes a single CLI invocation against the deployed
  API.

## Alternatives considered

### A. Keep `starters.py` as the source of truth; fix `seed()` to overwrite
Cheaper change. Rejected because the operator's reflex problem
remains: they still have to remember to run `seed-starters` after
every code change. Doesn't address the failure mode at all — just
makes the seed command less broken.

### B. Filesystem-watched prompt files (`infra/prompts/role-reviewer.md`)
A daemon watches a directory and PUTs changes to the API. Closer to
the "DB is source of truth" world but adds a new file-watching
component and an interpretation step (frontmatter parsing). Rejected
as more moving parts than the CLI approach buys back.

### C. UI-first edit interface
Web UI for prompt editing. Right end state for v1.5+ once we have
operator-grade UI; not the v1 move. CLI is the right primitive to
build first — a UI can wrap it later.

## Open Qs (for operator review)

* **Q28.a — How does a fresh deployment bootstrap?**
  Options:
  * (i) Operator runs `treadmill workflows seed-starters` after
    `treadmill-local up` — current path, unchanged. Manual step but
    explicit.
  * (ii) API runs `seed_starters_if_empty()` on startup — auto on a
    fresh DB, silent on an existing one. Tied to the alembic-on-startup
    pattern in `services/api/treadmill_api/cli.py`. Slight risk of
    racing if two API replicas come up against a fresh DB; benign
    today (one-replica dev-local), worth flagging for the future.
  * (iii) Seed via an alembic data migration — version-pinned, but
    awkward to update since alembic migrations don't get edited
    after they ship.
  Leaning (ii) for the operator-ergonomics win; (i) is the
  conservative fallback.

* **Q28.b — Edit interface for v1.**
  CLI subcommands needed:
  * `treadmill role show <id>` (current prompt).
  * `treadmill role update <id> --prompt-from-file <path>` (PUT new
    version).
  * `treadmill role versions <id>` (history).
  * `treadmill role rollback <id> --to-version <n>`?
  * Equivalent commands for workflows + workflow versions?
  Scope of v1 unclear. Minimum viable is `show` + `update`; the
  others are quality-of-life.

* **Q28.c — `starters.py`'s long-term fate.**
  * (i) Keep in repo indefinitely as the bootstrap fixture +
    invariant spec.
  * (ii) Move to `infra/bootstrap/starters.py` to signal its
    "fresh deploy only" status.
  * (iii) Move to a separate fixtures package, version-pinned, so
    the repo dev workflow doesn't surface it at all.
  Leaning (i) — it's small, the invariants in `test_starters.py`
  are valuable spec coverage, and the rename buys little.

* **Q28.d — Prompt edit audit trail.**
  Do `role_versions` rows carry an optional `notes` / `pr_url`
  column for the operator to link a prompt change back to a PR or
  incident? Useful for explainability when the loop misbehaves;
  cheap to add. Leaning yes.

* **Q28.e — Scope: just roles, or also workflows + versions?**
  Roles' `system_prompt` is the obvious mutable surface. Workflow
  shape (the step list, the role refs) is less commonly tweaked but
  also lives in `starters.py`. Are workflow edits also operator-
  initiated via the CLI, or are workflow-shape changes always a
  code change requiring an ADR + re-seed? Leaning "workflows stay
  code-driven; only role prompts go DB-authoritative" — workflow
  shape is rarer and higher-stakes, and an ADR review is the right
  forcing function.

## Phasing

Sketched here; the durable plan lives at
`docs/plans/2026-05-13-db-authoritative-configs.md`.

1. **Decide the open Qs.** Cheap up-front pedantry per
   [[feedback_phase_closure]].
2. **Fix `seed()`'s 409 behavior** — make the no-op behavior
   explicit, gate the overwrite behind `--reset-prompts-from-code`.
3. **Wire the API endpoint(s)** if the role PUT path doesn't yet
   exist for prompt-only updates. (It may; need to audit.)
4. **CLI subcommands** per Q28.b's resolution.
5. **Auto-seed on fresh DB** per Q28.a's resolution, if (ii).
6. **Update `test_starters.py`** to reflect the spec-only role of
   `starters.py` post-bootstrap.
7. **Operator-facing doc** under `docs/runbooks/` explaining the
   new edit workflow. (Folds into task #107.)
