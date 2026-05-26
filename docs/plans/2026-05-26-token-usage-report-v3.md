---
auto_merge: true
status: active
---

# Plan: Token-usage report v3 (API + CLI) — hygienic validation script

- **Status:** active
- **Date:** 2026-05-26
- **Related:** ADR-0020 (token tracking — persisted as of token-persist v2 PR #13),
  [[project_validation_gate_loop_pattern]]
- **Supersedes:** v1, v2 (cancelled — author-gate failed with `pytest -k`
  matching no tests; analyzer kept declaring "complete"; loop).

## Goal

Re-dispatch the token-usage report (API + CLI) — same surface as v2, but with
a validation script that doesn't trip the `pytest -k` wedge.

## Constraints / scope

### In scope
The aggregation report only: `GET /api/v1/token-usage` + `treadmill tokens`
CLI + tests + docs. The persist columns it reads are already on main (PR #13).

### Out of scope
Grafana panels (Wave 2, separate plan in this PR). Any change to the persist
columns, migrations, or worker emission path.

### Validation hygiene (the v2 wedge fix)
Target **exact test file paths**, not `pytest -k`. Per
[[project_validation_gate_loop_pattern]] the `-k` pattern was the recurring
trap (exit 5 on no-match, transient `uv` install blips, deceptive "1 passed"
chains). Worker must create `services/api/tests/test_token_usage_endpoint.py`
and `cli/tests/test_cli_tokens.py` with **those exact names** so the script
can hit them deterministically.

## sequence_of_work

```yaml
sequence_of_work:
  - id: token-report
    title: Token-usage report — aggregate durable per-step tokens (API + CLI), v3
    workflow: wf-author
    intent: |
      Surface the durable token columns (added by PR #13) as an aggregation
      report. Read first: ``services/api/treadmill_api/models/run.py`` —
      ``WorkflowRunStep`` now has ``input_tokens, output_tokens,
      cache_creation_tokens, cache_read_tokens, model`` (all nullable).
      ``services/api/treadmill_api/routers/tasks.py`` for router style;
      ``cli/treadmill_cli/cli.py`` + ``cli/treadmill_cli/api_client.py`` for
      the CLI + client patterns.

      (1) API — ``services/api/treadmill_api/routers/token_usage.py`` (NEW,
      registered in ``routers/__init__.py``): ``GET /api/v1/token-usage`` with
      query params:
        - ``group_by`` (one of ``role|workflow|model|task``, default ``role``)
        - ``task_id`` (optional UUID filter)
        - ``since`` (optional ISO datetime window)
      Returns rows ``{group: str, input_tokens, output_tokens,
      cache_creation_tokens, cache_read_tokens, total_tokens}`` summed via SQL
      ``GROUP BY`` over ``workflow_run_steps`` (join to runs/tasks/workflow
      for the ``workflow``/``task`` grouping). NULLs sum as 0 via ``coalesce``;
      exclude steps with all-NULL tokens from counts. Sort by ``total_tokens``
      DESC.

      (2) CLIENT — ``cli/treadmill_cli/api_client.py``: ``get_token_usage(
      self, *, group_by, task_id=None, since=None) -> list[dict]``. Mirror
      ``retry_task`` for error handling.

      (3) CLI — ``cli/treadmill_cli/cli.py``: a ``tokens`` command:
      ``treadmill tokens [--by role|workflow|model|task] [--task <id>]
      [--since <iso>]`` printing a table (group, input, output, cache, total)
      sorted by total desc.

      (4) TESTS — create EXACTLY these two files (the validation script
      targets these paths):
        * ``services/api/tests/test_token_usage_endpoint.py`` — seed two
          ``workflow_run_steps`` rows with different roles + token values;
          assert the GET endpoint returns coalesced sums grouped correctly;
          assert the ``task_id`` filter narrows results; assert all-NULL rows
          are excluded.
        * ``cli/tests/test_cli_tokens.py`` — invoke ``treadmill tokens
          --by role`` against a stubbed client returning canned rows; assert
          the output table has the expected columns + order.

      (5) DOCS (ADR-0030 — REQUIRED): update ``services/api/AGENT.md`` with
      the new endpoint; CLI command reference if one exists.
    scope:
      files:
        - services/api/treadmill_api/routers/token_usage.py
        - services/api/treadmill_api/routers/__init__.py
        - cli/treadmill_cli/api_client.py
        - cli/treadmill_cli/cli.py
        - services/api/tests/test_token_usage_endpoint.py
        - cli/tests/test_cli_tokens.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/models/run.py
        - services/api/alembic/versions/
        - workers/agent/
        - infra/observability/
    validation:
      - kind: deterministic
        description: |
          The endpoint + CLI exist with the expected functions, and their
          dedicated test files (exact paths, no -k) pass.
        script: |
          grep -q "def list_token_usage\|@router.get(\"\"\\|/token-usage" services/api/treadmill_api/routers/token_usage.py \
            && grep -q "def get_token_usage" cli/treadmill_cli/api_client.py \
            && grep -qE "tokens|@task_app.command|@app.command" cli/treadmill_cli/cli.py \
            && [ -f services/api/tests/test_token_usage_endpoint.py ] \
            && [ -f cli/tests/test_cli_tokens.py ] \
            && cd services/api && uv run pytest tests/test_token_usage_endpoint.py -q \
            && cd ../cli && uv run pytest tests/test_cli_tokens.py -q
```

## Risks / unknowns

- **High-cardinality grouping on `task_id`:** if there are many tasks, the
  default `group_by=role` keeps cardinality bounded. Per-task filtering with
  `task_id` is explicit.
- **No alembic/model change** — the v2 worker already shipped these. If the
  worker tries to "fix" the model, push back via review.

## Post-mortem

_(filled when the wave completes)_
