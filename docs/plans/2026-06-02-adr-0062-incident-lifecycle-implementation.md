# Plan: ADR-0062 — operator escalations as incidents with MTTR + notification fan-out (implementation)

- **Status:** drafting
- **Date:** 2026-06-02
- **Related ADRs:** ADR-0062 (the decision, amended 2026-05-29 to
  cover the terminal-step-failure producer), ADR-0048 (existing
  `task.escalated_to_operator` event), ADR-0035 (scheduler — used
  by the close-detection sweep), ADR-0056 (operator dashboard —
  the existing rendering surface)

## Goal

Implement the four-part contract from ADR-0062: complete the
producer taxonomy (every terminal-failure path opens an incident),
add the close-event + MTTR lifecycle, build the operator surfaces
(CLI tail / list / close / ack / report; Slack notifier with
pluggable webhook fan-out), and update the dashboard to render
MTTR. Step 1 — the terminal-step-failure producer — ships first;
it would have caught both of this session's silent stalls on its
own.

## Success criteria

1. `task.escalated_to_operator` fires from a sixth producer site
   (`terminal_step_failure`) whenever a `step.failed` event reaches
   a terminal workflow state without a cap-reached producer
   already firing in the last 5 minutes. Dedup is verified by
   tests.
2. A new `task.escalation_closed` event records incident
   resolution with `close_reason ∈ {re_progressed, pr_merged,
   cancelled, superseded, operator_close}` and `mttr_seconds`.
   A `*/2` scheduled sweep emits it on the five close triggers.
3. `treadmill escalations` CLI group surfaces `tail`, `list`,
   `close`, `ack`, and `report` subcommands against new
   `/api/v1/escalations/*` endpoints. `tail` long-polls a
   streaming endpoint (≤5s latency).
4. A new in-process notification subscriber posts open + close
   events to a Slack webhook (env: `TREADMILL_SLACK_WEBHOOK_URL`)
   AND to a list of generic webhooks (env:
   `TREADMILL_NOTIFICATION_WEBHOOKS`, comma-separated). Failure of
   any target is logged and does not block the others.
5. The dashboard's escalations view honors `escalation_closed` —
   closed incidents drop out of the open list automatically — and
   displays a per-incident MTTR column.
6. AGENT.md updates per ADR-0030 at every touched component
   (services/api + cli + dashboard where applicable).

## Constraints / scope

### In scope

- All six producer + lifecycle + consumer pieces above.
- Server-side test coverage at each layer (producer, sweep, API
  endpoints, notifier, dashboard query).
- CLI test coverage for the new subcommands.
- AGENT.md updates per ADR-0030.

### Out of scope

- Frontend dashboard (services/dashboard) changes beyond the API
  contract. The dashboard renders whatever the API exposes; any
  JS/TS visual changes ship in a separate follow-up plan on the
  dashboard track.
- Grafana / OTel surfacing of MTTR metrics. The MTTR field is on
  the `escalation_closed` event payload; piping it to OTel is a
  follow-up.
- Mobile / native push, email, PagerDuty integrations. The
  generic webhook fan-out covers the operator targets we have
  today; richer surfaces ship if a real need surfaces.
- Backfilling MTTR for historical (pre-ADR-0062) escalations.
  The close sweep operates forward-only; old escalations stay
  open until they hit a close trigger naturally or are
  `operator_close`d via the CLI.

### Budget

Five worker dispatches. Tasks A and B dispatch in parallel
immediately; C / D / E unblock after B's PR merges.

## Sequence of work

