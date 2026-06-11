# Plan: ADR-0089 token-economics implementation

- **Status:** drafting
- **Date:** 2026-06-11
- **Related ADRs:** ADR-0089 (token-economics controls — PR #305), ADR-0071
  (relay verbosity), ADR-0068 (treadmill-events channel)

## Goal

Implement ADR-0089's three controls — wake-class filtering, the `llm_calls`
harvester + standing report, and the cache-aware cadence convention — as
worker tasks on the treadmill repo's own team (the first plan routed
through `coordinator-joelepper-treadmill`). Per the operator's tier
directive (2026-06-11): implementation belongs in the worker cluster.

## Success criteria

1. With `TREADMILL_WAKE_ACTIONS` unset, an orchestrator-role session
   receives NO wake for `github.check_run_completed` /
   `github.pr_synchronize`, DOES wake for `github.pr_merged` /
   `task.*_verdict` / escalations / `prod_promotion.*` / relays, and the
   first delivered wake after suppression carries the digest line with
   accurate per-action counts — and a suppressed-only stream (no
   allowlisted event at all) produces a self-originated digest wake
   within max-suppression-age + one poll period.
2. A configured wake/relay pair violating the wake ⊇ relay superset logs a
   startup WARN naming both sets.
3. `treadmill tokens report --since <date>` prints per-label calls,
   output, cache-read, cache-creation, and hit-ratio from `llm_calls`
   rows the harvester inserted; rerunning the harvester is idempotent
   (no duplicate rows for the same transcript span).
4. Coordinator/evaluator/worker sessions remain unfiltered by default
   (their event consumption is bookkeeping-load-bearing).
5. Session templates carry the cadence convention; template tests pin it.

## Constraints / scope

### In scope
- `tools/cc-channel-treadmill/treadmill-events.ts`: wake filter +
  role defaults + digest counter + superset WARN.
- `services/api` or `cli`: the harvester (transcript JSONL →
  `llm_calls`) + `treadmill tokens report`; the `task_execution_id`
  nullability-vs-synthetic-row decision (implementer documents choice).
- Session/team templates: the cadence convention text + tests.

### Out of scope
- Model routing by task class; automated context compaction; fleet
  sizing; changing coordinator/evaluator/worker default filters
  (measure first); Telegram relay-level changes (ADR-0071 untouched).

### Budget
Three worker tasks, ~1 day each. Abort trigger: if the channel server's
notification path can't carry the digest without a protocol change,
stop and re-scope rather than redesign in-flight.

## Sequence of work

