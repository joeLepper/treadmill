# ADR-0112: Event-driven team teardown by the exec-in-charge; reconcile is a backstop

- **Status:** accepted
- **Date:** 2026-09-14
- **Amends:** ADR-0109 (ephemeral, repo-mode-driven team lifecycle)
- **Related:** ADR-0110 (feature-branch integration — the `plan.handoff_pr_opened` done-signal), ADR-0087 (team execution model), exec-in-charge skill

## Context

ADR-0109 made teams ephemeral and defined a "team lifecycle manager." The v1
implementation realized teardown as a periodic `team reconcile` (a level-triggered
loop on a 2-minute systemd timer) that tears a team down once its drain-guard is
clean and it has been idle beyond a grace (defaulted to 6 hours). Two facts make
that the wrong PRIMARY mechanism for teardown:

- **"Done" is deterministic.** The coordinator records `plan.handoff_pr_opened`
  (ADR-0110) at the exact instant a feature-branch plan is implemented; a terminal
  failure is equally knowable. No polling is needed to *detect* done.
- **A polling teardown keyed on a 6-hour idle grace keeps a finished team up for
  hours** — closer to the 25-idle-teams incident ADR-0109 exists to prevent than it
  should be.

The one hard constraint is why teardown cannot simply be "the coordinator tears
itself down": the coordinator is a *member* of the team, so stopping the team's
units kills the coordinator mid-teardown. Teardown of the whole team must be
performed by an actor OUTSIDE the team.

The **exec-in-charge** — the submitting orchestrator (`plans.created_by`) — is
exactly that actor: external to the worker team, already event-subscribed, and
already the executive responsible for driving the plan to done (the exec-in-charge
skill). It can run `treadmill team down` on the team without self-killing.

## Decision

We made team teardown **event-driven and owned by the exec-in-charge**, with the
reconcile demoted to a backstop (operator-directed, 2026-09-14):

- The coordinator, on reaching "implemented" (§9.7: handoff opened + recorded +
  surfaced) or on a terminal-failure escalation, **relays a "team safe to tear
  down" signal to `plans.created_by`** over the same channel §10 escalations use.
- **The teardown actor is external to the team it tears down — by invariant, and
  enforced in depth.** `plans.created_by` is ALWAYS the submitting orchestrator
  (`treadmill-alan`, `-bert`, …); per the project's CLAUDE.md it "is NOT set to the
  coordinator label" — only orchestrators submit plans, and orchestrators are never
  team members. So the exec-in-charge is external BY CONSTRUCTION; this is not luck.
  We still enforce externality in DEPTH — against the realistic misconfiguration where
  `created_by` resolves to a session whose real label IS a team member, or a future
  change that weakens the invariant — at TWO layers: (a) the actor self-checks — its
  own `TREADMILL_LABEL` must not be in the team's full label set (coordinator,
  evaluator, AND all workers, read from `team_configs` — not a hand-enumerated role
  prefix, which drops the evaluator); and (b) `team down` ITSELF refuses when its
  invoking `TREADMILL_LABEL` is a team member — testing against the SAME full
  `team_configs` label set as layer (a) (coordinator + evaluator + all workers,
  `_all_team_labels`), NOT a hand-enumerated role prefix, so the evaluator cannot be
  dropped at either layer. SCOPE: both layers trust the launcher-set `TREADMILL_LABEL`
  as the actor's identity. A session that FORGES its own label (claims an orchestrator
  label while actually being a team member) defeats any label-based guard — that is a
  substrate trust-boundary concern (the launcher owns the label; a process that
  overrides it can bypass far more than this), not something a teardown guard can
  catch. GIVEN A CORRECT LABEL, the tool-side guard (b) means a buggy or omitted agent
  self-check (a) cannot cause a self-kill. `--force`
  overrides the drain-guard, never this self-kill guard.
  A member simply does NOT run `team down`; it takes no other action, and the backstop
  reconcile (external by construction — a systemd-timer process, not a team session)
  reaps the team on its next tick. This is a PASSIVE fallback (tick-bounded), not an
  active handoff — consistent with the diagram's else-branch; there is no immediate
  nudge, and none is needed because a genuinely-external `created_by` (the invariant)
  makes this branch a rare defense-in-depth path, not the normal one.
