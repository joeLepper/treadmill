# ADR-0115: Feature-branch teams self-drive integration (no CI gate, inline depends_on)

- **Status:** proposed
- **Date:** 2026-09-14
- **Amends:** ADR-0110 (agent teams integrate on a feature branch, not main)
- **Related:** ADR-0090 (API-side CI-observer / task.ci_result), ADR-0109 (team lifecycle), ADR-0113 (PR-state polling for webhookless repos), ADR-0114 (per-plan integration_base)

## Context

ADR-0110 feature-branch mode has the coordinator INTEGRATE each approved task PR into
the per-plan `joes-agents/<slug>` branch by its own `git merge + push` (§9.3). But the
coordinator's flow still routes each task through the CI gate and the event-driven
dependency resolver inherited from main-merge mode:

- §6.1/§3.5: after a worker opens a PR, the coordinator WAITS for `task.ci_result`
  (CI-green) before peer review.
- §3.3: `depends_on` dependents are dispatched only when the `github.pr_merged` EVENT
  wakes the coordinator.

The first collaborator-repo dogfood (`netlify/agent-runner-orchestrator`, ADR-0113)
surfaced that both are wrong for a feature/integration branch:

1. **CI on every finding is slow and low-value.** The dogfood's docs-only note triggered
   ≈8 build jobs. The operator's directive (2026-09-14): "we probably don't want to wait
   on CI when building an integration branch" — CI belongs on the human-owned
   branch→main PR, not on every finding landing on the integration branch.
2. **§3.5 mis-rolls-up multi-suite / skipped conclusions.** The repo emitted FOUR
   `github-actions` check-suites (its several `pull_request` workflows): 2 `success` +
   2 `skipped`. §3.5's binary `success`→review / else→rework treats `skipped` as a
   failure, and multiple github-actions suites give conflicting decisions.
3. **The observer→coordinator EVENT push did not wake the coordinator.** `task.ci_result`
   was correctly synthesized + persisted (WS connected, resolution intact) but never
   pushed to the coordinator; it stalled. Since `github.pr_merged` is task-scoped the
   same way, the `depends_on` resolver (§3.3) would stall identically at the merge step.

## Decision

We decided that in **feature-branch mode** (`merge_target = feature-branch`) the
coordinator SELF-DRIVES integration and does NOT depend on the CI gate or the
observer→coordinator event push:

1. **Gate on peer review, not CI.** After a worker opens a PR (§6.1), the coordinator
   proceeds DIRECTLY to peer review (§8) → evaluator (§9) → integrate (§9.3). It does
   NOT wait on `task.ci_result`. `task.ci_result` is INFORMATIONAL in this mode (log +
   ignore). CI is exercised on the human-owned branch→main PR.
2. **Resolve `depends_on` inline at integration.** Because the coordinator performs the
   integration itself (§9.3 `git merge + push`), it KNOWS the moment a task integrated —
   so it re-evaluates dependents INLINE right there (dispatch each now-unblocked task),
   rather than waiting for the `github.pr_merged` event/§3.3. Feature-branch mode
   therefore never depends on the (fragile) event-push wake.

Main-merge mode (`merge_target = main`, the rare permissive repo) is UNCHANGED: it keeps
the §3.5 CI gate and the §3.3 event-driven resolver.

## Alternatives considered

- **Incumbent: keep the §3.5 CI gate + §3.3 event resolver in feature-branch mode.** Why
  insufficient: it waits ≈8 build jobs per docs finding, mis-handles skipped/multi-suite
  rollups, and — load-bearing — stalls entirely when the observer→coordinator event push
  fails to wake the coordinator (observed live). The integration branch is not `main`; a
  human reviews it via the branch→main PR where CI runs, so per-finding CI is redundant.
- **Fix the event-push wake, keep the gates.** Rejected as the primary fix: it's a real
  bug worth fixing (main-merge repos still need it — filed), but it does not address the
  slow/low-value CI gate or the skipped/multi-suite mis-rollup, and it leaves
  feature-branch integration depending on a fragile push path. Self-driving is more
  robust: the coordinator already has the ground truth (it did the integration).
- **Skip CI everywhere.** Rejected: main-merge repos legitimately gate agent-merges on
  CI; only feature-branch mode (human owns the branch→main gate) can safely skip it.
- **Resolve depends_on via a poll instead of inline.** Rejected: the coordinator already
  knows it integrated the task; an inline dispatch is exact and immediate, no poll.

## Consequences

### Good
- A feature-branch plan runs at peer-review speed, not CI speed; no per-finding build wait.
- Feature-branch integration + dependency dispatch no longer depend on the
  observer→coordinator event push (both CI-wake and pr_merged-wake) — robust on
  webhookless repos (ADR-0113) where events are polled, and immune to the wake gap.
- Main-merge mode is byte-for-byte unchanged.

### Bad / trade-offs
- A finding can land on the integration branch with red CI; it surfaces at the human
  branch→main PR instead of per-finding. Acceptable: the integration branch is a
  reviewed deliverable, not `main`.
- Two flows to maintain (feature-branch self-driven vs main-merge gated), keyed on
  `merge_target`.

### Risks
- **Falsifier:** in `feature-branch` mode, a task whose PR is open + peer-approved sits
  waiting on `task.ci_result` before review, OR a `depends_on` dependent stays blocked
  after its upstream integrated until a `github.pr_merged` event arrives — either means
  the coordinator is still on the main-merge (event-gated) path, not self-driving.

## References

- `tools/team-templates/coordinator/CLAUDE.md.tmpl` §6.1 (PR open → peer review in
  feature-branch mode), §3.5 (informational in feature-branch mode), §9.3-feature-branch
  (inline depends_on resolution).
- Slice-1 findings (2026-09-14 dogfood): the observer→coordinator wake gap (filed
  separately as a must-fix for main-merge repos), the §3.5 skipped/multi-suite
  mis-rollup, and the bot-suite `queued` tail.
