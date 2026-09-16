# 2026-09-16 — ADR-0118: integration sweep + slug sanitization (enable-hardening)

The two enable-step hardening items Bert homed at #418, built dark.

- `coordination/dispatch_consumer.py`:
  - `integration_sweep`: the retry backstop for approve→integration (analog of the dispatch
    reconcile sweep). Re-drives every approved-but-not-integrated router task
    (`verdict_applications.decision='approve'` with no `github.pr_merged`); `integrate_task` is
    idempotent (ancestry no-op → `already-integrated`). Runs each reconcile tick; SKIPPED when
    dark (no `runner_factory`). Closes the "approval remains for retry was aspirational" gap.
  - `is_valid_ref_component` (pure): rejects a slug that is not a legal git ref component
    (space, `~ ^ : ? * [ \`, `..`, `@{`, leading/trailing dot, `.lock`). `_maybe_integrate`
    escalates (reason `integration_blocked`) on an invalid slug instead of looping on a
    perpetual infra-fail.
- `events/task.py`: added the `integration_blocked` escalation reason.
- Verified: 6 pure foils (git-ref validation + slug); 3 real-DB foils (sweep re-drives a
  stranded approval; skips an already-merged task; invalid slug escalates, never attempts the
  branch). Consumer DB suite (25) green against real Postgres. No migration; still dark.
- NEXT: prod runner-factory wiring (SubprocessGitRunner + ambient fleet creds + state dir) +
  main-mode `gh pr merge`, then enable on a pilot plan.