- The **external exec-in-charge tears the team down** in response — running
  `treadmill team down <repo>`, which is drain-guarded, so a team that is not actually
  done is refused. This is the PRIMARY teardown path: external actor, deterministic,
  prompt. The drain-check and unit shutdown inside `team down` are NOT one atomic
  transaction, so a plan.submitted that lands in the gap can leave a just-registered
  task on a team whose units are stopping. This is NOT a permanent strand: `team down`
  disables the COORDINATOR unit FIRST, and the reconcile keys team liveness on the
  coordinator unit — so ANY teardown that has begun (even a partial one that failed
  after stopping the coordinator but before a worker) reads as not-live, and the
  backstop revives a not-live team that has work (re-enabling every unit). The residual
  exposure is a worker's UNCOMMITTED work during the ~seconds shutdown, bounded by the
  same commit/push-before-terminal durability the ADR-0109 parked-on-human path already
  requires. `team down` re-evaluates the drain as late as possible before the stop, to
  keep the window minimal.
- **Teardown and the reconcile pass are SERIALIZED on a host lock** so a revive can
  never INTERLEAVE a teardown. Without it, a reconcile tick that revives (re-enables
  every unit) partway through a `team down` — after the coordinator is stopped but
  before a worker — could resume the teardown and stop workers under a now-live
  coordinator, a broken coordinator-up/workers-down state that coordinator-liveness
  reads as live and never repairs. Both `team down` and `team reconcile` take the same
  host `flock`: `team down` blocks (waits for the in-flight reconcile pass, then tears
  down — never skipping), the reconcile skips a tick it cannot acquire. So teardown and
  revive run to completion one at a time, never overlapped. This is a HOST-LOCAL guard,
  which is sufficient because a team and its lifecycle actors run on ONE operator host
  (all `treadmill-channel@*` units and the reconcile timer are `systemctl --user` on
  that host); a future multi-host substrate would replace the flock with a per-repo DB
  lock (the atomic lease of ADR-0109 step 4a is the natural anchor). The reconcile
  BACKSTOP still ticks on its 2-minute timer (ADR-0109 unchanged) — only its teardown
  GRACE was shortened (to 0.5h); so an orphaned task from the TOCTOU window is revived
  within one tick (~2 min), the "~seconds" being the shutdown window itself.
- The **`team reconcile` timer is the BACKSTOP only** — a low-frequency safety net
  (not a 2-minute primary loop) that tears down a team the responsible agent did not
  (the exec-in-charge was down, busy, or the coordinator crashed before signaling),
  and that revives a down team with pending work (a role no down team can perform
  for itself). Its teardown grace is shortened so even the backstop does not keep a
  finished team idle for hours.

Standup and crash-revival remain the reconcile's job (a down team cannot act on its
own behalf); this ADR changes only the PRIMARY teardown path.

## Alternatives considered

- **Incumbent: the `team reconcile` timer as the primary teardown mechanism (v1).**
  Why insufficient: it polls for a state the coordinator already knows deterministically,
  and its idle-grace keeps a finished team up for hours — latency and waste for an
  event that is precisely observable.
- **Coordinator tears down its own team.** Rejected: the coordinator is a member of
  the team; stopping the team's units kills it mid-teardown. An external actor is
  mandatory.
- **Event-driven teardown with NO backstop.** Rejected: the exec-in-charge is a
  Claude session — it can be busy, miss the signal, or be down. Teardown that depends
  only on it silently recreates the idle-team problem. The reconcile backstop is the
  mechanical safety net behind the responsible party.

## Consequences

### Good
- Finished teams tear down promptly on the deterministic done-signal, not after a
  multi-hour idle timer — the resource goal, achieved without polling latency.
- The responsible party (the exec-in-charge that commissioned the plan) owns the
  outcome end to end, consistent with the exec-in-charge skill.

### Bad / trade-offs
- Teardown now depends on a live, correctly-behaving exec-in-charge for the fast path
  (externality is guaranteed by the created_by invariant, not a runtime dependency);
  the backstop covers its absence (down or busy) at the backstop's slower cadence.
