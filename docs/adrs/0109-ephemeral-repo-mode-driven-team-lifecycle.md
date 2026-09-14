# ADR-0109: Ephemeral, repo-mode-driven team lifecycle

- **Status:** accepted (2026-09-13; cross-model panel review — findings folded — + sibling co-sign by Ernie across two rounds, verified in-text)
- **Date:** 2026-09-13
- **Related:** ADR-0087 (team execution model), ADR-0108 (panel-backed evaluator), ADR-0110 (feature-branch integration — the merge-target mode + the "implemented" trigger), ADR-0018 (retired container autoscaler)

## Context

A team (coordinator + evaluator + N workers) is stood up by `treadmill team up`
(`tools/team-templates/install.py` renders each role's `CLAUDE.md` + systemd units)
and then runs PERSISTENTLY. On 2026-09-13 five repos had teams up — 25 sessions
(5 coordinators, 5 evaluators, 15 workers) — all idle, burning Claude session slots
and host load for no work. There is no `team down` primitive, no per-repo lifecycle
policy, and no automation to match team liveness to whether there is a plan to work.
The ADR-0018 autoscaler (ephemeral Docker containers) is retired; the substrate is now
long-lived named sessions. (Worker MODEL diversity — cross-model workers — is a
separate, capacity-gated decision in its own ADR; this ADR is only the lifecycle.)

## Decision

We adopt a **team lifecycle manager** over the ADR-0087 primitives (operator-directed,
2026-09-13):

- **`team down <slug>`** — the missing primitive: stop + disable EVERY member's runtime
  (the coordinator/evaluator/worker systemd units) and keep the rendered dir
  (revivable). (When cross-model workers land as their own ADR, `team down` must also
  reach their non-systemd runtime — that is stated there, not here.)
- **Drain-guard (DEFINED here, not deferred — it is the load-bearing safety
  invariant).** Teardown is safe only when, for EVERY plan targeting the repo, NO task
  is in a non-terminal state. The blocking set spans the full ADR-0087 state machine:
  `registered | assigned | executing | pr_open | rework-pending | escalated`, AND —
  critically — **post-merge observation**: a task whose PR merged but whose deploy /
  `staging_smoke` observation is not yet settled (ADR-0087 §3.7) is still in-flight,
  because the coordinator owns the rollback task; `merged` is NOT terminal. Terminal =
  `merged` AND post-merge observation complete, OR a terminal failure. The guard also
  refuses on any open PR authored by any team member — CI-running is covered by the
  open PR. If anything is in-flight, `team down` refuses (manual) or waits (auto). The
  guarantee "teardown never abandons in-flight work" is only as strong as this set, so
  the set is pinned here.
- **Per-repo mode** (`ephemeral` default | `persistent` | `manual`), in a per-repo team
  config:
  - `ephemeral`: stand the team up on the first `plan.submitted` for a repo with no
    live team; tear it down once EVERY task of every plan targeting the repo reaches a
    TERMINAL state — `merged` (into `main` OR, on a `feature-branch`-mode repo, the
    integration branch per ADR-0110 = the plan "implemented") OR a terminal failure
    (`terminal_step_failure`, `cancelled`, `gate_broken`, cap/amend-exhausted) — so a
    failed plan does not pin the team up forever, after a short idle-grace (anti-thrash).
  - **Standup and teardown are serialized behind an ATOMIC per-repo lease (no
    check-then-act race).** Two `plan.submitted` for the same repo arriving together
    must NOT both read "no live team" and stand up two coordinators — that is a
    single-writer-invariant violation + WS owner-cache corruption, not just waste.
    Standup claims an atomic per-repo lease (a unique DB row keyed on repo-slug, or an
    atomic directory/unit create) so the second submit joins the standing team. The
    same lease serializes teardown: a `plan.submitted` during idle-grace or
    teardown-in-progress cancels the teardown (or is admitted before units stop), and
    after any teardown the manager RECONCILES — re-stands-up if an active plan exists.
    An `ephemeral` submission must never be left without a team, and a repo must never
    have two teams.
  - `persistent`: never auto-down (hot repos).
  - `manual`: up/down only by explicit command (still drain-guarded).
- **Cheap workers.** Workers run on the cheap frontline tier (model tiering: sonnet
  workers; the panel-backed evaluator supplies the review rigor). CROSS-MODEL workers
  (Codex-harness workers on GPT / open-weight) are a SEPARATE decision — split to their
  own ADR in the capacity-gated cross-model track (with the Codex-sibling migration) —
  because a code-WRITING non-Claude worker is a materially larger trust boundary than
  the Gerald/Fran REVIEW peers and is orthogonal to the lifecycle. The lifecycle here is
  model-agnostic: it manages whatever workers a team spec declares.
- The evaluator stays panel-backed (ADR-0108) and bounces `rework` to the coordinator.

## Alternatives considered

- **Incumbent: persistent teams + manual `team up`, no teardown.** Why insufficient:
  25 idle sessions burn resources with no work; there is no lifecycle at all.
- **Revive the ADR-0018 container autoscaler.** Rejected: wrong substrate (Docker
  containers, not the named sessions teams now use) and already retired.
- **Keep persistent; add only a manual `team down`.** Rejected: saves little without
  the ephemeral automation; the operator would micromanage every team's lifecycle.
- **Bundling cross-model workers into this ADR.** Rejected: it is an orthogonal,
  higher-risk decision (a code-writing non-Claude worker's trust boundary) that would
  make this ADR decide two things at once; it is split to its own ADR in the
  capacity-gated cross-model track (operator + review, 2026-09-13).

## Consequences

### Good
- Large resource savings — idle teams no longer run; liveness tracks work.
- A fresh standup always renders the LATEST templates (e.g. the panel evaluator) with
  no restart dance.
- Model-agnostic: the cross-model-worker track can layer on later without changing the
  lifecycle mechanism.

### Bad / trade-offs
- Standup latency on the first plan for an ephemeral repo (mitigated: fast render;
  persistent mode for latency-sensitive repos).
- Thrash risk if plans arrive in bursts (mitigated: idle-grace before teardown).

### Risks
- **Falsifier:** a team is torn down while any of its plans has a task in a
  non-terminal state per the drain-guard set — including a `registered`/queued task, a
  dispatched-but-unstarted rework, or a merged task still under post-merge deploy/smoke
  observation — or an open PR it was driving (work lost / no coordinator to roll back);
  OR two teams (two coordinators) are stood up for one repo (single-writer violation);
  OR an `ephemeral` repo receives a `plan.submitted` and no team is stood up (work
  stalls).

## Follow-ups

- The exact WATCHER that drives auto-standup/teardown (a lifecycle daemon vs a
  coordinator-adjacent hook vs a periodic reconcile) — the Decision pins the semantics
  (serialized admission/teardown, terminal-state trigger, reconcile); the plan picks
  the mechanism. Whatever it is must make the admission/teardown transition atomic per
  repo so the drop race cannot occur.
- Idle-grace duration and per-repo overrides (tuning, not a new decision).

(The drain-guard's in-flight set is DEFINED in the Decision, not a follow-up.
Cross-model workers are a SEPARATE ADR — not part of this lifecycle decision.)

## References

- tools/team-templates/ (`install.py`, role templates) — the standup primitive.
- ADR-0108 (panel-backed evaluator), ADR-0104/0102 (Gerald/Fran Codex-on-bus substrate).
