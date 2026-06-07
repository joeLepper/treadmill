# Plan: ADR-0080 implementation — alembic-migration-runnable rule-check

- **Status:** drafting
- **Date:** 2026-06-05
- **Related ADRs:** ADR-0080 (the decision, PR #226), ADR-0030 (rule-check pattern this extends)

## Goal

Land the new `alembic-migration-runnable` rule-check under
`tools/rule-checks/` so plan-validate (pre-submit) and
wf-validate (in-author cycle) both catch alembic migration
bugs before they ship. Specifically targets the two failure
modes observed in the ADR-0076 PR A implementation today:

1. `op.create_check_constraint(name, condition, table_name=...)`
   — wrong arg order → TypeError at upgrade.
2. Multiple migrations chained off the same parent →
   two alembic heads.

## Success criteria

1. New `tools/rule-checks/alembic-migration-runnable/check.sh`
   that:
   - Exits 0 immediately if no path under
     `services/api/alembic/versions/` is in the changed-files
     list (so the check is a no-op on unrelated PRs).
   - Runs `alembic heads --resolve-dependencies` and asserts
     the line count is exactly 1 (multi-head detection).
   - Runs `alembic upgrade --sql head` and asserts the
     subprocess exit is 0 AND the output contains at least one
     `CREATE`, `ALTER`, `INSERT`, or `DROP` keyword.
   - Emits clear stderr messages naming the failing migration
     filename + revision when either check fails.
2. New `tools/rule-checks/alembic-migration-runnable/README.md`
   following the existing rule-check documentation pattern.
3. `cli/treadmill_cli/plan_validate.py` invokes the check when
   the plan's `scope.files` include any path under
   `services/api/alembic/versions/`.
4. `workers/agent/treadmill_agent/validation_runtime.py` —
   verify the existing rule-check loader picks up the new
   check by filename convention without code changes
   (auto-discovery). If a code change IS required, scope it as
   part of this PR.
5. Test coverage at
   `tools/rule-checks/alembic-migration-runnable/test_check.sh`
   (or `.py` if that's the existing pattern):
   - Happy path: a correctly-shaped migration file passes both
     gates.
   - Failure path A: a migration with a TypeError-shaped
     `op.create_check_constraint` call fails with a clear
     diagnostic.
   - Failure path B: two migrations chained off the same
     parent fail with a multi-head diagnostic.
   - No-op path: no alembic files in changed-files → exit 0
     without running alembic.
6. `tools/rule-checks/AGENT.md` (or equivalent) Recent-changes
   entry citing ADR-0080.

## Constraints / scope

### In scope

- New rule-check script + README in
  `tools/rule-checks/alembic-migration-runnable/`.
- `cli/treadmill_cli/plan_validate.py` wiring.
- `validation_runtime.py` verification (and minimal wiring if
  needed).
- Tests at the rule-check's test path.
- `tools/rule-checks/AGENT.md` Recent-changes entry.

### Out of scope

- **Live-DB upgrade + downgrade** as a richer check (catches
  data-migration bugs the `--sql` dry-run misses). Listed in
  the ADR's Follow-ups; out of v1.
- **Widening the change detector** to also fire on
  `services/api/treadmill_api/models/` changes that imply
  pending migrations. ADR Follow-up.
- **Dashboard surface** for rule-check failures distinct from
  other validation failures.

### Budget

One PR, hand-authored OR worker-dispatched. Estimated
~half-day. No `auto_merge: false` warranted — pure tooling
addition, no shared schema, no Alembic schema change (the
check INVOKES alembic but doesn't add migrations), no CDK.

## Sequence of work

```yaml
sequence_of_work:
  - id: alembic-rule-check
    title: "ADR-0080 — alembic-migration-runnable rule-check + plan-validate + wf-validate wiring"
    workflow: wf-author
    intent: |
      STUDY:
        - docs/adrs/0080-alembic-migration-runnable-gate.md
          — the decision; the three-step check shape (no-op
          guard, heads count, --sql exit + DDL presence) is
          load-bearing.
        - tools/rule-checks/ — list existing rule-checks and
          read 2-3 (e.g. `agent-md-locations/check.sh`,
          `python-tests-resolve/check.sh`,
          `cdk-synth-passes/check.sh`) to internalize the
          existing shape:
            * shebang + set -euo pipefail
            * change-list detection convention
            * exit codes (0 = pass, non-zero = fail)
            * stderr message format
        - cli/treadmill_cli/plan_validate.py — how it invokes
          rule-checks, especially how it filters by scope.files.
        - workers/agent/treadmill_agent/validation_runtime.py —
          the rule-check loader; confirm auto-discovery by
          filename / directory pattern (most likely just
          discovers every check.sh in tools/rule-checks/).
        - services/api/alembic/env.py — confirm offline-mode
          support so `alembic --sql upgrade head` works without
          a live DB.

      BUILD:
        1. New directory tools/rule-checks/alembic-migration-runnable/
           containing:
             - check.sh — the rule-check script (see shape
               below).
             - README.md — documents the check, the two failure
               modes it catches, and the two-step flow.
             - test_check.sh (or .py if existing pattern uses
               Python) — covers happy + failure + no-op paths.
        2. check.sh shape:
             #!/usr/bin/env bash
             set -euo pipefail
             CHANGED_FILES_LIST="${1:-}"   # rule-check loader passes this
             if ! grep -qE 'services/api/alembic/versions/' "$CHANGED_FILES_LIST"; then
               exit 0  # no migrations touched, no-op pass
             fi
             cd services/api
             # Multi-head check
             HEADS_OUTPUT=$(uv run alembic heads --resolve-dependencies 2>&1)
             HEAD_COUNT=$(echo "$HEADS_OUTPUT" | grep -c '(head)' || true)
             if [ "$HEAD_COUNT" -ne 1 ]; then
               echo "alembic-migration-runnable: multiple heads found ($HEAD_COUNT)" >&2
               echo "$HEADS_OUTPUT" >&2
               exit 1
             fi
             # SQL dry-run
             SQL_OUTPUT=$(uv run alembic upgrade --sql head 2>&1)
             if ! echo "$SQL_OUTPUT" | grep -qE 'CREATE|ALTER|INSERT|DROP'; then
               echo "alembic-migration-runnable: --sql head produced no DDL" >&2
               echo "$SQL_OUTPUT" >&2
               exit 1
             fi
             exit 0
        3. Wire into cli/treadmill_cli/plan_validate.py: the
           existing scope-files-based rule-check matcher
           should pick this up automatically if the wiring is
           pattern-based. If it's an explicit registry,
           append the new check name.
        4. Verify workers/agent/treadmill_agent/validation_runtime.py
           auto-discovers the new check.sh by directory
           traversal (the existing nine all live the same way).
           No code edit expected; if one IS needed, scope it.

      TEST:
        - tools/rule-checks/alembic-migration-runnable/test_check.sh
          (or test_check.py if that's the existing pattern):
          * test_happy_path: stage a known-good migration file in
            a temp fixture; run check.sh; assert exit 0.
          * test_failure_typeerror_in_upgrade: stage a migration
            with op.create_check_constraint(name, condition,
            table_name=...) — the exact ADR-0076 PR A pass-1
            bug. Run check.sh; assert non-zero exit; assert
            stderr names the migration filename.
          * test_failure_multi_head: stage two migrations
            chained off the same parent. Run check.sh; assert
            non-zero exit; assert stderr names "multiple heads".
          * test_no_op_when_no_alembic_files: run check.sh with
            a CHANGED_FILES_LIST that contains no alembic paths;
            assert exit 0 immediately.

      DOC: tools/rule-checks/AGENT.md (or
      tools/AGENT.md if the rule-checks index lives there)
      gains a Recent-changes entry naming the new check,
      the two failure modes, and citing ADR-0080.

      Validation MUST NOT use cdk synth, docker, live AWS, or
      network egress. The check.sh's `alembic upgrade --sql head`
      is offline by design; the tests stage fixture migrations
      in temp dirs and don't touch any DB.
    scope:
      files:
        - tools/rule-checks/alembic-migration-runnable/check.sh
        - tools/rule-checks/alembic-migration-runnable/README.md
        - tools/rule-checks/alembic-migration-runnable/test_check.sh
        - cli/treadmill_cli/plan_validate.py
        - workers/agent/treadmill_agent/validation_runtime.py
        - tools/rule-checks/AGENT.md
      services_affected:
        - tools/rule-checks
        - cli
        - workers/agent
      out_of_scope:
        - Live-DB upgrade+downgrade check (ADR Follow-up)
        - Widening change-detector to models/ changes
        - Dashboard rendering of rule-check failures
    validation:
      - kind: deterministic
        description: |
          New rule-check passes its own happy / failure /
          no-op tests. Existing rule-check suite remains green.
        script: |
          bash tools/rule-checks/alembic-migration-runnable/test_check.sh && bash tools/rule-checks/agent-md-locations/check.sh tools/rule-checks/agent-md-section-presence/check.sh
        severity: blocking
        timeout_seconds: 120
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes carries the ADR-0080 entry.
        prompt: |
          The DIFF should include a Recent-changes entry under
          tools/rule-checks/AGENT.md (or sibling) citing ADR-0080,
          naming the new rule-check directory, and naming the two
          failure modes (TypeError in op.* call + multi-head).
          Return verdict 'pass' when present; 'fail' otherwise.
        severity: blocking
```

## Risks / unknowns

- **Existing rule-check test pattern** may be .sh or .py;
  STUDY confirms which before writing the test file.
- **`alembic upgrade --sql head` output format**: the assert
  on `CREATE|ALTER|INSERT|DROP` is loose enough to survive
  format changes; if an empty-migration false positive arises
  later, add a `--allow-empty` opt-out (ADR mentions this).
- **Worker auto-discovery**: if the rule-check loader is more
  picky than directory traversal (e.g. requires registration in
  a manifest), the worker scoping needs the manifest edit too.

## Diagram

Reference ADR-0080's flowchart.

## Decisions captured during execution

_Empty._

## Post-mortem

_Filled on completion._
