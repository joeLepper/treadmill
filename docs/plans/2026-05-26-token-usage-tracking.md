---
auto_merge: true
status: active
---

# Plan: Track token usage (Wave 1 — persist per-step tokens to DB + report)

- **Status:** active
- **Date:** 2026-05-26
- **Related ADRs:** ADR-0020 (observability — "Token tracking"), ADR-0012 (StepOutput envelope), ADR-0011 (typed columns, no JSONB), ADR-0044 (datetime-keyed migrations)
- **Follow-on:** Wave 2 — Grafana dashboard panels on the durable data + optional $cost (tokens × per-model price).

## Goal

We collect per-invocation token usage in the worker (`claude_code.py` parses
Claude Code's `usage` and emits OTel counters `treadmill.claude.tokens.*` labeled
`{model, role, task_id, step_id}`) — and now that the OTel HTTP exporter is
live (2026-05-26), those counters reach Prometheus. But they're still
**ephemeral + high-cardinality on `task_id`**, so per-task / per-workflow /
per-role cost accounting is awkward. This wave makes token usage **durable
and queryable in Postgres**: persist it per workflow-step (riding the
existing `step.completed` event — no new round-trip), then expose a report
(API + CLI) that aggregates by task / workflow / role / model. Wave 2 puts a
Grafana dashboard on top.

## Success criteria

- Each LLM-backed step persists its token usage (input / output / cache-creation
  / cache-read) + the model used, on `workflow_run_steps`.
