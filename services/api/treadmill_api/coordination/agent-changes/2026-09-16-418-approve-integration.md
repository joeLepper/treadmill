# 2026-09-16 — ADR-0118: approve → feature-branch integration (dark/injectable)

- `coordination/dispatch_consumer.py`:
  - `integration_slug` (pure core): derive `joes-agents/<slug>` from `plan.doc_path` per the
    coordinator template §3.1a (basename, date-prefix kept, `.md` stripped). None → no branch.
    §3.1a's collision-dedup (`-<plan_id[:8]>`) is a follow-on (needs a cross-plan uniqueness
    check the pure core lacks).
  - `_maybe_integrate`: on an `approve` verdict, after the `verdict_applications` claim commits,
    if a `runner_factory` is wired AND `team_configs.merge_target == 'feature-branch'`, build the
    `MergeOp` (slug + `integration_base`) and run `integration_merger.integrate_task`
    (`git merge --no-ff` + push). `merged`/`already-integrated` → the push closes the PR and fires
    `github.pr_merged` → dependents dispatch. `conflict` → escalate (`integration_conflict`; a
    worker resolves it). infra failure → the approval row remains as the retry work-list.
  - DARK: `runner_factory` defaults to None and `make_dispatch_consumer` does not wire it, so the
    approve path records the approval but performs no git ops (the same "wired but off" shape as
    the task.ready launch). Enable-step wiring (git identity/creds + working-clone dir) and
    main-mode `gh pr merge` are deferred (operator decision).
  - Shared `_escalate` helper (the headless-verdict path now delegates to it).
- `events/task.py`: added the `integration_conflict` escalation reason.
- Verified: 4 pure `integration_slug` foils; 4 real-DB foils with a scripted runner (integrates
  into `joes-agents/<slug>` + pushes; conflict → escalation; main-mode → deferred; no-doc_path →
  deferred). Full consumer DB suite (22) + pure/predicate suites (62) green against real Postgres.
- FOLLOW-ON: main-mode integration (`gh pr merge`), the prod runner-factory wiring (creds), and
  §3.1a slug collision-dedup.
