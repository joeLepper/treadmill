# Session handoff — 2026-05-12 (loop hardening → first o11y smoke → teardown)

## Where we landed

The loop hardening landed and the first end-to-end smoke ran cleanly far
enough to validate both hardening pieces. The smoke surfaced one new
structural gap. Two follow-up ADRs are drafted and waiting on operator
decisions before their plans go active.

**Commits on `main` this session:**
- `213b23c` — Loop hardening impl (ADR-0025 heartbeat + ADR-0026 dispatch dedup)
- `4357bd5` — `treadmill-local redeploy` CLI (cdk deploy → down → up)
- `cd05c79` — o11y plan re-fire smoke PR #15
- `8ff414c` — VERDICT tourniquet (regex tolerates `**…**`, leading bullets, etc.)
- `ef2afa6` — ADRs 0027 + 0028 + plans (durable VERDICT fix; DB-authoritative configs)

**Current state of `personal` deployment:**
- AWS CloudFormation stack: **up** (`TreadmillPersonalCloudLite` in us-west-2).
  Cheap to keep — pennies/day; holds the IAM-key secret material.
- Local containers: **down** (`treadmill-local down`).
- SQS queues: drained to 0/0 (work, coordination, webhook-inbox).
- DB volume: persists (in-flight tasks from the smoke are still in the
  Postgres data volume; `treadmill-local up` would resume them — drain the
  DB or reset before the next smoke if that's not desired).

## Smoke result summary

PR #15 merged → plan-merge trigger fired → o11y plan submitted → 5 tasks
created → task 1 (Wire OTel SDK) ran wf-author successfully → opened PR #16
→ wf-review dispatched **once** (ADR-0026 dedup verified by the single
dedup-table row keyed on `wf-review:pr=16,sha=2d5753d`) → wf-review failed
3× with `GraphQL: Review Can not approve your own pull request` →
ADR-0025's "don't delete on error" pattern correctly let SQS redeliver
the message at the visibility timeout, capped at `maxReceiveCount=3` →
torn down before DLQ to save tokens.

What's confirmed working:
- Plan-merge-to-main auto-trigger (ADR-0021).
- Dispatch dedup keyed on `(workflow, repo, pr, sha)` (ADR-0026).
- Visibility heartbeat + don't-delete-on-error redrive (ADR-0025).
- `treadmill-local redeploy` end-to-end (cdk no-op → down → up with alembic).

## Decisions waiting on the operator

### ADR-0027 (durable VERDICT fix — structured JSON envelope)
- **Q27.a — Tourniquet retention.** What's the bar for deleting the regex
  tourniquet? Leaning: N consecutive runs without falling to the regex path.
  Concrete N?
- **Q27.b — Rationale length cap.** Pydantic `max_length=2000` on
  `rationale`, or uncapped?
- **Q27.c — Strip-the-fence visibility.** When the handler strips the JSON
  block from the PR review body, leave a marker ("Verdict parsed from
  structured block") or invisible? Leaning invisible.
- **Q27.d — Dry-run JSON parsing.** Parse-and-log on dry-run too (for drift
  signal), or skip entirely? Leaning always-parse-always-log.

### ADR-0028 (DB-authoritative workflow/role configs)
- **Q28.a — Fresh-deployment bootstrap.** Manual `seed-starters`, auto-seed
  on first API startup, or alembic data migration? Leaning auto-seed (best
  ergonomics; needs a SELECT-FOR-UPDATE sentinel for multi-replica safety).
- **Q28.b — v1 CLI surface.** Minimum is `role show` + `role update`. Add
  `role versions` (history) and `role rollback`?
- **Q28.c — `starters.py`'s long-term home.** Keep in repo / move to
  `infra/bootstrap/` / move to a pinned fixtures package? Leaning keep.
- **Q28.d — Audit trail.** Add `notes` / `pr_url` columns to `role_versions`?
  Leaning yes.
- **Q28.e — Scope.** Roles only, or workflows + workflow_versions too?
  Leaning roles-only (workflow shape changes deserve an ADR + code review).

### Task #108 (dual-identity / same-author review block)
Live design ladder (Joe's framing, captured in
[memory/project_dual_identity_gap.md](../../.claude/projects/-home-joe-treadmill/memory/project_dual_identity_gap.md)):

1. **Bunkhouse precedent — `gh pr comment` instead of `gh pr review`.**
   Cheapest. Loses the formal review state on the PR page but the
   mergeability VIEW uses Treadmill's own decision strings, so likely Just
   Works. Verify before adopting.
2. **Second PAT identity for the reviewer role.** Adds an identity to
   manage. Operator is lukewarm.
3. **Treadmill becomes a GitHub App.** Right long-term answer; substantial
   work. Defer until forcing function.

Leaning (1) for the first cut.

## Live threads (in priority order when you're back)

1. **Resolve the open Qs above.** Pedantry now is the cheapest it will ever
   be (per [[feedback_phase_closure]]).
2. **Flip ADR-0027 + ADR-0028 plans from `status: drafting` → `status: active`**
   once Qs are resolved. Re-merge fires the plan-merge trigger and Treadmill
   picks them up — once #108 lands.
3. **Land task #108's chosen approach** (likely path 1 — comment-only
   reviewer). Until this lands, every smoke past wf-author fails at wf-review.
4. **Then re-run the o11y plan as the smoke** — the same pattern as PR #15,
   with the dual-identity fix + structured JSON envelope in place. Expected
   to land all 5 tasks end-to-end this time.

## Resume commands

```bash
# Bring the local stack back up against the same AWS deployment:
cd /home/joe/treadmill/tools/local-adapter
uv run treadmill-local up --deployment personal

# OR, if the operator has merged anything that touched code:
uv run treadmill-local redeploy --deployment personal

# Reset the DB volume first if you want a clean smoke (drops the prior
# smoke's plans/tasks/runs):
docker volume rm treadmill-local_treadmill-postgres-data 2>/dev/null || true
# (volume name may differ — check `docker volume ls | grep postgres`)
```

## Pending tasks at handoff

- `#95` Bootstrap non-Treadmilled repos
- `#96` Learnings-to-validations pipeline
- `#98` Observability stack (ADR-0020 phases 3–7) — the o11y plan itself
- `#101` Ralph-loop validation architecture
- `#103` Structured step-output parsing ADR
- `#104` API credentials: long-lived IAM-User keys (ADR-0023 drafted)
- `#107` `docs/runbooks/` reorganization
- `#108` Dual-identity for bot PRs (this session's surfacing)

Plus the two ADRs landed today (0027, 0028) whose plans are gated on Qs.