- Two actors can now initiate teardown (exec-in-charge + backstop reconcile). A
  double-fire is safe because `team down` is drain-guarded AND a no-op on an
  already-down team (`systemctl disable --now` on an inactive unit is a no-op; the
  drain-guard passes). If a new plan arrived between the two fires, the drain-guard
  refuses the second — the guard, not the actor count, is the safety.
- The `team down` drain-check and shutdown are not one atomic transaction (see the
  Decision's TOCTOU note): a plan.submitted in the gap is not permanently stranded
  (the backstop revives it) but a worker's uncommitted work in the shutdown window is
  exposed — mitigated by commit/push-before-terminal durability, not eliminated.
- The primary relay is one-shot: on a multi-plan team, plan A's teardown signal is
  refused while plan B is in flight and is not re-queued. Teardown re-fires on plan
  B's own completion (its §9.7 signal) or, failing that, the backstop reaps it — so a
  multi-plan team leans more on plan-B's signal and the backstop.

### Risks
- **Falsifier:** a plan reaches `plan.handoff_pr_opened`; the team has NO other
  in-flight work (its drain is clean, so it is teardown-ELIGIBLE); its `created_by`
  exec-in-charge is external to the team and idle (not mid-task) — yet the team's
  units are still `active` **10 minutes** later, well inside the backstop's **30-minute**
  grace so the backstop has not yet fired. That is the event-driven primary path
  failing to fire. Scope matters: a team kept up because a late plan left work
  (drain not-clean) is the drain-guard working CORRECTLY, not a falsifier hit; and an
  exec-in-charge that is alive-but-busy is covered by the backstop, not a failure of
  the design. Equally falsifying: idle finished teams accumulate again across many
  plans despite live external exec-in-charge sessions.

## Diagram

```mermaid
sequenceDiagram
    actor Coordinator
    actor ExecInCharge
    participant Team as Worker team units
    participant Reconcile as team reconcile (backstop)
    alt plan implemented
        Coordinator->>Coordinator: §9.7 handoff recorded
    else plan terminally failed
        Coordinator->>ExecInCharge: escalate (§10) + teardown-candidate note
    end
    Coordinator->>ExecInCharge: relay "team safe to tear down" (plan_id, repo)
    ExecInCharge->>ExecInCharge: verify I am EXTERNAL to the team
    alt external (normal)
        ExecInCharge->>Team: treadmill team down (drain-guarded)
        Note over ExecInCharge,Team: primary path — external, deterministic, prompt
    else exec-in-charge is a team member (self-submitted) / down / missed
        Reconcile->>Team: team down on next backstop tick (external by construction)
    end
```

## Follow-ups

- **Durable server-routed done-signal (robustness upgrade).** The primary trigger is
  today the coordinator's soft relay to `created_by` (agent-initiated; the backstop
  covers a missed relay). To make the fast path prompt-BY-CONSTRUCTION, a server-side
  relay-drop to `~/.cc-channels/<created_by>/relay/` on `plan.handoff_pr_opened` would
  remove the dependence on the coordinator also sending a separate relay. Deferred —
  and it is NOT the ~20-line mirror of the task-relay pattern it first appears: the
  `ArchitectEmitFailure` relay trigger the docstrings name
  (`maybe_drop_relay_on_architect_emit_failure`) has no implementation in the tree,
  and broadcast fan-out addresses channel SUBSCRIBERS, not an arbitrary `created_by`
  label. So a durable per-event relay-drop keyed on a payload label needs its
  delivery plumbing ESTABLISHED and verified first, not merely a new field. The soft
  relay + backstop deliver safely in the meantime.

## References

- ADR-0109 (the lifecycle this amends), ADR-0110 (the `plan.handoff_pr_opened`
  done-signal), the exec-in-charge skill, coordinator template §9.7 + §10.
- Treadmill CLAUDE.md §`created_by` field — "The `created_by` field on a plan is the
  orchestrator session label that submitted the plan … It is NOT set to the
  coordinator label." This is the invariant that makes `plans.created_by` external to
  the worker team by construction; the self-kill guards are defense-in-depth for a
  violation of it.