```yaml
sequence_of_work:
  - id: terminal-step-failure-producer
    title: "ADR-0062 Step 1 — terminal-step-failure escalation producer"
    workflow: wf-author
    intent: |
      STUDY:
        - `services/api/treadmill_api/events/task.py` —
          `TaskEscalatedToOperator` payload; the `reason` Literal
          to extend; the field set to add `step_name` to.
        - `services/api/treadmill_api/coordination/triggers.py` —
          existing escalation triggers (`maybe_dispatch_gate_broken_escalation`
          is the closest sibling). Locate the consumer wire-up.
        - `services/api/treadmill_api/coordination/consumer.py` —
          where `_maybe_dispatch_gate_broken_escalation` is called
          from the step-event handler. The new producer hooks in
          at the same seam.

      BUILD:
        - Extend `TaskEscalatedToOperator.reason` Literal to
          include `terminal_step_failure`. Add a new optional
          field `step_name: str | None = None` to the payload.
        - In `coordination/triggers.py`, add
          `maybe_dispatch_terminal_step_failure_escalation(session,
          step_id, task_id, ...)`. It subscribes to `step.failed`
          events. Logic:
          - Check whether the workflow_run associated with this
            step has any remaining steps to dispatch. If yes,
            return without emitting (the loop will retry).
          - Check whether ANOTHER `task.escalated_to_operator`
            event fired for this task within the last 5 minutes
            (dedup window). If yes, return — the cap-reached
            producer has already covered this case.
          - Otherwise emit `escalated_to_operator` with
            `reason='terminal_step_failure'`, `step_name=<the
            failing step>`, and `gate_log_excerpt` populated
            from the step's captured `log_excerpt` if present.
        - In `coordination/consumer.py`, call the new producer
          from the `step.failed` branch alongside the existing
          gate-broken / cap-reached call sites.

      Tests:
        - `services/api/tests/test_terminal_step_failure_producer.py`:
          - Happy path: a step.failed reaches terminal workflow
            state with no concurrent cap-reached event ->
            escalation fires once with the expected reason +
            step_name + gate_log_excerpt.
          - Dedup: a wf-conflict-cap-reached escalation fired 30s
            earlier -> the terminal-step-failure producer skips.
          - Workflow has more steps queued -> producer skips.
        - Update `services/api/tests/test_events_registry.py`
          (or wherever the registry's snapshot test lives) for
          the extended reason Literal.

      AGENT.md updates: services/api with one "Recent changes"
      bullet citing ADR-0062 + the new producer, and an extension
      to the existing key-surfaces entry for triggers.py.
    scope:
      files:
        - services/api/treadmill_api/events/task.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_terminal_step_failure_producer.py
        - services/api/tests/test_events_registry.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/coordination/escalation_close_sweep.py
        - services/api/treadmill_api/routers/escalations.py
    validation:
      - kind: deterministic
        description: |
          The new producer tests plus the existing trigger / registry
          tests pass.
        script: |
          cd services/api && uv run pytest tests/test_terminal_step_failure_producer.py tests/test_events_registry.py -q
      - kind: deterministic
        description: |
          The new producer function and the payload extensions are
          present.
        script: |
          grep -lE "maybe_dispatch_terminal_step_failure_escalation" services/api/treadmill_api/coordination/triggers.py
          grep -lE "terminal_step_failure" services/api/treadmill_api/events/task.py
          grep -lE "step_name" services/api/treadmill_api/events/task.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0062.
        script: |
          grep -lE "ADR-0062" services/api/AGENT.md

  - id: escalation-closed-event-and-sweep
    title: "ADR-0062 Step 2 — TaskEscalationClosed event + close-detection sweep"
    workflow: wf-author
    intent: |
      STUDY:
        - `services/api/treadmill_api/events/task.py` +
          `events/registry.py` — the typed-event idiom.
        - `services/api/treadmill_api/coordination/stuck_task_sweep.py`
          — the closest sibling for the new sweep module's shape
          (a deterministic scheduled-tick handler that emits
          events).
        - `services/api/treadmill_api/seed/schedules.py` — where
          new `*/N` schedules register.

      BUILD:
        - New `TaskEscalationClosed` event payload with fields:
          `close_reason: Literal['re_progressed','pr_merged',
          'cancelled','superseded','operator_close']`,
          `opened_at: datetime` (denormalized from the matching
          open event), `mttr_seconds: int`. Register in
          `events/registry.py`.
        - New module `coordination/escalation_close_sweep.py`
          (mirrors `stuck_task_sweep.py`'s shape). The sweep:
          - Finds open incidents (any `task.escalated_to_operator`
            without a matching later `task.escalation_closed` for
            the same `task_id`).
          - For each, checks the five close triggers in order
            (re_progressed via step.completed > opened_at; then
            terminal events pr_merged / cancelled / superseded).
          - On match, emits `escalation_closed` with computed
            `mttr_seconds = (now - opened_at).seconds`.
        - Add a `*/2` schedule entry in `seed/schedules.py` named
          `wf-escalation-close-sweep` referencing the new module.
          Workflow registration uses the existing
          `register_no_op_workflow` idiom (or whatever
          stuck_task_sweep uses).

      Tests:
        - `services/api/tests/test_escalation_close_sweep.py`:
          one case per close trigger plus a no-op case (open
          incident with no trigger fires nothing).
        - Update `services/api/tests/test_seed_schedules.py` if
          it snapshots the registered schedules.

      Operator-close path is added in this task (so the CLI
      `close` command in Step 3 has a corresponding emission
      function), even though the CLI itself ships in Step 3.

      AGENT.md update + ADR-0062 reference.
    scope:
      files:
        - services/api/treadmill_api/events/task.py
        - services/api/treadmill_api/events/registry.py
        - services/api/treadmill_api/coordination/escalation_close_sweep.py
        - services/api/treadmill_api/seed/schedules.py
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_escalation_close_sweep.py
        - services/api/tests/test_seed_schedules.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/escalations.py
        - services/api/treadmill_api/coordination/notification_fanout.py
    validation:
      - kind: deterministic
        description: |
          The new sweep tests plus the seed-schedules test pass.
        script: |
          cd services/api && uv run pytest tests/test_escalation_close_sweep.py tests/test_seed_schedules.py -q
      - kind: deterministic
        description: |
          The new event type and the sweep module exist.
        script: |
          grep -lE "TaskEscalationClosed|escalation_closed" services/api/treadmill_api/events/task.py
          grep -lE "TaskEscalationClosed" services/api/treadmill_api/events/registry.py
          grep -lE "wf-escalation-close-sweep|escalation_close_sweep" services/api/treadmill_api/seed/schedules.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0062.
        script: |
          grep -lE "ADR-0062" services/api/AGENT.md

  - id: cli-escalations-group
    title: "ADR-0062 Step 3 — treadmill escalations CLI group + API endpoints"
    workflow: wf-author
    depends_on: [task.escalation-closed-event-and-sweep.pr_merged]
    intent: |
      STUDY:
        - `cli/treadmill_cli/commands/onboarding.py` and
          `cli/treadmill_cli/commands/schedules.py` for the
          command-group module idiom + how they wire into
          `cli.py`'s `app.add_typer(...)`.
        - `services/api/treadmill_api/routers/dashboard/overview.py`
          — `_ESCALATIONS_SQL` is the existing read surface for
          open incidents; the new endpoints reuse it.
        - `services/api/treadmill_api/coordination/triggers.py`
          — `_emit_operator_escalation` shape. The `operator_close`
          path emits via a sibling helper in this task.

      BUILD:
        - New router `services/api/treadmill_api/routers/escalations.py`
          exposing:
          - `GET /api/v1/escalations` — list open incidents
            (optional `?reason=` + `?task=` prefix filters).
          - `GET /api/v1/escalations/stream` — long-poll
            endpoint for `tail`; chunked transfer or
            server-sent-events, whichever lands cleaner with
            FastAPI. Streams new open + close events as they
            land.
          - `POST /api/v1/escalations/{task_id}/close` — emits
            `escalation_closed` with
            `close_reason='operator_close'` + computed MTTR.
          - `POST /api/v1/escalations/{task_id}/ack` — emits
            existing `escalation_acknowledged`.
          - `GET /api/v1/escalations/report` — MTTR aggregation;
            query params `?since=` + `?by=reason|day|task`.
        - New CLI module `cli/treadmill_cli/commands/escalations.py`
          with `tail`, `list`, `close`, `ack`, `report`
          subcommands. Wire into `cli/treadmill_cli/cli.py` via
          `app.add_typer(escalations_app)`.
        - Tests on both sides.

      AGENT.md updates on both cli and services/api.
    scope:
      files:
        - services/api/treadmill_api/routers/escalations.py
        - services/api/treadmill_api/routers/__init__.py
        - services/api/treadmill_api/app.py
        - services/api/tests/test_escalations_routes.py
        - cli/treadmill_cli/commands/escalations.py
        - cli/treadmill_cli/cli.py
        - cli/tests/test_escalations_cli.py
        - cli/treadmill_cli/AGENT.md
        - services/api/AGENT.md
      services_affected:
        - services/api
        - cli
      out_of_scope:
        - services/api/treadmill_api/coordination/notification_fanout.py
        - services/api/treadmill_api/routers/dashboard/
    validation:
      - kind: deterministic
        description: |
          API + CLI test suites pass for the new module.
        script: |
          cd services/api && uv run pytest tests/test_escalations_routes.py -q
          cd cli && uv run pytest tests/test_escalations_cli.py -q
      - kind: deterministic
        description: |
          The new router and CLI module are present and wired in.
        script: |
          grep -lE "escalations_app|@escalations_app" cli/treadmill_cli/commands/escalations.py
          grep -lE "add_typer.*escalations" cli/treadmill_cli/cli.py
          grep -lE "/escalations|escalations_router" services/api/treadmill_api/app.py
      - kind: deterministic
        description: |
          AGENT.md files reference ADR-0062.
        script: |
          grep -lE "ADR-0062" cli/treadmill_cli/AGENT.md
          grep -lE "ADR-0062" services/api/AGENT.md

  - id: notification-fanout-slack-and-webhooks
    title: "ADR-0062 Step 4 — Slack notifier + pluggable webhook fan-out"
    workflow: wf-author
    depends_on: [task.escalation-closed-event-and-sweep.pr_merged]
    intent: |
      STUDY:
        - `services/api/treadmill_api/app.py` — the lifespan
          context where subscribers are wired (look for
          `app.state.publisher` and `app.state.consumer`
          patterns).
        - `services/api/treadmill_api/settings.py` (or
          `config.py`) — how env vars are declared as typed
          Settings fields.
        - `services/api/treadmill_api/events/__init__.py` — the
          `EventPayload` / `parse_payload` shape for subscribers
          that consume from the events stream.

      BUILD:
        - New module `coordination/notification_fanout.py`:
          - Async subscriber over the events bus (or a thin
            tail on the events table — pick whatever the
            existing publisher pattern uses).
          - On every `task.escalated_to_operator` and
            `task.escalation_closed` event:
            - If `TREADMILL_SLACK_WEBHOOK_URL` is set, POST a
              Slack-formatted JSON (text body with emoji + task
              id snippet + reason + MTTR for close events).
            - For each URL in `TREADMILL_NOTIFICATION_WEBHOOKS`
              (comma-separated, may be empty), POST the raw
              typed-event JSON.
          - Per-target failures: log + continue; never throw.
        - Wire the subscriber into the FastAPI lifespan
          alongside the existing publisher / consumer setup.
        - Add the two new settings fields in `settings.py`
          (default empty string / empty list).

      Tests:
        - Mock httpx (or whatever HTTP client is in use), assert
          Slack format on open + close events, assert each
          configured webhook URL receives the raw event JSON,
          assert one-failing-target-does-not-block-others.

      AGENT.md update with the new module + the env var contract.
    scope:
      files:
        - services/api/treadmill_api/coordination/notification_fanout.py
        - services/api/treadmill_api/app.py
        - services/api/treadmill_api/settings.py
        - services/api/tests/test_notification_fanout.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/escalations.py
        - services/api/treadmill_api/routers/dashboard/
    validation:
      - kind: deterministic
        description: |
          The notification_fanout tests pass against mocked HTTP.
        script: |
          cd services/api && uv run pytest tests/test_notification_fanout.py -q
      - kind: deterministic
        description: |
          The new module exists and the env vars are declared.
        script: |
          grep -lE "TREADMILL_SLACK_WEBHOOK_URL|TREADMILL_NOTIFICATION_WEBHOOKS" services/api/treadmill_api/settings.py
          grep -lE "def fanout|class NotificationFanout|class FanoutSubscriber" services/api/treadmill_api/coordination/notification_fanout.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0062.
        script: |
          grep -lE "ADR-0062" services/api/AGENT.md

  - id: dashboard-mttr-rendering
    title: "ADR-0062 Step 5 — dashboard escalations view honors close + MTTR column"
    workflow: wf-author
    depends_on: [task.escalation-closed-event-and-sweep.pr_merged]
    intent: |
      STUDY:
        - `services/api/treadmill_api/routers/dashboard/overview.py`
          — `_ESCALATIONS_SQL` is the read query. Today it
          treats every escalation row as open until
          `escalation_acknowledged` lands. The amendment must
          also exclude rows that have a later
          `escalation_closed` event for the same task_id.
        - The dashboard API response shape — find the
          escalations column array and add an `mttr_seconds`
          field (nullable; populated only when the event is
          closed in the result set, which the open-incidents
          query won't normally include).

      BUILD:
        - Update `_ESCALATIONS_SQL` so an open incident requires
          no later `escalation_closed` event AND no later
          `escalation_acknowledged` event (existing).
        - Add a sibling `_CLOSED_ESCALATIONS_SQL` for an
          optional `?include_closed=true` query param so the
          dashboard can also surface a "recently closed"
          ribbon with MTTR.
        - Extend the JSON response shape with `mttr_seconds`
          on the closed-incident path.

      Tests for the new query shapes + the response field.
      AGENT.md update.

      No services/dashboard frontend changes — the dashboard
      will pick up the new field whenever the JS layer is
      ready; out of scope here.
    scope:
      files:
        - services/api/treadmill_api/routers/dashboard/overview.py
        - services/api/tests/test_dashboard_overview.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/dashboard/
        - services/api/treadmill_api/routers/escalations.py
        - services/api/treadmill_api/coordination/notification_fanout.py
    validation:
      - kind: deterministic
        description: |
          The dashboard overview test suite passes with the
          updated _ESCALATIONS_SQL.
        script: |
          cd services/api && uv run pytest tests/test_dashboard_overview.py -q
      - kind: deterministic
        description: |
          The query honors escalation_closed and the new column.
        script: |
          grep -lE "escalation_closed" services/api/treadmill_api/routers/dashboard/overview.py
          grep -lE "mttr_seconds" services/api/treadmill_api/routers/dashboard/overview.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0062.
        script: |
          grep -lE "ADR-0062" services/api/AGENT.md
```

