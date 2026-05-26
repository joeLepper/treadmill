---
auto_merge: true
status: active
---

# Plan: Token usage dashboards (Wave 2) — Grafana panels on Prometheus

- **Status:** active
- **Date:** 2026-05-26
- **Related ADRs:** ADR-0020 (observability — "Token tracking"), ADR-0043 (dev-local o11y)
- **Builds on:** OTel HTTP-exporter fix (PR #4) — Prometheus now receives the
  worker token counters; token-persist (PR #13) is durable accounting; this
  Wave 2 makes it operator-visible.

## Goal

Prometheus already has the 4 Claude token counters flowing
(`treadmill_claude_tokens_{input,output,cache_creation,cache_read}_total`)
labeled `{model, role, task_id, step_id}` — verified. The existing dashboard
`infra/observability/dashboards/treadmill-overview.json` has Worker Step
Duration but **zero token panels**. Add panels so operators can see token
usage by role/model/type over time, and drill into a single task via the
existing `$task_id` template variable.

## Success criteria

- The dashboard JSON includes ≥4 new panels, all backed by the existing
  Prometheus datasource and the `treadmill_claude_tokens_*_total` metrics.
- A `python3 -c "import json; json.load(...)"` round-trip succeeds (valid JSON).
- The dashboard's existing panels remain unchanged.

## Constraints / scope

### In scope
The dashboard JSON only:
`infra/observability/dashboards/treadmill-overview.json`. No code, no
infra/CDK, no Grafana config beyond the JSON.

### Out of scope
- $cost math (per-model price table) — note as a follow-on; needs a price
  table.
- Postgres-datasource panels on the durable columns — separate plan (Grafana
  may not have a Postgres datasource configured yet).
- New datasources, alerting rules.

### Budget
One task, `auto_merge: true`. Pure JSON change; no migrations, no code.

## sequence_of_work

```yaml
sequence_of_work:
  - id: token-dashboards-wave2
    title: Add token-usage panels to the Grafana overview dashboard (ADR-0020)
    workflow: wf-author
    intent: |
      Add ≥4 new panels to
      ``infra/observability/dashboards/treadmill-overview.json``. Read it
      first to see the existing panel structure (title, type, gridPos,
      datasource, targets/expr) and the ``$task_id`` template variable — match
      that shape exactly.

      Prometheus has these metrics (verified 2026-05-26):
        - ``treadmill_claude_tokens_input_total``
        - ``treadmill_claude_tokens_output_total``
        - ``treadmill_claude_tokens_cache_creation_total``
        - ``treadmill_claude_tokens_cache_read_total``
      Labels on each: ``model``, ``role``, ``task_id``, ``step_id``,
      ``service_name``.

      Add these panels (timeseries unless noted):

        (1) **Token rate by type** — stacked timeseries; one series per type
        (input, output, cache_creation, cache_read). Use
        ``sum(rate(treadmill_claude_tokens_<type>_total[5m]))`` as the
        expression, one query per type. Legend = the type name.

        (2) **Tokens by role (top N over window)** — bar chart or stat panel
        showing total tokens per ``role`` over the dashboard's time range.
        ``sum by (role) (increase(treadmill_claude_tokens_input_total[$__range])
        + increase(treadmill_claude_tokens_output_total[$__range]))`` (sum
        input + output; cache_read is usually free; cache_creation is paid
        but small — pick one expression that captures "spend signal", and
        document the choice in the panel description).

        (3) **Tokens by model** — same shape as #2 but ``sum by (model)``.

        (4) **Tokens for selected task** — drilldown filtered by the existing
        ``$task_id`` variable: ``sum by (model)
        (increase(treadmill_claude_tokens_input_total{task_id=~"$task_id"}[$__range])
        + increase(treadmill_claude_tokens_output_total{task_id=~"$task_id"}[$__range]))``.
        Use the same regex pattern the Worker Logs panel uses for
        ``$task_id`` matching. Panel description: "Total tokens consumed by
        the selected $task_id, grouped by model. Filter via the dashboard's
        task_id variable at top."

      All panels: copy ``datasource`` UID/value from the existing
      ``Worker Step Duration`` panel (the existing Prometheus datasource);
      put the new panels in a new row "Claude token usage (ADR-0020)" below
      the existing Worker Metrics row; set sensible ``gridPos`` (8h × 12w
      for timeseries, smaller for stat panels). Don't touch the existing
      panels.

      DOCS (ADR-0030 — REQUIRED): note in
      ``infra/observability/AGENT.md`` (or the dashboards subdir's AGENT.md
      if one exists; check first) that the overview dashboard now includes
      token-usage panels backed by ``treadmill_claude_tokens_*_total`` from
      OTel/Prometheus.
    scope:
      files:
        - infra/observability/dashboards/treadmill-overview.json
        - infra/observability/AGENT.md
      out_of_scope:
        - services/api/
        - workers/agent/
        - cli/
        - infra/observability/otel-collector-config.yaml
        - infra/observability/prometheus.yml
    validation:
      - kind: deterministic
        description: |
          Dashboard JSON is valid; at least 4 new panels reference the
          treadmill_claude_tokens metrics; existing panel titles are
          preserved.
        script: |
          python3 -c "
          import json
          d = json.load(open('infra/observability/dashboards/treadmill-overview.json'))
          titles = [p.get('title','') for p in d.get('panels',[])]
          token_panels = [t for t in titles if 'token' in t.lower()]
          assert len(token_panels) >= 4, f'expected >=4 token panels, got {token_panels}'
          # at least one panel must query a treadmill_claude_tokens_* metric
          import json as _j
          blob = _j.dumps(d)
          assert 'treadmill_claude_tokens' in blob, 'no panel queries the token metrics'
          # preserved: the existing 'Worker Step Duration' panel still there
          assert any('Worker Step Duration' in t for t in titles), 'existing panel missing'
          print('OK', len(token_panels), 'token panels;', len(titles), 'total')
          "
```

## Risks / unknowns

- **Dashboard format drift:** Grafana JSON schema varies by version; the
  worker should match the existing panels' shape exactly (datasource, gridPos
  keys) rather than invent new keys.
- **`task_id` cardinality on Prometheus:** the per-task drilldown is bounded
  by the active task_id filter; aggregate panels sum-by other labels, so
  cardinality stays manageable.
- **Deploy:** dashboards reload automatically (Grafana provisioning); no
  redeploy needed beyond the merge.

## Post-mortem

_(filled when the wave completes)_