```yaml
sequence_of_work:
  - id: wake-filter
    title: Wake-class filtering in the treadmill-events channel server
    workflow: wf-implement
    depends_on: []
    intent: |
      IMPLEMENT in tools/cc-channel-treadmill/treadmill-events.ts (Bun):
      1. TREADMILL_WAKE_ACTIONS env — comma-separated entity.action globs
         deciding which events become notifications/claude/channel wakes.
      2. Role defaults when unset (TREADMILL_ROLE): orchestrator =
         github.pr_merged, task.*_verdict, task.escalat*,
         task.evaluator_timeout, task.rework_exhausted (escalation-CLASS
         actions that escape the glob are ENUMERATED — a filtered-away
         escalation is the one forbidden failure mode), task.registered,
         task.cancelled, deploy.failed, staging_smoke.failed, datamigration.*; relay messages and
         reconcile frames ALWAYS wake. coordinator/evaluator/worker =
         unfiltered.
      3. Suppression digest: count suppressed events per action; prepend a
         one-line summary to the NEXT delivered wake, then reset.
      4. MAX-SUPPRESSION-AGE (bounded blindness): suppressed events
         pending AND no delivered wake for TREADMILL_MAX_SUPPRESSION_AGE
         (default 60min) -> emit ONE self-originated digest wake.
      5. Startup WARN when the wake set is not a superset of the
         ADR-0071 relay set (wake-superset-of-relay invariant).
      Tests (bun test, same dir): role defaults incl. the two enumerated
      escalation actions wake; glob matching; digest accumulate/reset; a
      suppressed-only stream produces a digest wake within age+period;
      superset WARN fires on a violating pair.
      DOC: file header + tools/cc-channel-treadmill section of the
      component docs; AGENT.md "Recent changes" entry.
    scope:
      files:
        - tools/cc-channel-treadmill/treadmill-events.ts
        - tools/cc-channel-treadmill/AGENT.md
    validation:
      - kind: deterministic
        description: channel-server suite green incl. the new filter/digest/WARN tests
        script: cd tools/cc-channel-treadmill && bun test
  - id: tokens-harvester
    title: llm_calls harvester + treadmill tokens report
    workflow: wf-implement
    depends_on: []
    intent: |
      IMPLEMENT (cli + services/api surfaces):
      1. Harvester: walk ~/.claude/projects/*/ transcript JSONL; extract
         per-call usage (timestamp, model, input/output/cache-creation/
         cache-read) from assistant-message usage fields; attribute
         session-dir -> label; join to task_executions
         (worker_label + started_at/completed_at window) where exactly one
         matches. Idempotency cursor: (transcript file, byte offset) —
         re-runs insert nothing already harvested.
      2. llm_calls.task_execution_id is NOT NULL today but orchestrator
         calls have no execution row: either relax to nullable (alembic
         migration) or synthesize a per-session execution row — decide,
         document the choice in the migration/module docstring.
      3. treadmill tokens report --since <date>: per-label calls, output,
         fresh-in, cache-creation, cache-read, hit-ratio; unparseable
         transcript lines are COUNTED AND REPORTED, never silently
         skipped.
      Tests: pytest against fixture JSONL (happy path, idempotent rerun,
      malformed lines counted, window-join attribution).
      DOC: services/api AGENT.md + cli AGENT.md entries.
    scope:
      files:
        - cli/treadmill_cli/commands/tokens.py
        - cli/treadmill_cli/cli.py
        - cli/tests/test_tokens_command.py
        - services/api/alembic/versions/
        - services/api/AGENT.md
        - cli/AGENT.md
    validation:
      - kind: deterministic
        description: tokens command suite green (fixtures, idempotent rerun, malformed-line counting)
        script: cd cli && python3 -m pytest tests/test_tokens_command.py -q
  - id: cadence-convention
    title: Cache-aware cadence convention in session templates
    workflow: wf-implement
    depends_on:
      - task.wake-filter.pr_merged
    intent: |
      After wake-filter merges (the text cites the live mechanism): add
      the ADR-0089 cadence convention to the team templates — when
      actively watching fast-changing state poll inside the cache window
      (<=270s); otherwise commit to long intervals (>=20min); never the
      ~5-minute middle; bursty roles batch work per wake. Template tests
      pin the convention lines (same pattern as the ADR-0088 §3.7/§3.8
      pins in tools/team-templates/tests/test_coordinator_template.py).
      DOC: tools/team-templates/AGENT.md.
    scope:
      files:
        - tools/team-templates/coordinator/CLAUDE.md.tmpl
        - tools/team-templates/worker/CLAUDE.md.tmpl
        - tools/team-templates/evaluator/CLAUDE.md.tmpl
        - tools/team-templates/tests/test_coordinator_template.py
        - tools/team-templates/AGENT.md
    validation:
      - kind: deterministic
        description: template suite green incl. the pinned cadence-convention lines
        script: cd tools/team-templates && python3 -m pytest tests/ -q
```

## Risks / unknowns

- Transcript format drift (undocumented JSONL): the harvester must skip
  unparseable lines loudly (count them in the report) rather than crash.
- The digest counter lives in channel-server memory; a server restart
  loses suppressed counts — acceptable (the events table is the record),
  note in code.
- First plan through the treadmill team: the team's CI/lint surface is
  this repo's own (`pytest` for cli/api, Bun for the channel server) —
  worker briefs must name exact test commands per component.

## Decisions captured during execution

(running)

## Post-mortem

(filled at completion)