## Diagram

Not applicable here — ADR-0062's sequence diagram is the canonical
view of the buffer/open/close lifecycle. Plan readers should
reference the ADR.

## Risks / unknowns

- **`step.failed` terminal-detection logic.** Tonight's silent
  stall fired because the workflow's local retry exhausted; we
  need a reliable "is this workflow_run terminal?" signal in the
  producer. Mitigation: the producer reads the run's remaining
  step queue; if zero, treat as terminal. If the signal is
  fuzzier than expected, fall back to a "no step.completed within
  N seconds of step.failed" heuristic.
- **Slack webhook URL secret rotation.** Same secrets channel as
  `CLAUDE_CODE_OAUTH_TOKEN` per ADR-0055; rotation procedure
  documented separately. We accept the rotation discipline as
  pre-existing.
- **Streaming endpoint complexity.** `GET /api/v1/escalations/
  stream` can be implemented as SSE (clean) or as long-polling
  (simpler). The CLI's `tail` works with either. The task's
  worker picks.
- **Dashboard frontend lag.** The dashboard JS layer is on a
  separate track; the MTTR column may not render visibly until
  it picks up the new API field. The CLI surfaces work
  immediately and are sufficient for orchestrator-side use.

## Decisions captured during execution

(empty at draft time; appended as work progresses)

## Post-mortem

(filled when plan transitions to completed)