- A completed `wf-author` (etc.) step shows non-null token columns after the run.
- `treadmill tokens` reports totals grouped by role / workflow / model (and a
  single task's total), reading the durable columns — survives Prometheus
  retention.
- Existing step lifecycle + tests stay green; steps with no LLM call (or
  pre-migration rows) read NULL, not zero-as-fact.

## Constraints / scope

### In scope
Persisting token usage on `step.completed` (worker emit → API persist → DB
columns) and a read-only aggregation report (API + CLI).

### Out of scope
- The OTel counters (keep emitting them — unchanged).
- Grafana panels + $cost math (Wave 2).
- Backfilling historical token usage (only steps completed after this lands).

### Budget
Two sequenced tasks (`token-report` `depends_on` `token-persist` → it dispatches
after persist's PR merges, so it branches off a main that already has the
columns). `auto_merge: true`. Capped at 4 workers globally.

## sequence_of_work

```yaml
sequence_of_work:
  - id: token-persist
    title: Persist per-step token usage to Postgres via step.completed (ADR-0020)
    workflow: wf-author
    intent: |
      Route the already-parsed per-step token usage into durable storage by
      riding the existing ``step.completed`` event. Read first:
        * ``workers/agent/treadmill_agent/claude_code.py`` — where ``usage`` is
          parsed (``_try_parse_json_output``) and ``record_token_usage`` is
          called; ``CodeAuthorResult`` currently carries only ``summary``.
        * ``workers/agent/treadmill_agent/runner.py`` — publishes
          ``step.completed`` (the worker-side event) with PR/branch metadata.
        * ``workers/agent/treadmill_agent/events.py`` — worker-side event
          payloads.
        * ``services/api/treadmill_api/events/step.py`` — ``StepCompleted``
          (carries ``output: StepOutput``, ADR-0012).
        * the consumer's ``step.completed`` handler in
          ``services/api/treadmill_api/coordination/consumer.py`` (find the
          ``UPDATE WorkflowRunStep`` for completed).
        * ``services/api/treadmill_api/models/run.py`` — ``WorkflowRunStep``.

      (1) WORKER — thread the parsed token usage upward:
        - ``claude_code.py``: add the parsed ``usage`` dict + ``role.model`` to
          ``CodeAuthorResult`` (e.g., a ``token_usage: dict | None`` field with
          keys ``input_tokens, output_tokens, cache_creation_tokens,
          cache_read_tokens`` and a ``model`` string). Keep the existing OTel
          ``record_token_usage`` call unchanged.
        - ``runner.py`` + worker ``events.py``: include the token usage in the
          ``step.completed`` event payload as a typed optional sub-field
          ``token_usage`` (NOT inside the polymorphic ``StepOutput.metadata`` —
          token usage is step-execution telemetry, keep it a distinct field).
          When the step made no LLM call, omit it (``None``).

      (2) API EVENT — ``events/step.py``: add an optional ``token_usage`` field
      to ``StepCompleted`` — a small typed model ``StepTokenUsage`` with
      ``input_tokens: int, output_tokens: int, cache_creation_tokens: int,
      cache_read_tokens: int, model: str`` (all required within the sub-model;
      the sub-model itself optional/``None``). Mirror the worker's shape exactly.

      (3) MIGRATION (ADR-0044 datetime-keyed id; ``down_revision`` = current
      single head, verify ``uv run alembic heads``): add to ``workflow_run_steps``
      nullable columns ``input_tokens BIGINT``, ``output_tokens BIGINT``,
      ``cache_creation_tokens BIGINT``, ``cache_read_tokens BIGINT``,
      ``model TEXT`` (all NULLABLE). ``uv run alembic heads`` must still report
      exactly ONE head.

      (4) MODEL — ``run.py``: add the five nullable columns to
      ``WorkflowRunStep``.

      (5) CONSUMER — in the ``step.completed`` handler, when
      ``typed.token_usage`` is present, set the five columns in the same
      ``UPDATE`` that writes ``status='completed'`` / ``output``. NULL when
      absent.

      (6) TESTS:
        * worker: ``claude_code`` returns token_usage in ``CodeAuthorResult``
          when stdout has ``usage`` (and ``None`` when not) — extend the existing
          claude_code test that patches subprocess/stdout.
        * API: a ``step.completed`` carrying ``token_usage`` persists the five
          columns; absent → columns stay NULL.
        * structural: ``WorkflowRunStep.__table__`` has the five columns, all
          nullable.

      (7) DOCS (ADR-0030 — REQUIRED): update ``services/api/AGENT.md`` (new
      token columns + that ``step.completed`` now persists token usage) and
      ``workers/agent/AGENT.md`` (token usage now flows to the API on
      step.completed, in addition to the OTel counters).
    scope:
      files:
        - workers/agent/treadmill_agent/claude_code.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/treadmill_agent/events.py
        - workers/agent/AGENT.md
        - services/api/treadmill_api/events/step.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/models/run.py
        - services/api/alembic/versions/
        - services/api/tests/
        - workers/agent/tests/
        - services/api/AGENT.md
    validation:
      - kind: deterministic
        description: |
          Single alembic head; the five token columns exist + are nullable;
          worker + API token tests pass.
        script: |
          cd services/api \
            && [ "$(uv run alembic heads | grep -c '(head)')" = "1" ] \
            && uv run python -c "from treadmill_api.models.run import WorkflowRunStep as S; c=S.__table__.c; assert all(c[n].nullable for n in ['input_tokens','output_tokens','cache_creation_tokens','cache_read_tokens','model'])" \
            && uv run pytest tests/ -q -k "token" \
            && cd ../../workers/agent && uv run pytest tests/ -q -k "token"

  - id: token-report
    title: Token-usage report — aggregate durable per-step tokens (API + CLI)
    workflow: wf-author
    depends_on: [task.token-persist.pr_merged]
    intent: |
      Surface the durable token columns (added by ``token-persist``) as an
      aggregation report. Read first: ``services/api/treadmill_api/models/run.py``
      (``WorkflowRunStep`` now has the five token columns);
      ``services/api/treadmill_api/routers/tasks.py`` for router/response
      style; ``cli/treadmill_cli/cli.py`` + ``cli/treadmill_cli/api_client.py``
      for the CLI + client patterns (mirror an existing read command like
      ``task list``).

      (1) API — add ``GET /api/v1/token-usage`` (new
      ``routers/token_usage.py``, registered in ``routers/__init__.py``) with
      query params: ``group_by`` (one of ``role|workflow|model|task``, default
      ``role``), optional ``task_id`` filter, optional ``since`` (ISO datetime)
      window. Returns rows ``{group: str, input_tokens, output_tokens,
      cache_creation_tokens, cache_read_tokens, total_tokens}`` summed via SQL
      ``GROUP BY`` over ``workflow_run_steps`` (join to runs/tasks/workflow as
      needed). NULL token columns sum as 0 (``coalesce``); exclude steps with
      all-NULL tokens from counts.

      (2) CLIENT — ``api_client.py``: ``get_token_usage(self, *, group_by,
      task_id=None, since=None) -> list[dict]``.

      (3) CLI — ``cli.py``: a ``tokens`` command:
      ``treadmill tokens [--by role|workflow|model|task] [--task <id>]
      [--since <iso>]`` printing a table (group, input, output, cache, total),
      sorted by total desc.

      (4) TESTS: API aggregation (seed a couple of steps with token columns,
      assert grouped sums + coalesce + ``task_id`` filter); CLI invokes
      ``get_token_usage`` with parsed flags and renders rows.

      (5) DOCS (ADR-0030 — REQUIRED): ``services/api/AGENT.md`` (new
      ``/token-usage`` endpoint) + CLI command reference if one exists.
    scope:
      files:
        - services/api/treadmill_api/routers/token_usage.py
        - services/api/treadmill_api/routers/__init__.py
        - cli/treadmill_cli/api_client.py
        - cli/treadmill_cli/cli.py
        - services/api/tests/
        - cli/tests/
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/models/run.py
        - services/api/alembic/versions/
    validation:
      - kind: deterministic
        description: |
          The token-usage endpoint + CLI exist and their tests pass.
        script: |
          cd services/api && uv run pytest tests/ -q -k "token_usage or token_report" \
            && cd ../cli && uv run pytest tests/ -q -k "tokens"
```

## Risks / unknowns

- **Event round-trip fidelity:** worker-side and API-side `token_usage` shapes
  must match exactly. Mirror field names 1:1.
- **Multiple LLM calls per step:** today a step ≈ one Claude Code invocation;
  if a step makes several, persist the LAST/summed usage — note the assumption.
- **Worker redeploy:** the persist change touches `workers/agent` — after merge
  it needs the agent image rebuilt + new workers to pick it up (the
  deploy-watcher handles that; PRs landing alongside the watcher stale-source
  fix make this fully automatic).

## Post-mortem

_(filled when the plan completes)_
